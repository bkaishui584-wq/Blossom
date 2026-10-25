"""环境感知层：让桌宠知道“现在环境发生了什么”。

第一阶段只做低成本读取：电量、是否插着电源、内存占用。
只读状态，不读窗口内容，不发任何网络请求，不把读数发给模型。

低电量 -> BATTERY_LOW 事件 -> 调度器规则 -> 桌宠提醒（不需要模型）
插上电源或电量回升 -> BATTERY_OK 事件

读不到就当作“不知道”，保持安静，绝不让桌宠崩。
"""

import ctypes
import os
from abc import ABC, abstractmethod

from events import bus as bus_module

DEFAULT_LOW_BATTERY = 20

_BATTERY_UNKNOWN = 255


class _PowerStatus(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_byte),
        ("BatteryFlag", ctypes.c_byte),
        ("BatteryLifePercent", ctypes.c_byte),
        ("SystemStatusFlag", ctypes.c_byte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("dwTotalPhys", ctypes.c_size_t),
        ("dwAvailPhys", ctypes.c_size_t),
        ("dwTotalPageFile", ctypes.c_size_t),
        ("dwAvailPageFile", ctypes.c_size_t),
        ("dwTotalVirtual", ctypes.c_size_t),
        ("dwAvailVirtual", ctypes.c_size_t),
        ("dwAvailExtendedVirtual", ctypes.c_size_t),
    ]


class EnvironmentProvider(ABC):
    """环境读数来源。换平台或换实现都不需要改 Agent Core。"""

    name = "base"

    @abstractmethod
    def is_available(self):
        """能不能读到环境状态。"""

    @abstractmethod
    def snapshot(self):
        """返回一次环境读数，读不到的项给 None。"""


class NullEnvironmentProvider(EnvironmentProvider):
    """没有接环境感知时的默认实现：永远读不到，桌宠照常运行。"""

    name = "null"

    def is_available(self):
        return False

    def snapshot(self):
        return {}

    def describe(self):
        return {"provider": self.name, "available": False, "note": "没有接入环境感知"}


class WindowsEnvironmentProvider(EnvironmentProvider):
    """Windows 的低成本环境读数。只查状态，不做任何网络请求。"""

    name = "windows"

    def __init__(self):
        self._kernel32 = None

    def is_available(self):
        return self._api() is not None

    def snapshot(self):
        power = self._power()
        memory = self._memory()
        return {
            "battery_percent": _battery_percent(power),
            "on_power": _on_power(power),
            "memory_percent": None if memory is None else int(memory.dwMemoryLoad),
        }

    def describe(self):
        return {
            "provider": self.name,
            "available": self.is_available(),
            "note": "只读电量与内存占用，不读窗口内容，也不联网",
        }

    # ---------- 内部 ----------
    def _power(self):
        kernel32 = self._api()
        if kernel32 is None:
            return None
        status = _PowerStatus()
        try:
            if not kernel32.GetSystemPowerStatus(ctypes.byref(status)):
                return None
        except Exception:
            return None
        return status

    def _memory(self):
        kernel32 = self._api()
        if kernel32 is None:
            return None
        status = _MemoryStatus()
        status.dwLength = ctypes.sizeof(_MemoryStatus)
        try:
            if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return None
        except Exception:
            return None
        return status

    def _api(self):
        if os.name != "nt":
            return None
        if self._kernel32 is None:
            try:
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.GetSystemPowerStatus.argtypes = [ctypes.POINTER(_PowerStatus)]
                kernel32.GetSystemPowerStatus.restype = ctypes.c_bool
                kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MemoryStatus)]
                kernel32.GlobalMemoryStatusEx.restype = ctypes.c_bool
            except Exception:
                return None
            self._kernel32 = kernel32
        return self._kernel32


def _battery_percent(power):
    if power is None or power.BatteryLifePercent == _BATTERY_UNKNOWN:
        return None
    return max(0, min(100, int(power.BatteryLifePercent)))


def _on_power(power):
    if power is None or power.ACLineStatus == _BATTERY_UNKNOWN:
        return None
    return power.ACLineStatus == 1


class EnvironmentWatcher:
    """按固定节奏读一次环境状态，只在变化时发事件，不做判断也不问模型。"""

    def __init__(self, provider=None, bus=None, logger=None, low_battery_percent=DEFAULT_LOW_BATTERY):
        self.provider = provider or NullEnvironmentProvider()
        self.bus = bus
        self.logger = logger
        self.low_battery_percent = int(low_battery_percent)
        self.reading = {}
        self.low_battery = False
        self.low_count = 0
        self.ok_count = 0

    # ---------- 状态 ----------
    def available(self):
        try:
            return bool(self.provider.is_available())
        except Exception as exc:
            self._warn("environment provider probe failed: %s", exc)
            return False

    def snapshot(self):
        try:
            reading = self.provider.snapshot() or {}
        except Exception as exc:
            self._warn("environment read failed: %s", exc)
            return {}
        return dict(reading)

    # ---------- 走一拍 ----------
    def update(self):
        """读一次环境状态，返回本次发出的事件名；没有变化返回空字符串。"""
        self.reading = self.snapshot()
        low = self._is_low(self.reading)
        if low is None:
            # 读不到电量就当不知道，保持之前的状态，不乱喊。
            return ""
        if low and not self.low_battery:
            self.low_battery = True
            self.low_count += 1
            self._publish(bus_module.BATTERY_LOW)
            return bus_module.BATTERY_LOW
        if self.low_battery and not low:
            self.low_battery = False
            self.ok_count += 1
            self._publish(bus_module.BATTERY_OK)
            return bus_module.BATTERY_OK
        return ""

    def describe(self):
        """只给开发者面板看，绝不发给模型。"""
        return {
            "provider": getattr(self.provider, "name", "?"),
            "available": self.available(),
            "low_battery": self.low_battery,
            "low_battery_percent": self.low_battery_percent,
            "low_count": self.low_count,
            "ok_count": self.ok_count,
            **self.reading,
        }

    # ---------- 内部 ----------
    def _is_low(self, reading):
        percent = reading.get("battery_percent")
        if percent is None:
            return None
        if reading.get("on_power"):
            # 正插着电源就不提醒，免得刚插上就喊没电。
            return False
        return int(percent) <= self.low_battery_percent

    def _publish(self, event_name):
        if self.bus is None:
            return
        payload = {
            key: self.reading.get(key)
            for key in ("battery_percent", "on_power", "memory_percent")
        }
        try:
            self.bus.emit(event_name, **payload)
        except Exception as exc:
            self._warn("environment publish failed: %s", exc)

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)
