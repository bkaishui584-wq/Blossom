"""用户活动感知。判断用户还在不在电脑前，不读取任何屏幕内容。

链路：
    用户 40 分钟没动键盘鼠标 -> USER_IDLE 事件   -> 行为层停下待机小动作
    用户又回来操作           -> USER_RETURNED 事件 -> 行为层恢复互动节奏

只用系统自带的「最后一次操作距今多久」，不采集按键、不读窗口内容、不联网。
读不到时保持安静，当作用户一直在，不影响别的功能。
以后换平台或换传感器，实现 idle_seconds 就行，行为层与 Agent Core 都不用改。
"""

import ctypes
import os
import time
from abc import ABC, abstractmethod

from events import bus as bus_module

DEFAULT_IDLE_AFTER = 2400.0
DEFAULT_RETURNED_AFTER = 5.0


class _LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


class UserActivityProvider(ABC):
    """用户活动接入点。以后换平台或换传感器，实现这两个方法就行。"""

    name = "base"

    @abstractmethod
    def is_available(self):
        """现在能不能读到用户活动。"""

    @abstractmethod
    def idle_seconds(self):
        """距离用户上一次操作键盘鼠标过了多少秒。"""


class NullUserActivityProvider(UserActivityProvider):
    """读不到用户活动时的默认实现：永远当作刚操作过，永远不会判定离开。"""

    name = "null"

    def is_available(self):
        return False

    def idle_seconds(self):
        return 0.0

    def describe(self):
        return {
            "provider": self.name,
            "available": False,
            "note": "没有接入用户活动感知",
        }


class WindowsUserActivityProvider(UserActivityProvider):
    """Windows 的空闲时间。只问「上一次操作距今多久」，不读输入内容。"""

    name = "windows"

    def __init__(self):
        self._user32 = None
        self._kernel32 = None

    def is_available(self):
        return self._api() is not None

    def idle_seconds(self):
        api = self._api()
        if api is None:
            return 0.0
        user32, kernel32 = api
        info = _LastInputInfo()
        info.cbSize = ctypes.sizeof(_LastInputInfo)
        try:
            if not user32.GetLastInputInfo(ctypes.byref(info)):
                return 0.0
            ticks = kernel32.GetTickCount()
        except Exception:
            return 0.0
        return max(0.0, ((ticks - info.dwTime) & 0xFFFFFFFF) / 1000.0)

    def describe(self):
        return {
            "provider": self.name,
            "available": self.is_available(),
            "note": "只读空闲时间，不读按键内容",
        }

    def _api(self):
        if os.name != "nt":
            return None
        if self._user32 is None:
            try:
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                user32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LastInputInfo)]
                user32.GetLastInputInfo.restype = ctypes.c_bool
                kernel32.GetTickCount.restype = ctypes.c_uint32
            except Exception:
                return None
            self._user32 = user32
            self._kernel32 = kernel32
        return self._user32, self._kernel32


class ActivityWatcher:
    """把「用户还在不在」变成事件。程序判断，不经过模型。"""

    def __init__(
        self,
        provider=None,
        bus=None,
        logger=None,
        idle_after=DEFAULT_IDLE_AFTER,
        returned_after=DEFAULT_RETURNED_AFTER,
        clock=time.time,
        on_idle=None,
        on_return=None,
    ):
        self.provider = provider or NullUserActivityProvider()
        self.bus = bus
        self.logger = logger
        self.idle_after = float(idle_after)
        self.returned_after = float(returned_after)
        self.clock = clock
        self.on_idle = on_idle
        self.on_return = on_return
        self.away = False
        self.away_count = 0
        self.return_count = 0
        self.last_idle_seconds = 0.0

    # ---------- 状态 ----------
    def available(self):
        try:
            return bool(self.provider.is_available())
        except Exception as exc:
            self._warn("activity provider probe failed: %s", exc)
            return False

    def idle_seconds(self):
        try:
            return max(0.0, float(self.provider.idle_seconds()))
        except Exception as exc:
            self._warn("activity read failed: %s", exc)
            return 0.0

    # ---------- 看一次 ----------
    def check(self, now=None):
        """返回 USER_IDLE / USER_RETURNED / 空串。闲时不调模型。"""
        seconds = self.idle_seconds()
        self.last_idle_seconds = seconds
        if self.away:
            if seconds <= self.returned_after:
                self.away = False
                self.return_count += 1
                self._notify(self.on_return, bus_module.USER_RETURNED, seconds)
                return bus_module.USER_RETURNED
            return ""
        if seconds >= self.idle_after:
            self.away = True
            self.away_count += 1
            self._notify(self.on_idle, bus_module.USER_IDLE, seconds)
            return bus_module.USER_IDLE
        return ""

    def describe(self):
        """开发者面板用的只读视图。绝不发给模型。"""
        return {
            "provider": getattr(self.provider, "name", "?"),
            "available": self.available(),
            "idle_seconds": self.last_idle_seconds,
            "away": self.away,
            "idle_after": self.idle_after,
            "away_count": self.away_count,
            "return_count": self.return_count,
        }

    # ---------- 内部 ----------
    def _notify(self, callback, event_name, seconds):
        if self.bus is not None:
            try:
                self.bus.emit(event_name, idle_seconds=seconds)
            except Exception as exc:
                self._warn("activity publish failed: %s", exc)
        if callback is None:
            return
        try:
            callback(seconds)
        except Exception as exc:
            self._warn("activity callback failed: %s", exc)

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)
