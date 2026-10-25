"""屏幕感知：只读屏幕有多少块、分辨率多大、虚拟桌面范围。

不截图、不读像素、不做 OCR、不碰摄像头。只问 Windows 要几个数字，
所以成本极低，也不涉及任何隐私内容。

屏幕布局变化（拔掉显示器、改分辨率、插投影）-> SCREEN_CHANGED
-> 桌宠重新校准自己的位置，不会被留在看不见的地方。

换平台或换实现只改 provider，界面与 Agent Core 都不用动。
"""

import ctypes
import os
from abc import ABC, abstractmethod

from events import bus as bus_module

SM_CXSCREEN = 0
SM_CYSCREEN = 1
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
SM_CMONITORS = 80


class ScreenProvider(ABC):
    """屏幕布局读数来源。只给数字，不给画面。"""

    name = "base"

    @abstractmethod
    def is_available(self):
        """能不能读到屏幕布局。"""

    @abstractmethod
    def layout(self):
        """返回 {"count", "primary": {...}, "virtual": {...}}。"""


class NullScreenProvider(ScreenProvider):
    """没有屏幕信息来源时的默认实现：读不到，也不影响运行。"""

    name = "null"

    def is_available(self):
        return False

    def layout(self):
        return {}

    def describe(self):
        return {"provider": self.name, "available": False, "note": "没有接入屏幕感知"}


class WindowsScreenProvider(ScreenProvider):
    """Windows 屏幕布局。只查数字，不读画面。"""

    name = "windows"

    def __init__(self):
        self._user32 = None

    def is_available(self):
        return self._api() is not None

    def layout(self):
        user32 = self._api()
        if user32 is None:
            return {}
        try:
            get = user32.GetSystemMetrics
            return {
                "count": int(get(SM_CMONITORS)),
                "primary": {"width": int(get(SM_CXSCREEN)), "height": int(get(SM_CYSCREEN))},
                "virtual": {
                    "left": int(get(SM_XVIRTUALSCREEN)),
                    "top": int(get(SM_YVIRTUALSCREEN)),
                    "width": int(get(SM_CXVIRTUALSCREEN)),
                    "height": int(get(SM_CYVIRTUALSCREEN)),
                },
            }
        except Exception:
            return {}

    def describe(self):
        return {
            "provider": self.name,
            "available": self.is_available(),
            "note": "只读屏幕数量与分辨率，不截图也不看内容",
        }

    # ---------- 内部 ----------
    def _api(self):
        if os.name != "nt":
            return None
        if self._user32 is None:
            try:
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.GetSystemMetrics.argtypes = [ctypes.c_int]
                user32.GetSystemMetrics.restype = ctypes.c_int
            except Exception:
                return None
            self._user32 = user32
        return self._user32


class ScreenWatcher:
    """按固定节奏看一眼屏幕布局，只在变化时发事件，不做判断也不问模型。"""

    def __init__(self, provider=None, bus=None, logger=None):
        self.provider = provider or NullScreenProvider()
        self.bus = bus
        self.logger = logger
        self.layout = {}
        self.changes = 0

    # ---------- 状态 ----------
    def available(self):
        try:
            return bool(self.provider.is_available())
        except Exception as exc:
            self._warn("screen provider probe failed: %s", exc)
            return False

    def signature(self):
        layout = self.layout or {}
        virtual = layout.get("virtual") or {}
        return (
            layout.get("count"),
            virtual.get("left"),
            virtual.get("top"),
            virtual.get("width"),
            virtual.get("height"),
        )

    # ---------- 走一拍 ----------
    def update(self):
        """读一次屏幕布局，变化时返回 SCREEN_CHANGED，否则返回空字符串。"""
        try:
            layout = dict(self.provider.layout() or {})
        except Exception as exc:
            self._warn("screen read failed: %s", exc)
            return ""
        if not layout:
            # 读不到就当没变化，不动用户的位置。
            return ""
        before = self.signature()
        self.layout = layout
        if before == self.signature():
            return ""
        self.changes += 1
        if self.bus is not None:
            try:
                self.bus.emit(bus_module.SCREEN_CHANGED, **layout)
            except Exception as exc:
                self._warn("screen publish failed: %s", exc)
        return bus_module.SCREEN_CHANGED

    def describe(self):
        """只给开发者面板看，绝不发给模型。"""
        layout = self.layout or {}
        return {
            "provider": getattr(self.provider, "name", "?"),
            "available": self.available(),
            "count": layout.get("count"),
            "primary": layout.get("primary"),
            "changes": self.changes,
        }

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)
