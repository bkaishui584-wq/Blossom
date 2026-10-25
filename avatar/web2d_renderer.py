"""Live2D 渲染子进程。

pywebview 的窗口必须跑在进程主线程，而 DeskPet 的主线程已经被 Tk 占用，
所以角色画面单独起一个进程：Tk 那边继续当"身体"和交互层，这里只负责画角色。

这个进程：
    只做渲染、拖动、命中判断，把状态和鼠标事件用 JSON 行报回父进程；
    不碰大脑、不碰数据库、不读密钥、不执行任何外部命令。

通信：
    子进程 -> 父进程：stdout，每行一个 JSON。
    父进程 -> 子进程：stdin，每行一个 JSON。
日志一律走 stderr，避免污染协议。
"""

import argparse
import base64
import ctypes
import http.server
import json
import os
import socketserver
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

WINDOW_TITLE = "Blossom Live2D"
BLOCK = 6  # 命中判断的方块边长（设备像素）
POLL_SECONDS = 0.04
POINTER_EVERY = 1  # 每次轮询都回光标位置，约 25 次/秒：更贴手
POINTER_FIELD = 3.0  # 光标归一的视场：桌宠窗口宽度的这个倍数处到极限
POINTER_DEADBAND = 0.005  # 归一化位移小于这个数就不重复发
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_APPWINDOW = 0x00040000
LWA_COLORKEY = 0x00000001
COLOR_BTNFACE = 15
HIT_ALPHA = 16
MIN_SIDE = 40
MAX_SIDE = 4096


def log(message):
    try:
        sys.stderr.write("[live2d] %s\n" % message)
        sys.stderr.flush()
    except Exception:
        pass


def emit(event, **fields):
    payload = {"ev": event}
    payload.update(fields)
    try:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def _user32():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    user32.ScreenToClient.restype = wintypes.BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongPtrW.restype = ctypes.c_longlong
    user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_longlong]
    user32.SetWindowLongPtrW.restype = ctypes.c_longlong
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.GetDpiForWindow.argtypes = [wintypes.HWND]
    user32.GetDpiForWindow.restype = ctypes.c_uint
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetClientRect.restype = wintypes.BOOL
    user32.GetSysColor.argtypes = [ctypes.c_int]
    user32.GetSysColor.restype = wintypes.DWORD
    user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_ubyte, wintypes.DWORD]
    user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
    return user32


def window_rect(user32, hwnd):
    """窗口在屏幕上的物理位置。子进程是 DPI 感知的，拿到的是真实像素。"""
    rect = wintypes.RECT()
    if not hwnd or not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        return None
    return (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)


def virtual_screen(user32):
    return (
        user32.GetSystemMetrics(76),
        user32.GetSystemMetrics(77),
        user32.GetSystemMetrics(78),
        user32.GetSystemMetrics(79),
    )


class _Routes(http.server.SimpleHTTPRequestHandler):
    """静态服务给页面用；鼠标事件走 /ev 回来，不依赖 pywebview 的桥。"""

    routes = {}
    on_event = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=".", **kwargs)

    def translate_path(self, path):
        for prefix, base in self.routes.items():
            if path.startswith(prefix):
                rel = path[len(prefix):].split("?")[0].split("#")[0]
                return str(Path(base).joinpath(*[p for p in rel.split("/") if p]))
        return str(Path(".") / "__missing__")

    def do_POST(self):
        if not self.path.startswith("/ev"):
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            message = json.loads(raw.decode("utf-8", "replace") or "{}")
        except Exception:
            message = {}
        try:
            if _Routes.on_event is not None:
                _Routes.on_event(str(message.get("name") or ""), message.get("payload"))
        except Exception as exc:
            log("event failed: %r" % (exc,))
        try:
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except Exception:
            pass

    def log_message(self, *args):
        pass


def serve(routes, on_event=None):
    _Routes.routes = dict(routes)
    _Routes.on_event = on_event
    httpd = socketserver.TCPServer(("127.0.0.1", 0), _Routes)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def decode_mask(raw):
    if not raw:
        return None
    try:
        bits = base64.b64decode(raw.get("data") or "")
        cols = int(raw.get("cols") or 0)
        rows = int(raw.get("rows") or 0)
    except Exception:
        return None
    if cols <= 0 or rows <= 0 or len(bits) * 8 < cols * rows:
        return None
    return (cols, rows, bits)


class Hotspot(threading.Thread):
    """光标只在角色身上时窗口才接鼠标，透明的地方让点击穿过去。"""

    def __init__(self, user32, on_hover):
        super().__init__(daemon=True)
        self.user32 = user32
        self.on_hover = on_hover
        self.lock = threading.Lock()
        self.hwnd = 0
        self.mask = None
        self.css = (1, 1)
        self.stop = False
        self.hold = False
        self.inside = False
        self.clickthrough = None
        self.on_pointer = None  # 归一化光标位置回调，给页面做视线跟随
        self._last_pointer = None
        self._pointer_tick = 0

    def update(self, hwnd=None, mask=None, css=None):
        with self.lock:
            if hwnd is not None:
                self.hwnd = int(hwnd)
            if mask is not None:
                self.mask = mask
            if css is not None:
                self.css = css

    def clear_hit(self):
        with self.lock:
            self.mask = None

    def _client_size(self, hwnd):
        # 命中用的是物理像素的客户区尺寸，和掩码画布一致；CSS 尺寸在缩放屏上不等于它。
        rect = wintypes.RECT()
        if not self.user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            return None
        return (max(1, int(rect.right)), max(1, int(rect.bottom)))

    def _hit_client(self, px, py, size=None):
        with self.lock:
            mask = self.mask
        if not mask or size is None:
            return False
        cols, rows, bits = mask
        width, height = size
        col = int(px / width * cols)
        row = int(py / height * rows)
        if col < 0 or row < 0 or col >= cols or row >= rows:
            return False
        index = row * cols + col
        return bool((bits[index >> 3] >> (index & 7)) & 1)

    def _hit(self, hwnd, x, y):
        point = wintypes.POINT(x, y)
        if not self.user32.ScreenToClient(wintypes.HWND(hwnd), ctypes.byref(point)):
            return False
        return self._hit_client(point.x, point.y, self._client_size(hwnd))

    def _push_pointer(self, px, py, size):
        """光标位置回给页面：不管光标在不在桌宠身上，角色的眼睛和头都跟着它。

        以桌宠中心为原点归一化，视场取窗口宽度的 POINTER_FIELD 倍。以前拿窗口
        自己当视场，光标一离开窗口就顶到 ±1，头一直贴着极限、光标一跨就跳，
        看着不流畅；现在视场宽了，光标在桌宠附近是连续的小幅变化。
        """
        if self.on_pointer is None or not size:
            return
        self._pointer_tick += 1
        # 约 12 次/秒：手移动快的时候也跟得住，又不会拿 evaluate_js 把渲染进程压着跑。
        if self._pointer_tick % POINTER_EVERY:
            return
        width, height = size
        field = max(1.0, width * POINTER_FIELD / 2.0)
        nx = round((px - width / 2.0) / field, 3)
        ny = round((py - height / 2.0) / field, 3)
        last = self._last_pointer
        if last is not None and abs(nx - last[0]) < POINTER_DEADBAND and abs(ny - last[1]) < POINTER_DEADBAND:
            return
        self._last_pointer = (nx, ny)
        try:
            self.on_pointer(nx, ny)
        except Exception as exc:
            log("pointer push failed: %r" % (exc,))

    def _apply(self, hwnd, through):
        try:
            style = self.user32.GetWindowLongPtrW(wintypes.HWND(hwnd), GWL_EXSTYLE)
            new = (style | WS_EX_TRANSPARENT) if through else (style & ~WS_EX_TRANSPARENT)
            if new != style:
                self.user32.SetWindowLongPtrW(wintypes.HWND(hwnd), GWL_EXSTYLE, new)
        except Exception as exc:
            log("clickthrough failed: %r" % (exc,))

    def run(self):
        while not self.stop:
            time.sleep(POLL_SECONDS)
            with self.lock:
                hwnd = self.hwnd
                has_mask = self.mask is not None
            if not hwnd:
                continue
            if not has_mask:
                # 还没有碰撞体积时不能放行鼠标：窗口一旦长期穿透，
                # 桌宠就再也点不着了。先整块接住，掩码回来再恢复精确判定。
                if self.clickthrough is not False:
                    self.clickthrough = False
                    self._apply(hwnd, False)
                continue
            point = wintypes.POINT()
            if not self.user32.GetCursorPos(ctypes.byref(point)):
                continue
            size = self._client_size(hwnd)
            if size is None:
                continue
            self.user32.ScreenToClient(wintypes.HWND(hwnd), ctypes.byref(point))
            self._push_pointer(point.x, point.y, size)
            hit = self._hit_client(point.x, point.y, size)
            if hit != self.inside:
                self.inside = hit
                try:
                    self.on_hover(hit)
                except Exception:
                    pass
            through = False if self.hold else (not hit)
            if through != self.clickthrough:
                self.clickthrough = through
                self._apply(hwnd, through)


class Renderer:
    """一个进程里的一个窗口，一次只装一个模型。"""

    def __init__(self, options):
        self.options = options
        self.user32 = _user32()
        self.dpi_scale = 1.0
        self.runtime = Path(options.runtime)
        self.model = Path(options.model)
        self.window = None
        self.state = {
            "ready": False,
            "status": {},
            "mask": None,
            "hwnd": 0,
            "size": (0, 0),
            "drag": None,
            "scale": float(options.scale or 1.0),
            "visible": True,
        }
        self.hotspot = Hotspot(self.user32, self._on_hover)
        self.hotspot.on_pointer = self._push_pointer
        self.lock = threading.Lock()
        # 往页面里跑脚本的线程不止一个（光标跟随、命令、掩码刷新）。
        # pywebview 的 evaluate_js 是派到界面线程再等回来，几路同时进去会把
        # WebView2 弄崩，整个渲染进程跟着没掉，所以这里排成一队。
        self.js_lock = threading.Lock()
        self.quit = threading.Event()

    def _probe_dpi(self):
        """本进程是 DPI 感知的，这里拿到的是真实缩放；父进程未必是，所以由这边换算。"""
        try:
            hwnd = int(self.user32.FindWindowW(None, WINDOW_TITLE) or 0)
            if not hwnd:
                return self.dpi_scale
            dpi = int(self.user32.GetDpiForWindow(wintypes.HWND(hwnd)) or 96)
            return max(0.5, min(4.0, dpi / 96.0))
        except Exception:
            return self.dpi_scale

    # ---------- 状态 ----------
    def _title(self):
        return WINDOW_TITLE

    def _own_hwnd(self):
        hwnd = self.state.get("hwnd") or 0
        if hwnd:
            return hwnd
        found = self.user32.FindWindowW(None, self._title())
        if found:
            self.state["hwnd"] = int(found)
            self.dpi_scale = self._probe_dpi()
            self.hotspot.update(hwnd=int(found))
        return int(found or 0)

    def _apply_colorkey(self):
        """WebView2 透明处露出的是窗体默认底色，把这层底色抠掉才真的透出桌面。"""
        hwnd = self._own_hwnd()
        if not hwnd:
            return False
        try:
            key = int(self.user32.GetSysColor(COLOR_BTNFACE)) & 0xFFFFFF
            style = self.user32.GetWindowLongPtrW(wintypes.HWND(hwnd), GWL_EXSTYLE)
            if not int(style) & WS_EX_LAYERED:
                self.user32.SetWindowLongPtrW(wintypes.HWND(hwnd), GWL_EXSTYLE, int(style) | WS_EX_LAYERED)
            self.user32.SetLayeredWindowAttributes(wintypes.HWND(hwnd), key, 0, LWA_COLORKEY)
        except Exception as exc:
            log("colorkey failed: %r" % (exc,))
            return False
        self.state["colorkey"] = key
        return True

    def _hide_from_taskbar(self):
        """角色窗口不进任务栏：它是桌宠的一部分，不是独立程序窗口。"""
        hwnd = self._own_hwnd()
        if not hwnd:
            return False
        try:
            style = int(self.user32.GetWindowLongPtrW(wintypes.HWND(hwnd), GWL_EXSTYLE))
            wanted = (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
            if wanted != style:
                self.user32.SetWindowLongPtrW(wintypes.HWND(hwnd), GWL_EXSTYLE, wanted)
        except Exception as exc:
            log("taskbar hide failed: %r" % (exc,))
            return False
        return True

    def _on_hover(self, inside):
        emit("hover" if inside else "unhover", inside=bool(inside))

    def _push_pointer(self, nx, ny):
        if not self.state.get("ready"):
            return
        self.run_js("window.Pet.setPointer(%r, %r)" % (nx, ny))

    def metrics(self):
        """渲染侧真实运行的指标：帧率、参数个数、待机状态。只给开发者面板看。"""
        try:
            raw = self.run_js("JSON.stringify(window.Pet.metrics())")
            return json.loads(raw) if raw else {}
        except Exception as exc:
            log("metrics failed: %r" % (exc,))
            return {}

    def _refresh_hit(self):
        mask = None
        raw_mask = None
        try:
            raw = self.run_js("JSON.stringify(window.Pet.maskRaw())")
            raw_mask = json.loads(raw) if raw and raw != "null" else None
            mask = decode_mask(raw_mask) if raw_mask else None
        except Exception as exc:
            log("mask read failed: %r" % (exc,))
        css = (1, 1)
        try:
            raw_status = self.run_js("JSON.stringify(window.Pet.status())")
            status = json.loads(raw_status) if raw_status else None
            # 问不到就留着上一次的结果；空字典会被当成「角色还没就绪」。
            if isinstance(status, dict) and status:
                host = status.get("host") or [1, 1]
                css = (int(host[0] or 1), int(host[1] or 1))
                self.state["status"] = status
        except Exception:
            pass
        self.state["mask"] = mask
        self.hotspot.update(mask=mask, css=css, hwnd=self._own_hwnd())
        # 碰撞体积交给父进程：桌宠窗口按角色形状接鼠标，透明处照旧穿过去。
        # 每个窗口的鼠标事件只可能落到一个窗口上，渲染窗口自己是被色键穿透的，
        # 所以真正接点击的是父进程那个透明宿主。
        if raw_mask != self.state.get("mask_raw"):
            self.state["mask_raw"] = raw_mask
            emit("mask", data=raw_mask)

    def status(self):
        data = dict(self.state.get("status") or {})
        # 碰撞体积：掩码有多少格是角色本体。开发面板用它说明「能不能点中」。
        mask = self.state.get("mask")
        if mask:
            cols, rows, bits = mask
            on = sum(bin(byte).count("1") for byte in bits)
            data["mask_cols"] = cols
            data["mask_rows"] = rows
            data["mask_on"] = on
            data["mask_ratio"] = round(on / max(1, cols * rows), 3)
        data.update(
            {
                "host": data.get("host"),
                "mask": bool(self.state.get("mask")),
                "window": list(self.state.get("size") or (0, 0)),
                "visible": bool(self.state.get("visible")),
                "dpi_scale": self.dpi_scale,
            }
        )
        return data

    # ---------- 几何 ----------
    def mirror(self):
        """贴住父进程给的窗口句柄，尺寸也跟着它。"""
        hwnd = int(self.options.attach_hwnd or 0)
        if not self.dpi_scale or self.dpi_scale == 1.0:
            self.dpi_scale = self._probe_dpi()
        rect = window_rect(self.user32, hwnd)
        if rect is None:
            return False
        x, y, w, h = rect
        w = max(MIN_SIDE, min(MAX_SIDE, w))
        h = max(MIN_SIDE, min(MAX_SIDE, h))
        self.place(x, y, w, h)
        return True

    def place(self, x, y, w, h):
        left, top, width, height = virtual_screen(self.user32)
        w = max(MIN_SIDE, min(MAX_SIDE, int(w)))
        h = max(MIN_SIDE, min(MAX_SIDE, int(h)))
        x = max(left, min(int(x), left + width - w))
        y = max(top, min(int(y), top + height - h))
        old = self.state.get("size") or (0, 0)
        scale = self.dpi_scale or 1.0
        try:
            if self.window is not None:
                # pywebview 的 resize/move 收的是逻辑像素，这里手上是物理像素，先换回去。
                if (w, h) != tuple(old):
                    self.window.resize(int(round(w / scale)), int(round(h / scale)))
                self.window.move(int(round(x / scale)), int(round(y / scale)))
        except Exception as exc:
            log("place failed: %r" % (exc,))
            return False
        self.state["size"] = (w, h)
        return True

    # ---------- 命令 ----------
    def handle(self, message):
        command = message.get("cmd")
        if command == "quit":
            self.quit.set()
            return False
        if command == "attach":
            if message.get("hwnd"):
                self.options.attach_hwnd = int(message["hwnd"])
            if not self.mirror():
                emit("error", message="找不到桌宠窗口", cmd=command)
            return True
        if command == "place":
            self.place(message.get("x"), message.get("y"), message.get("w"), message.get("h"))
            return True
        if command == "scale":
            self.state["scale"] = float(message.get("value") or 1.0)
            self.run_js("window.Pet.setScale(%s)" % (self._js(self.state["scale"]),))
            self._refresh_hit()
            emit("scaled", value=self.state["scale"])
            return True
        if command == "expression":
            self._forward(
                "expression",
                "window.Pet.setExpression(%s)" % (self._js(message.get("name")),),
            )
            return True
        if command == "react":
            # 用户点了桌宠：让角色当场有个反应。没有动作资源也认，参数演出就够。
            self._forward(
                "react",
                "window.Pet.react(%s)" % (self._js(str(message.get("name") or "click")),),
            )
            return True
        if command == "motion":
            self._forward(
                "motion",
                "window.Pet.playMotion(%s, %s)"
                % (self._js(message.get("group")), self._js(int(message.get("index") or 0))),
            )
            return True
        if command == "reduced-motion":
            # 减少动态效果：摆动与呼吸停下，眨眼保留。孩子进程没就绪也照回。
            self._forward(
                "motion",
                "window.Pet.setMotion(%s)" % ("false" if message.get("value") else "true",),
            )
            return True
        if command == "emotion":
            # 情绪是认得的才算生效；渲染器没有这个情绪就如实说没有。
            self._forward(
                "emotion",
                "window.Pet.setEmotion(%s)" % (self._js(message.get("name")),),
            )
            return True
        if command == "visible":
            self.set_visible(bool(message.get("value")))
            return True
        if command == "hit":
            # 界面要一次碰撞体积：不管变没变都回一次，
            # 免得界面那边把掩码清掉之后再也补不回来。
            self._refresh_hit()
            emit("mask", data=self.state.get("mask_raw"))
            return True
        if command == "status":
            # 顺手核对一次碰撞体积：掩码要是在别处被清过，这里就补回去。
            if self.state.get("ready"):
                self._refresh_hit()
            emit("status", data=self.status())
            return True
        if command == "metrics":
            emit("result", cmd="metrics", data=self.metrics())
            return True
        if command == "samples":
            # 真实参数值：用来核对待机、表情、情绪是不是真的生效了。
            self._forward("samples", "window.Pet.samples()")
            return True
        if command == "mask":
            # 碰撞体积：把掩码给出去，界面和诊断脚本可以画出来看。
            mask = self.state.get("mask")
            if not mask:
                emit("result", cmd="mask", data={"ok": False, "error": "还没有掩码"})
            else:
                cols, rows, bits = mask
                emit(
                    "result",
                    cmd="mask",
                    data={
                        "ok": True,
                        "cols": cols,
                        "rows": rows,
                        "data": base64.b64encode(bytes(bits)).decode("ascii"),
                    },
                )
            return True
        if command == "capture":
            # 把当前画面导成 PNG，开发者面板自检用；渲染进程不改任何状态。
            self._forward("capture", "({png: window.Pet.capture()})")
            return True
        if command == "unload":
            self.run_js("window.Pet.unload()")
            self.state["ready"] = False
            self.hotspot.clear_hit()
            emit("unloaded")
            return True
        if command == "reload":
            self.load_model()
            return True
        return True

    @staticmethod
    def _js(value):
        """Python 值换成 JS 字面量。%r 会把 None 写成 None，在 JS 里是语法错误。"""
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return "null"

    def _forward(self, name, script):
        """把页面里的一次调用结果回给父进程。

        pywebview 的 evaluate_js 有时直接给出解析好的对象，有时给字符串，
        所以统一在外面套一层 JSON.stringify，再两种都认。
        """
        raw = self.run_js("JSON.stringify(%s)" % script)
        if isinstance(raw, (dict, list)):
            data = raw
        elif isinstance(raw, str) and raw.strip() not in ("", "null", "undefined"):
            try:
                data = json.loads(raw)
            except Exception:
                data = {"ok": False, "error": "渲染器返回了看不懂的结果"}
        else:
            data = {"ok": False, "error": "渲染器没有返回结果"}
        if isinstance(data, list):
            data = {"ok": True, "items": data}
        emit("result", cmd=name, data=data)

    def run_js(self, script):
        try:
            with self.js_lock:
                return self.window.evaluate_js(script)
        except Exception as exc:
            log("js failed: %r" % (exc,))
            return None

    def set_visible(self, visible):
        self.state["visible"] = bool(visible)
        try:
            if visible:
                self.window.show()
            else:
                self.window.hide()
        except Exception as exc:
            log("visibility failed: %r" % (exc,))
        emit("visible", value=bool(visible))

    # ---------- 模型 ----------
    def model_url(self):
        return "/model/" + self.model.name

    def load_model(self):
        if not self.model.is_file():
            self.state["ready"] = False
            emit("loadfail", message="模型文件不存在：%s" % self.model)
            return
        self.run_js(
            "window.Pet.load(%s, {scale: %s})"
            % (self._js(self.model_url()), self._js(self.state["scale"]))
        )
        deadline = time.time() + 45
        ready = False
        while time.time() < deadline:
            if self.run_js("window.Pet.status().ready") is True:
                ready = True
                break
            time.sleep(0.25)
        # 结论只看这一轮问出来的答案。status() 读的是缓存，偶发问不到时会空掉，
        # 拿它判断会把已经画在屏幕上的角色误报成「加载失败」。
        self.state["ready"] = ready
        self._refresh_hit()
        emit("ready" if ready else "loadfail", status=self.status())

    # ---------- 拖动 ----------
    def drag_begin(self):
        hwnd = self._own_hwnd()
        rect = window_rect(self.user32, hwnd)
        if rect is None:
            self.state["drag"] = None
            return
        self.state["drag"] = {"origin": (rect[0], rect[1]), "size": (rect[2], rect[3])}
        self.hotspot.hold = True

    def drag_move(self, dx, dy):
        info = self.state.get("drag")
        if not info:
            return
        ox, oy = info["origin"]
        w, h = info["size"]
        # 页面报的是 CSS 像素，这里是物理像素，缩放屏上要乘回去。
        scale = self.dpi_scale or 1.0
        dx = int(round(float(dx or 0) * scale))
        dy = int(round(float(dy or 0) * scale))
        self.place(ox + dx, oy + dy, w, h)
        emit("moving", x=ox + dx, y=oy + dy, w=w, h=h)

    def drag_end(self):
        info = self.state.get("drag")
        self.state["drag"] = None
        self.hotspot.hold = False
        if not info:
            return
        rect = window_rect(self.user32, self._own_hwnd())
        if rect:
            self.state["size"] = (rect[2], rect[3])
            emit("moved", x=rect[0], y=rect[1], w=rect[2], h=rect[3])

    # ---------- 主流程 ----------
    def on_event(self, name, payload):
        if name == "ready" or name == "error":
            self._refresh_hit()
            emit(name, status=self.status())
        elif name == "mask_ready":
            self._refresh_hit()
            emit("hit", data=self.status())
        elif name == "drag_begin":
            self.drag_begin()
        elif name == "drag":
            data = payload or {}
            self.drag_move(data.get("dx") or 0, data.get("dy") or 0)
        elif name == "drag_end":
            self.drag_end()
        else:
            emit(name, data=payload)

    def run(self):
        httpd, port = serve({"/rt/": self.runtime, "/model/": self.model.parent}, self.on_event)
        url = "http://127.0.0.1:%d/rt/renderer.html" % port
        log("serving %s for %s" % (url, self.model))
        emit("boot", port=port)
        self.hotspot.start()
        self.window = _create_window(self.options, url, self)
        _start(self, self.window, httpd)
        return 0


def _create_window(options, url, renderer):
    import webview

    width = max(MIN_SIDE, int(options.w or 300))
    height = max(MIN_SIDE, int(options.h or 400))
    window = webview.create_window(
        WINDOW_TITLE,
        url=url,
        width=width,
        height=height,
        x=int(options.x or 0),
        y=int(options.y or 0),
        frameless=True,
        transparent=True,
        on_top=True,
        resizable=False,
        min_size=(MIN_SIDE, MIN_SIDE),
        easy_drag=False,
    )
    return window


def _start(renderer, window, httpd):
    import webview

    def worker(win):
        try:
            time.sleep(1.2)
            if renderer.options.attach_hwnd:
                if not renderer.mirror():
                    emit("error", message="桌宠窗口还没准备好")
            renderer._own_hwnd()
            renderer._apply_colorkey()
            renderer._hide_from_taskbar()
            if not renderer.state.get("size"):
                renderer.place(renderer.options.x, renderer.options.y, renderer.options.w, renderer.options.h)
            renderer.load_model()
        except Exception as exc:
            log("startup failed: %r" % (exc,))
            emit("error", message=str(exc))
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except Exception:
                continue
            try:
                if not renderer.handle(message):
                    break
            except Exception as exc:
                emit("error", message=str(exc), cmd=message.get("cmd"))
        renderer.hotspot.stop = True
        try:
            win.destroy()
        except Exception:
            pass

    try:
        webview.start(worker, (window,), debug=False)
    finally:
        try:
            httpd.shutdown()
        except Exception:
            pass
        emit("gone")
    return 0


def _hard_exit(code):
    """WebView2 与 pythonnet 关窗后经常不肯让进程结束，留在父进程那边就是一个
    没有窗口的僵尸渲染进程。这里直接硬退出，父进程才能拿到 EOF 并重新起一个。"""
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(code)


def _use_utf8():
    """管道默认不是 UTF-8，先统一，免得中文名字在协议里变乱码。"""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            continue


def main(argv=None):
    _use_utf8()
    parser = argparse.ArgumentParser(description="Blossom Live2D renderer")
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--hwnd", type=int, default=0)
    parser.add_argument("--x", type=int, default=0)
    parser.add_argument("--y", type=int, default=0)
    parser.add_argument("--w", type=int, default=300)
    parser.add_argument("--h", type=int, default=400)
    parser.add_argument("--scale", type=float, default=1.0)
    options = parser.parse_args(argv)
    options.attach_hwnd = int(options.hwnd or 0)
    renderer = Renderer(options)
    return _hard_exit(renderer.run())


if __name__ == "__main__":
    sys.exit(main())
