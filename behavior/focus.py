"""专注模式。用户全屏打游戏、看视频、放 PPT 时，桌宠自动降低存在感。

只看 Windows 窗口状态（前台窗口是不是铺满整块屏幕），不做视觉识别，
不读窗口内容，不联网，也不经过模型。

链路：
    全屏够久 -> FULLSCREEN_ENTERED -> 角色变淡、收起聊天浮层、暂停待机小动作
    退出全屏 -> FULLSCREEN_EXITED -> 一切恢复原样

之所以要连续几次都读到全屏才切换，是为了避免窗口一闪就跟着闪。
读不到窗口状态时保持原样，桌宠照常运作。
"""

import ctypes
import os

from behavior.manager import NullWindowProvider, WindowProvider
from events import bus as bus_module

DEFAULT_ENTER_AFTER = 2
DEFAULT_EXIT_AFTER = 2

# 系统自带的壳窗口，它们本来就铺满屏幕，不算用户全屏。
SHELL_CLASSES = ("Progman", "WorkerW", "Shell_TrayWnd", "TaskListThumbnailWnd")

MONITOR_DEFAULTTONEAREST = 2


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", _Rect),
        ("rcWork", _Rect),
        ("dwFlags", ctypes.c_ulong),
    ]


class WindowsWindowProvider(WindowProvider):
    """Windows 前台窗口全屏检测。只看窗口状态，不读窗口内容。"""

    name = "windows"

    def __init__(self):
        self._user32 = None

    def is_available(self):
        return self._api() is not None

    def is_busy(self, now=None):
        """全屏就是忙：这种时候不该主动开口。"""
        return self.is_fullscreen()

    def is_fullscreen(self):
        user32 = self._api()
        if user32 is None:
            return False
        try:
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return False
            if self._class_name(user32, hwnd) in SHELL_CLASSES:
                return False
            if user32.IsIconic(hwnd):
                return False
            rect = _Rect()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return False
            if rect.right <= rect.left or rect.bottom <= rect.top:
                return False
            monitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
            if not monitor:
                return False
            info = _MonitorInfo()
            info.cbSize = ctypes.sizeof(_MonitorInfo)
            if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                return False
            screen = info.rcMonitor
        except Exception:
            return False
        return (
            rect.left <= screen.left
            and rect.top <= screen.top
            and rect.right >= screen.right
            and rect.bottom >= screen.bottom
        )

    def describe(self):
        return {
            "provider": self.name,
            "available": self.is_available(),
            "fullscreen": self.is_fullscreen(),
            "note": "只看窗口是不是全屏，不读窗口内容",
        }

    # ---------- 内部 ----------
    def _api(self):
        if os.name != "nt":
            return None
        if self._user32 is None:
            try:
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.GetForegroundWindow.restype = ctypes.c_void_p
                user32.IsIconic.argtypes = [ctypes.c_void_p]
                user32.IsIconic.restype = ctypes.c_bool
                user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Rect)]
                user32.GetWindowRect.restype = ctypes.c_bool
                user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
                user32.MonitorFromWindow.restype = ctypes.c_void_p
                user32.GetMonitorInfoW.argtypes = [
                    ctypes.c_void_p,
                    ctypes.POINTER(_MonitorInfo),
                ]
                user32.GetMonitorInfoW.restype = ctypes.c_bool
                user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
                user32.GetClassNameW.restype = ctypes.c_int
            except Exception:
                return None
            self._user32 = user32
        return self._user32

    def _class_name(self, user32, hwnd):
        buf = ctypes.create_unicode_buffer(256)
        try:
            if user32.GetClassNameW(hwnd, buf, 256):
                return buf.value
        except Exception:
            pass
        return ""


class FocusMode:
    """专注模式状态机。全屏够久才进，退出也同理，避免来回切。"""

    def __init__(
        self,
        provider=None,
        bus=None,
        logger=None,
        enter_after=DEFAULT_ENTER_AFTER,
        exit_after=DEFAULT_EXIT_AFTER,
        on_enter=None,
        on_exit=None,
    ):
        self.provider = provider or NullWindowProvider()
        self.bus = bus
        self.logger = logger
        self.enter_after = max(1, int(enter_after))
        self.exit_after = max(1, int(exit_after))
        self.on_enter = on_enter
        self.on_exit = on_exit
        self.active = False
        self.fullscreen = False
        self.enter_count = 0
        self.exit_count = 0
        self._hits = 0

    # ---------- 状态 ----------
    def available(self):
        try:
            return bool(self.provider.is_available())
        except Exception as exc:
            self._warn("window provider probe failed: %s", exc)
            return False

    # ---------- 看一次 ----------
    def update(self, now=None):
        """返回 FULLSCREEN_ENTERED / FULLSCREEN_EXITED / 空串。不调模型。"""
        self.fullscreen = self._read()
        if self.active:
            if self.fullscreen:
                self._hits = 0
                return ""
            self._hits += 1
            if self._hits < self.exit_after:
                return ""
            self._hits = 0
            self.active = False
            self.exit_count += 1
            self._notify(self.on_exit, bus_module.FULLSCREEN_EXITED)
            return bus_module.FULLSCREEN_EXITED
        if not self.fullscreen:
            self._hits = 0
            return ""
        self._hits += 1
        if self._hits < self.enter_after:
            return ""
        self._hits = 0
        self.active = True
        self.enter_count += 1
        self._notify(self.on_enter, bus_module.FULLSCREEN_ENTERED)
        return bus_module.FULLSCREEN_ENTERED

    def describe(self):
        """开发者面板用的只读视图。绝不发给模型。"""
        return {
            "provider": getattr(self.provider, "name", "?"),
            "available": self.available(),
            "active": self.active,
            "fullscreen": self.fullscreen,
            "enter_count": self.enter_count,
            "exit_count": self.exit_count,
        }

    # ---------- 内部 ----------
    def _read(self):
        try:
            return bool(self.provider.is_busy())
        except Exception as exc:
            self._warn("fullscreen probe failed: %s", exc)
            return False

    def _notify(self, callback, event_name):
        if self.bus is not None:
            try:
                self.bus.emit(event_name)
            except Exception as exc:
                self._warn("focus publish failed: %s", exc)
        if callback is None:
            return
        try:
            callback()
        except Exception as exc:
            self._warn("focus callback failed: %s", exc)

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)
