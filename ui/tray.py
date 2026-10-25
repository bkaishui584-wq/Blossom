"""系统托盘与全局快捷键（Windows 原生，失败就安静降级）。

托盘是可选装饰：装不上、被系统拒绝、不是 Windows，桌宠照常运行，
右键菜单里的显示/隐藏、聊天、设置、退出一直都在。
不装第三方库，只用系统自带接口；每一步失败都只记日志，不向用户抛异常。

托盘和快捷键都只是「叫醒桌宠的入口」，它们不碰权限、工具和安全规则。
"""

import ctypes
import os
import threading
from pathlib import Path

DEFAULT_TOOLTIP = "桌宠"
TOOLTIP_MAX = 120

# 托盘菜单：(命令号, 菜单文字, DesktopPet 上的方法名)
MENU = (
    (1, "显示 / 隐藏桌宠", "toggle_visible"),
    (2, "打开聊天", "toggle_chat"),
    (3, "设置", "open_settings"),
    (4, "退出", "close"),
)

# 快捷键写法 -> Windows 修饰键
HOTKEY_MODIFIERS = {
    "ctrl": 0x0002,
    "control": 0x0002,
    "alt": 0x0001,
    "shift": 0x0004,
    "win": 0x0008,
    "super": 0x0008,
}
# 几个常用键名 -> 虚拟键码
HOTKEY_KEYS = {
    "space": 0x20,
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "tab": 0x09,
}

# 字幕快捷键用固定 ID，聊天快捷键保持原来的 1。
EXTRA_HOTKEY_IDS = {
    "start_subtitles_from_preferences": 2,
    "toggle_subtitle_pause": 3,
    "stop_subtitles": 4,
    "toggle_subtitle_overlay": 5,
}

# --- Win32 常量 ---
WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_HOTKEY = 0x0312
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203
WM_NULL = 0x0000
WM_QUIT = 0x0012
WM_APP = 0x8000
TRAY_CALLBACK = WM_APP + 1
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04
MF_STRING = 0x0000
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100
IDI_APPLICATION = 32512
IMAGE_ICON = 1
LR_LOADFROMFILE, LR_DEFAULTSIZE, LR_SHARED = 0x0010, 0x0040, 0x8000


def tooltip_text(text):
    """托盘提示文字。太长会被系统截断，这里先裁好。"""
    clean = " ".join(str(text or "").split())
    return (clean or DEFAULT_TOOLTIP)[:TOOLTIP_MAX]


def parse_hotkey(spec):
    """把 'ctrl+alt+d' 解析成 (修饰键, 虚拟键码)。解析不出来返回 (0, 0)。"""
    parts = [chunk.strip().lower() for chunk in str(spec or "").split("+")]
    parts = [chunk for chunk in parts if chunk]
    if len(parts) < 2:
        return (0, 0)
    mods = 0
    for chunk in parts[:-1]:
        if chunk not in HOTKEY_MODIFIERS:
            return (0, 0)
        mods |= HOTKEY_MODIFIERS[chunk]
    key = parts[-1]
    if key in HOTKEY_KEYS:
        vk = HOTKEY_KEYS[key]
    elif len(key) == 1 and (key.isalnum()):
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    else:
        return (0, 0)
    return (mods, vk) if mods else (0, 0)


def menu_map():
    """命令号 -> 方法名。测试和界面都用它，避免两处对不上。"""
    return {command: method for command, _label, method in MENU}


def _load_shell():
    """装上托盘需要的系统接口。拿不到就返回 None（不是 Windows 也会走到这里）。"""
    if os.name != "nt":
        return None
    try:
        from ctypes import wintypes

        class NOTIFYICONDATA(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128),
            ]

        # ctypes.wintypes 里没有 WNDCLASSW，按 Win32 的定义自己补一个。
        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32
        # 64 位下句柄是 64 位宽，不声明 restype 会被截断成 32 位，窗口就建不出来。
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        user32.CreateWindowExW.restype = wintypes.HWND
        # 带类型的参数表：hInstance 是 64 位句柄，不声明会被当成 32 位整数而溢出。
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            ctypes.c_void_p,
        ]
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadIconW.restype = wintypes.HICON
        user32.CreatePopupMenu.restype = wintypes.HMENU
        user32.RegisterClassW.restype = wintypes.ATOM
        # 窗口过程要回调 DefWindowProcW，返回值同样是 64 位；不声明类型时
        # lparam 会被当成 32 位整数而溢出，消息处理会静默失败。
        user32.DefWindowProcW.restype = ctypes.c_longlong
        user32.DefWindowProcW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL
        return {
            "wintypes": wintypes,
            "user32": user32,
            "shell32": shell32,
            "kernel32": kernel32,
            "NOTIFYICONDATA": NOTIFYICONDATA,
            "WNDCLASSW": WNDCLASSW,
        }
    except Exception:
        return None


class TrayIcon:
    """托盘图标 + 可选全局快捷键。拿不到系统接口时 available() 为 False。"""

    WINDOW_CLASS = "BlossomTrayWnd"
    HOTKEY_ID = 1

    def __init__(
        self,
        owner,
        icon_path="",
        tooltip=DEFAULT_TOOLTIP,
        hotkey="",
        extra_hotkeys=None,
        logger=None,
        loop=None,
        dispatch=None,
    ):
        self.owner = owner
        # 可选：把菜单命令切回界面线程执行。托盘有自己的线程，不能直接碰界面控件。
        self.dispatch = dispatch
        self.icon_path = str(icon_path or "")
        self.tooltip = tooltip_text(tooltip)
        self.hotkey = str(hotkey or "")
        self.extra_hotkeys = {
            str(action): str(spec or "").strip()
            for action, spec in dict(extra_hotkeys or {}).items()
        }
        self.logger = logger
        self.loop = loop  # 测试用：换成自己的循环，避免真去碰系统
        self.started = False
        self.hotkey_registered = False
        self.registered_hotkeys = {}
        self.hotkey_errors = {}
        self.failed = ""
        self.last_command = ""
        self._shell = None
        self._probed = False
        self._thread = None
        self._stop = threading.Event()
        self._thread_id = 0
        self._hwnd = 0
        self._wndproc = None

    # ---------- 状态 ----------
    def available(self):
        if not self._probed:
            self._shell = _load_shell()
            self._probed = True
        return self._shell is not None

    def describe(self):
        """开发者面板用。绝不发给模型。"""
        mods, vk = parse_hotkey(self.hotkey)
        return {
            "available": self.available(),
            "started": self.started,
            "tooltip": self.tooltip,
            "hotkey": self.hotkey,
            "hotkey_ok": bool(mods and vk),
            "hotkey_registered": self.hotkey_registered,
            "registered_hotkeys": dict(self.registered_hotkeys),
            "hotkey_errors": dict(self.hotkey_errors),
            "last_command": self.last_command,
            "failed": self.failed,
        }

    # ---------- 生命周期 ----------
    def start(self):
        """起托盘。起不来就返回 False，调用方照常跑。"""
        if self.started:
            return True
        if not self.available():
            return False
        self._stop.clear()
        self.started = True
        self._thread = threading.Thread(
            target=self._guarded_run, name="deskpet-tray", daemon=True
        )
        self._thread.start()
        return True

    def stop(self, timeout=2.0):
        """收掉托盘：先请消息循环退出，再等线程收尾。"""
        self._stop.set()
        if self._thread_id:
            try:
                self._shell["user32"].PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            except Exception:
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None
        self.started = False
        return True

    def _guarded_run(self):
        try:
            (self.loop or self._run)(self)
        except Exception as exc:
            self.failed = str(exc)
            self._warn("tray stopped with an error: %s", exc)
        finally:
            self.started = False
            self.hotkey_registered = False
            self.registered_hotkeys = {}

    # ---------- 菜单与命令 ----------
    def handle_command(self, command):
        """按菜单命令调宿主的方法。宿主没有这个方法或它报错，都不影响托盘。"""
        method = menu_map().get(int(command))
        if not method:
            return False
        self.last_command = method
        if callable(self.dispatch):
            try:
                return bool(self.dispatch(method))
            except Exception as exc:
                self._warn("tray command %s failed: %s", method, exc)
                return False
        action = getattr(self.owner, method, None)
        if not callable(action):
            self._warn("tray command %s has no handler", method)
            return False
        try:
            action()
        except Exception as exc:
            self._warn("tray command %s failed: %s", method, exc)
            return False
        return True

    def _on_hotkey(self, hotkey_id=None):
        """全局快捷键按注册 ID 分发。默认 ID 保留给聊天窗口。"""
        hotkey_id = self.HOTKEY_ID if hotkey_id is None else int(hotkey_id)
        action_name = self._hotkey_bindings().get(hotkey_id, "")
        if not action_name:
            return False
        self.last_command = action_name
        if callable(self.dispatch):
            try:
                return bool(self.dispatch(action_name))
            except Exception as exc:
                self._warn("hotkey action failed: %s", exc)
                return False
        action = getattr(self.owner, action_name, None)
        if not callable(action):
            return False
        try:
            action()
            return True
        except Exception as exc:
            self._warn("hotkey action failed: %s", exc)
            return False

    # ---------- Windows 消息循环 ----------
    def _run(self, tray):
        from ctypes import wintypes

        shell = self._shell
        user32, shell32, kernel32 = shell["user32"], shell["shell32"], shell["kernel32"]
        NOTIFYICONDATA = shell["NOTIFYICONDATA"]
        WNDPROC = ctypes.WINFUNCTYPE(
            ctypes.c_longlong, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
        )

        def wnd_proc(hwnd, message, wparam, lparam):
            if message == TRAY_CALLBACK:
                if lparam == WM_RBUTTONUP:
                    tray._popup(hwnd)
                elif lparam == WM_LBUTTONDBLCLK:
                    tray.handle_command(1)
            elif message == WM_COMMAND:
                tray.handle_command(int(wparam) & 0xFFFF)
            elif message == WM_HOTKEY:
                tray._on_hotkey(int(wparam))
            elif message == WM_DESTROY:
                user32.PostQuitMessage(0)
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)

        self._wndproc = WNDPROC(wnd_proc)
        self._thread_id = int(kernel32.GetCurrentThreadId())
        instance = kernel32.GetModuleHandleW(None)
        wc = shell["WNDCLASSW"]()
        wc.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p)
        wc.hInstance = instance
        wc.lpszClassName = self.WINDOW_CLASS
        try:
            user32.RegisterClassW(ctypes.byref(wc))
        except Exception:
            pass  # 已经注册过就继续用
        hwnd = user32.CreateWindowExW(
            0, self.WINDOW_CLASS, "Blossom", 0, 0, 0, 0, 0, None, None, instance, None
        )
        if not hwnd:
            raise RuntimeError("托盘窗口建不起来")
        self._hwnd = hwnd

        data = NOTIFYICONDATA()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATA)
        data.hWnd = hwnd
        data.uID = 1
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.uCallbackMessage = TRAY_CALLBACK
        data.hIcon = self._load_icon()
        data.szTip = self.tooltip
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)):
            raise RuntimeError("系统不接受托盘图标")

        self.hotkey_registered = self._register_hotkey()

        message = wintypes.MSG()
        try:
            while not self._stop.is_set():
                got = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if got in (0, -1):
                    break
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        finally:
            self._unregister_hotkeys(hwnd)
            self.hotkey_registered = False
            try:
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
            except Exception:
                pass
            try:
                user32.DestroyWindow(hwnd)
            except Exception:
                pass
            self._hwnd = 0

    def _popup(self, hwnd):
        """右键弹出菜单。菜单由系统画，选中的命令回到 handle_command。"""
        user32 = self._shell["user32"]
        menu = user32.CreatePopupMenu()
        if not menu:
            return False
        try:
            for command, label, _method in MENU:
                user32.AppendMenuW(menu, MF_STRING, command, label)
            user32.SetForegroundWindow(hwnd)
            picked = user32.TrackPopupMenu(
                menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, 0, 0, 0, hwnd, None
            )
            if picked:
                self.handle_command(picked)
            user32.PostMessageW(hwnd, WM_NULL, 0, 0)
            return True
        except Exception as exc:
            self._warn("tray menu failed: %s", exc)
            return False
        finally:
            try:
                user32.DestroyMenu(menu)
            except Exception:
                pass

    def _register_hotkey(self):
        """注册聊天和字幕快捷键；冲突的项单独记原因，不影响其他项。"""
        self._unregister_hotkeys(self._hwnd)
        self.registered_hotkeys = {}
        self.hotkey_errors = {}
        if not self._hwnd:
            return False
        user32 = self._shell["user32"]
        seen = set()
        chat_ok = False
        for hotkey_id, action_name, spec in self._hotkey_specs():
            mods, vk = parse_hotkey(spec)
            if not spec:
                continue
            if not (mods and vk):
                self.hotkey_errors[action_name] = "写法看不懂"
                continue
            combo = (mods, vk)
            if combo in seen:
                self.hotkey_errors[action_name] = "和其他快捷键重复"
                continue
            seen.add(combo)
            try:
                ok = bool(
                    user32.RegisterHotKey(self._hwnd, hotkey_id, mods, vk)
                )
            except Exception as exc:
                ok = False
                self._warn("hotkey registration failed: %s", exc)
            if ok:
                self.registered_hotkeys[hotkey_id] = action_name
                if hotkey_id == self.HOTKEY_ID:
                    chat_ok = True
            else:
                self.hotkey_errors[action_name] = "系统拒绝或已被其他程序占用"
        self.hotkey_registered = chat_ok
        return chat_ok or bool(self.registered_hotkeys)

    def _hotkey_specs(self):
        specs = [(self.HOTKEY_ID, "toggle_chat", self.hotkey)]
        for action_name, hotkey_id in EXTRA_HOTKEY_IDS.items():
            specs.append(
                (hotkey_id, action_name, self.extra_hotkeys.get(action_name, ""))
            )
        return specs

    def _hotkey_bindings(self):
        return {
            hotkey_id: action_name
            for hotkey_id, action_name, _spec in self._hotkey_specs()
        }

    def _unregister_hotkeys(self, hwnd=None):
        hwnd = hwnd or self._hwnd
        if not hwnd:
            return 0
        removed = 0
        user32 = self._shell["user32"]
        for hotkey_id, _action_name, _spec in self._hotkey_specs():
            try:
                if user32.UnregisterHotKey(hwnd, hotkey_id):
                    removed += 1
            except Exception:
                pass
        self.registered_hotkeys = {}
        return removed

    def _load_icon(self):
        """有图标文件就用文件，没有就用系统默认图标。"""
        shell = self._shell
        user32 = shell["user32"]
        path = Path(self.icon_path) if self.icon_path else None
        if path is not None and path.is_file():
            try:
                handle = user32.LoadImageW(
                    None, str(path), IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE
                )
                if handle:
                    return handle
            except Exception as exc:
                self._warn("tray icon load failed: %s", exc)
        try:
            return user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
        except Exception:
            return None

    def _warn(self, message, *args):
        if self.logger is None:
            return
        try:
            self.logger.warning(message, *args)
        except Exception:
            pass
