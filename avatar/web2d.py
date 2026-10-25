"""Live2D 渲染宿主（父进程侧）。

DeskPet 主线程归 Tk，pywebview 又必须占用主线程，所以角色画面交给
avatar/web2d_renderer.py 单独跑一个进程。这一层是"遥控器"：
    起停子进程、发命令、收状态与鼠标事件，并且绝不让子进程拖垮主程序。

缺 pywebview、缺运行时文件、子进程挂了，都只影响"画面"这一件事：
is_available() 返回 False，上层照旧退回占位形象，聊天不受影响。

接口与 avatar.live2d.Live2DHost 一致，另外按需求补齐
load_model / unload_model / set_visible / set_scale / set_expression /
play_motion / set_emotion / get_status。
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from avatar.live2d import Live2DHost

RUNTIME_FILES = ("renderer.html", "pet_render.js", "pet_page.js", "live2dcubismcore.min.js")
START_TIMEOUT = 60.0
CREATE_NO_WINDOW = 0x08000000
NOT_READY = "Live2D 画面还没准备好"
NO_MODEL = "还没有放模型"
NO_RUNTIME = "缺少 Live2D 运行文件"
# 心跳：子进程超过这么久一句话都不说，就当它已经不行了。
PING_SECONDS = 6.0
HEARTBEAT_SECONDS = 20.0
WATCH_INTERVAL = 3.0
MAX_RESTARTS = 3
RESTART_WINDOW = 300.0
# 子进程刚退出时别马上重启：缓一下再看，这中间要是被叫停过就不重启了。
RESTART_DELAY = 0.3


def runtime_dir():
    """渲染器自带资源的位置：打包后在解包目录，开发时在项目 resources 下。"""
    try:
        from security.config import resource_path

        return Path(resource_path("live2d"))
    except Exception:
        return Path(__file__).resolve().parents[1] / "resources" / "live2d"


def _webview_ready():
    try:
        import webview  # noqa: F401
    except Exception:
        return False
    return os.name == "nt"


NO_WEBVIEW2 = "缺 Microsoft Edge WebView2 运行时（装一次即可，装完重启桌宠）"


def webview2_state():
    """问探测层要 WebView2 运行时的状态。探测层不在也不崩，按"查不出来"处理。"""
    try:
        from adapters.local_runtimes import webview2_state as probe

        state = probe()
    except Exception:
        state = None
    if not isinstance(state, dict):
        return {"installed": None, "version": "", "reason": "查不出来"}
    return state


def webview2_missing():
    """是不是查清了确实没有 WebView2。查不出来一律当有，不拦着开窗口。"""
    try:
        return webview2_state().get("installed") is False
    except Exception:
        # 探测层自己炸了不该拦住画面：当"不知道"，照旧去开窗口。
        return False


def webview2_note():
    """给界面用的一句话：这台机器上的 WebView2 是什么情况。"""
    try:
        state = webview2_state()
        if not isinstance(state, dict):
            state = {}
    except Exception:
        return "查不出装没装（探测没答上来）"
    if state.get("installed") is False:
        return NO_WEBVIEW2
    if state.get("installed") is None:
        return "查不出装没装（%s）" % (state.get("reason") or "原因不明")
    return "已装 %s" % (state.get("version") or "")


def model_file(model):
    """模型文件在哪：界面传的是带 path 的模型对象，自愈重起时手上只有路径字符串，两种都认。"""
    if isinstance(model, (str, os.PathLike)):
        return str(model)
    path = getattr(model, "path", "")
    return str(path) if isinstance(path, str) else ""


class WebLive2DHost(Live2DHost):
    """把角色画在独立透明窗口里。缺条件就老实说不可用。"""

    name = "web2d"

    def __init__(self, runtime=None, logger=None, sink=None, scale=1.0):
        self.logger = logger
        self.sink = sink
        self._runtime = Path(runtime) if runtime else None
        self._scale = float(scale or 1.0)
        self._lock = threading.RLock()
        self._proc = None
        self._reader = None
        self._hwnd_provider = None
        self._status = {}
        self._state = "idle"
        self._error = ""
        self._model_path = ""
        self._available = None
        self._stopped = False
        self._last_beat = 0.0
        self._restarts = []
        self._watcher = None
        self._reduced_motion = False

    # ---------- 可用性 ----------
    def runtime_files(self):
        base = self._runtime or runtime_dir()
        return [str(base / name) for name in RUNTIME_FILES]

    def missing_files(self):
        return [path for path in self.runtime_files() if not Path(path).is_file()]

    def is_available(self):
        with self._lock:
            if self._available is not None:
                return self._available
            self._available = self._probe()
            if not self._available and self._error and self.logger:
                self.logger.info("live2d render host unavailable: %s", self._error)
            return self._available

    def _probe(self):
        if not _webview_ready():
            self._error = "这台机器上没有可用的 Live2D 渲染环境"
            return False
        missing = self.missing_files()
        if missing:
            self._error = NO_RUNTIME
            return False
        # pywebview 在 Windows 上要靠系统里的 WebView2 才开得出窗口。缺了它，
        # 失败会发生在建窗口那一刻，报出来的是一句看不懂的错。这里先查清楚，
        # 用户拿到的就是一句能照做的提示。查不出来时不拦，照旧去开窗口。
        if webview2_missing():
            self._error = NO_WEBVIEW2
            return False
        return True

    def unavailable_reason(self):
        self.is_available()
        return self._error

    # ---------- 装配 ----------
    def attach_window(self, provider):
        """provider() 返回桌宠窗口句柄；角色窗口跟着它走。"""
        self._hwnd_provider = provider
        return provider

    def set_sink(self, sink):
        """sink(name, payload) 在工作线程里被调用，界面要自己切回主线程。"""
        self.sink = sink
        return sink

    def _notify(self, name, payload=None):
        sink = self.sink
        if sink is None:
            return
        try:
            sink(name, payload)
        except Exception as exc:
            self._warn("live2d sink %s failed: %s", name, exc)

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)

    # ---------- 子进程 ----------
    def _command(self):
        script = Path(__file__).with_name("web2d_renderer.py")
        if getattr(sys, "frozen", False):
            return [sys.executable, "--live2d-renderer"]
        return [sys.executable, "-u", str(script)]

    def _args(self, model_path):
        base = self._runtime or runtime_dir()
        args = [
            "--runtime",
            str(base),
            "--model",
            str(model_path),
            "--scale",
            str(self._scale),
        ]
        hwnd = self._window_handle()
        if hwnd:
            args += ["--hwnd", str(hwnd)]
        return args

    def _window_handle(self):
        if self._hwnd_provider is None:
            return 0
        try:
            return int(self._hwnd_provider() or 0)
        except Exception:
            return 0

    def running(self):
        proc = self._proc
        return bool(proc is not None and proc.poll() is None)

    def start(self, model):
        """起子进程并开始加载。不阻塞界面，结果通过 sink 回报。"""
        if not self.is_available():
            return {"ok": False, "host": self.name, "error": self._error or NO_RUNTIME}
        path = model_file(model)
        if not path:
            return {"ok": False, "host": self.name, "error": NO_MODEL}
        if not Path(path).is_file():
            return {"ok": False, "host": self.name, "error": "模型文件不存在"}
        if self.running():
            if path == self._model_path:
                self._send({"cmd": "attach", "hwnd": self._window_handle()})
                return {"ok": True, "host": self.name, "note": "已经在显示"}
            self.stop()
        self._model_path = path
        self._stopped = False
        env = dict(os.environ)
        # 子进程的协议走管道，缓冲必须关掉，否则消息会卡在缓冲区里。
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            self._proc = subprocess.Popen(
                self._command() + self._args(path),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                env=env,
                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception as exc:
            self._state = "failed"
            self._error = "渲染进程起不来：%s" % exc
            self._warn("live2d spawn failed: %s", exc)
            return {"ok": False, "host": self.name, "error": self._error}
        self._state = "starting"
        self._error = ""
        self._last_beat = time.time()
        # 子进程可能一开机就退出：读线程会立刻把 self._proc 清空，
        # 所以这里先把句柄存进局部变量，两个线程拿到的是同一个进程。
        proc = self._proc
        self._reader = threading.Thread(target=self._read_loop, args=(proc,), daemon=True)
        self._reader.start()
        threading.Thread(target=self._log_loop, args=(proc,), daemon=True).start()
        self._start_watcher()
        if self.logger:
            self.logger.info("live2d render process started: %s", path)
        return {"ok": True, "host": self.name, "note": "正在加载角色", "pending": True}

    def stop(self, timeout=6.0):
        with self._lock:
            proc = self._proc
            self._proc = None
            self._stopped = True
            self._state = "stopped"
        if proc is None:
            return True
        try:
            if proc.poll() is None:
                try:
                    proc.stdin.write(b'{"cmd":"quit"}\n')
                    proc.stdin.flush()
                except Exception:
                    pass
                proc.wait(timeout=timeout)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        for stream in (proc.stdin, proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass
        if self.logger:
            self.logger.info("live2d render process stopped")
        return True

    def _send(self, message):
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return False
        try:
            line = json.dumps(message, ensure_ascii=False) + "\n"
            proc.stdin.write(line.encode("utf-8"))
            proc.stdin.flush()
            return True
        except Exception as exc:
            self._warn("live2d send failed: %s", exc)
            return False

    def _read_loop(self, proc):
        stream = proc.stdout
        while True:
            try:
                raw = stream.readline()
            except Exception:
                break
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            self._last_beat = time.time()
            try:
                message = json.loads(line)
            except Exception:
                continue
            try:
                self._dispatch(message)
            except Exception as exc:
                self._warn("live2d message failed: %s", exc)
        self._on_exit(proc)

    def _log_loop(self, proc):
        """子进程的日志只进日志文件，不进用户界面，也不进模型上下文。"""
        if proc is None:
            return
        stream = proc.stderr
        if stream is None:
            return
        while True:
            try:
                raw = stream.readline()
            except Exception:
                break
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self._last_beat = time.time()
                if self.logger:
                    self.logger.info("live2d renderer: %s", line)

    def _on_exit(self, proc):
        with self._lock:
            if self._proc is not proc:
                return
            self._proc = None
            if not self._stopped:
                self._state = "failed"
                self._error = "角色画面已经关闭"
                if self.logger:
                    self.logger.info("live2d render process exited")
        if not self._stopped:
            self._notify("live2d_exit", {"reason": self._error})
            # WebView2 崩了或者窗口被别的程序关掉时，子进程会就此消失。
            # 这时必须再起一个，否则桌面上会一直是占位形象，角色看着就像摆设。
            # 反复起不来由 _recover 的配额兜住，不会无限重试。
            self._restart_soon("角色画面已经关闭", self._model_path)

    def _restart_soon(self, reason, model):
        """缓一小会儿再重起，避开刚退出时的一连串连锁重启。"""
        def later():
            time.sleep(RESTART_DELAY)
            self._recover(reason, model)
        threading.Thread(target=later, name="live2d-again", daemon=True).start()

    # ---------- 心跳与自愈 ----------
    def _start_watcher(self):
        """看着子进程还在不在说话。窗口被关掉、WebView2 崩掉、进程假死，
        都靠这里发现并重起一个，而不是让桌面上只剩一块空白。"""
        with self._lock:
            alive = self._watcher is not None and self._watcher.is_alive()
        if alive:
            return self._watcher
        self._watcher = threading.Thread(
            target=self._watch_loop, name="live2d-watch", daemon=True
        )
        self._watcher.start()
        return self._watcher

    def _watch_loop(self):
        while True:
            time.sleep(WATCH_INTERVAL)
            with self._lock:
                proc = self._proc
                stopped = self._stopped
                model = self._model_path
            if stopped or proc is None:
                continue
            if proc.poll() is not None:
                continue  # 读线程会处理退出并回报
            if time.time() - self._last_beat < PING_SECONDS:
                continue
            self._send({"cmd": "status"})
            if time.time() - self._last_beat < HEARTBEAT_SECONDS:
                continue
            self._recover("渲染子进程没有回应", model)

    def _recover(self, reason, model=None):
        """重起渲染子进程；反复失败就如实说清楚，退回占位形象。"""
        if self._stopped:
            # 已经有人明确喊过停了。正在退出的旧子进程不该再拉一个新的起来，
            # 否则关掉角色或者换模型之后又会冒出一个渲染进程。
            return False
        now = time.time()
        self._restarts = [stamp for stamp in self._restarts if now - stamp < RESTART_WINDOW]
        if len(self._restarts) >= MAX_RESTARTS:
            with self._lock:
                self._state = "failed"
                self._error = "角色画面反复出错，当前只保留透明拖动区域"
            self._warn("live2d render keeps failing: %s", reason)
            self._notify("live2d_error", {"error": self._error})
            return False
        self._restarts.append(now)
        self._warn("live2d render restarting: %s", reason)
        self._notify("live2d_restarting", {"reason": reason, "count": len(self._restarts)})
        try:
            self.stop(timeout=3.0)
        except Exception as exc:
            self._warn("live2d restart stop failed: %s", exc)
        if model is None:
            return False
        result = self.start(model)
        if not result.get("ok"):
            # 重起失败不能只留一句"重起中"，日志里要能看出到底卡在哪。
            self._warn("live2d restart failed: %s", result.get("error"))
        return bool(result.get("ok"))

    def restart_count(self):
        return len(self._restarts)

    def _dispatch(self, message):
        event = str(message.get("ev") or "")
        if event in ("boot", "gone", "hit"):
            return
        if event == "ready":
            status = message.get("status") or {}
            with self._lock:
                self._status = dict(status)
                self._state = "ready"
                self._error = ""
            self._notify("live2d_ready", dict(status))
            return
        if event == "loadfail":
            reason = str(message.get("message") or (message.get("status") or {}).get("error") or "角色加载失败")
            with self._lock:
                self._state = "failed"
                self._error = reason
                self._status = dict(message.get("status") or {})
            self._notify("live2d_error", {"error": reason})
            return
        if event == "error":
            reason = str(message.get("message") or "角色画面出错")
            with self._lock:
                self._error = reason
                if self._state in ("idle", "starting"):
                    self._state = "failed"
            self._notify("live2d_error", {"error": reason, "cmd": message.get("cmd")})
            return
        if event == "status":
            data = dict(message.get("data") or {})
            with self._lock:
                self._status = data
                # 画面自己报就绪就以它为准：一次误报不该把角色一直按在失败状态上。
                if data.get("ready") and self._state != "ready":
                    self._state = "ready"
                    self._error = ""
            self._notify("live2d_status", data)
            return
        if event == "result":
            self._notify("live2d_result", message)
            return
        self._notify("live2d_" + event, message)

    # ---------- Live2DHost 接口 ----------
    def load(self, model):
        return self.start(model)

    def _ready(self):
        with self._lock:
            return self._state == "ready"

    def _declared(self, key):
        with self._lock:
            return list((self._status or {}).get(key) or [])

    def _reject(self, reason):
        return {"ok": False, "host": self.name, "error": reason}

    def set_expression(self, expression):
        name = str(expression or "").strip()
        if not self._ready():
            return self._reject(self._error or NOT_READY)
        declared = self._declared("expressions")
        if not declared:
            return self._reject("这个模型还没有表情资源")
        if name not in declared:
            return self._reject("模型里没有表情：%s" % (name or "(空)"))
        self._send({"cmd": "expression", "name": name})
        return {"ok": True, "host": self.name, "expression": name}

    def play_motion(self, group, index=0):
        name = str(group or "").strip()
        if not self._ready():
            return self._reject(self._error or NOT_READY)
        declared = self._declared("motions")
        if not declared:
            return self._reject("这个模型还没有动作资源")
        if name not in declared:
            return self._reject("模型里没有这组动作：%s" % (name or "(空)"))
        self._send({"cmd": "motion", "group": name, "index": int(index or 0)})
        return {"ok": True, "host": self.name, "group": name, "index": int(index or 0)}

    def set_emotion(self, emotion):
        name = str(emotion or "").strip()
        if not self._ready():
            return self._reject(self._error or NOT_READY)
        self._send({"cmd": "emotion", "name": name})
        return {"ok": True, "host": self.name, "emotion": name}

    def react(self, name="click"):
        """用户点了桌宠，让角色当场有个反应。画面没就绪就如实说没有。"""
        reaction = str(name or "click")
        if not self._ready():
            return self._reject(self._error or NOT_READY)
        self._send({"cmd": "react", "name": reaction})
        return {"ok": True, "host": self.name, "reaction": reaction}

    def set_reduced_motion(self, reduced):
        """减少动态效果：角色不再自动摆动和呼吸，眨眼留着。

        画面还没起来也照样记住，等子进程就绪再补发一次。
        """
        flag = bool(reduced)
        self._reduced_motion = flag
        if self.running():
            self._send({"cmd": "reduced-motion", "value": flag})
        return flag

    def reduced_motion(self):
        return bool(getattr(self, "_reduced_motion", False))

    def set_visible(self, visible):
        flag = bool(visible)
        if self.running():
            self._send({"cmd": "visible", "value": flag})
        if not flag:
            self._notify("live2d_visible", {"value": False})
        return flag

    def set_scale(self, value):
        try:
            self._scale = max(0.2, min(4.0, float(value)))
        except (TypeError, ValueError):
            return self._scale
        if self.running():
            self._send({"cmd": "scale", "value": self._scale})
        return self._scale

    def scale(self):
        return self._scale

    def sync_geometry(self):
        """让角色窗口贴住桌宠窗口。窗口还没就绪就什么都不做。"""
        if not self.running():
            return False
        hwnd = self._window_handle()
        if not hwnd:
            return False
        return self._send({"cmd": "attach", "hwnd": hwnd})

    def load_model(self, model=None):
        if model is None:
            path = self._model_path
            if not path:
                return self._reject(NO_MODEL)
            self._send({"cmd": "reload"})
            return {"ok": True, "host": self.name, "note": "重新加载"}
        return self.start(model)

    def unload_model(self):
        self._send({"cmd": "unload"})
        with self._lock:
            self._state = "idle"
            self._status = {}
        return {"ok": True, "host": self.name}

    def refresh_status(self, timeout=1.5):
        """主动要一次最新状态；界面点「刷新」时用。"""
        if not self.running():
            return self.get_status()
        self._send({"cmd": "status"})
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if self._status:
                    break
            time.sleep(0.05)
        return self.get_status()

    def request_hit(self):
        """要一次碰撞体积。

        见面把掩码清过之后再没等到新掩码时用它补回来，
        免得桌宠窗口一直是穿透状态、鼠标点不着。
        """
        if not self.running():
            return False
        return self._send({"cmd": "hit"})

    def get_status(self):
        with self._lock:
            status = dict(self._status or {})
            state = self._state
            error = self._error
            model = self._model_path
        status.update(
            {
                "host": self.name,
                "available": self.is_available(),
                "state": state,
                "running": self.running(),
                "error": error or None,
                "model_path": model,
                "scale": self._scale,
                "core": status.get("core"),
                "backend": status.get("backend"),
                "expressions": list(status.get("expressions") or []),
                "motions": list(status.get("motions") or []),
                "textures": status.get("textures") or 0,
                "masks": status.get("masks") or 0,
                "drawables": status.get("drawables") or 0,
                "parameters": status.get("parameters") or 0,
                "window": list(status.get("window") or []),
            }
        )
        return status

    def snapshot(self):
        status = self.get_status()
        return {
            "host": self.name,
            "available": status.get("available"),
            "rendering": status.get("state") == "ready",
            "reason": status.get("error"),
            "status": status,
        }

    def describe(self):
        status = self.get_status()
        return {
            "host": self.name,
            "available": status.get("available"),
            "state": status.get("state"),
            "model": Path(self._model_path).name if self._model_path else "",
            "scale": status.get("scale"),
            "expressions": status.get("expressions"),
            "motions": status.get("motions"),
            "textures": status.get("textures"),
            "backend": status.get("backend"),
            "core": status.get("core"),
            "error": status.get("error"),
            "runtime": str(self._runtime or runtime_dir()),
            "reason": self._error or None,
        }


def build_host(config=None, logger=None, **kwargs):
    """按配置装一个 Live2D 渲染宿主；不可用时返回 None，由上层退回占位形象。"""
    runtime = kwargs.pop("runtime", None)
    if runtime is None and config is not None:
        runtime = getattr(config, "live2d_runtime_dir", None)
    host = WebLive2DHost(runtime=runtime, logger=logger, scale=getattr(config, "live2d_scale", 1.0) if config else 1.0)
    if not host.is_available():
        return None
    return host
