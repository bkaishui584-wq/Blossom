"""实体身体：把桌宠当前状态映射成外部设备动作。

数据流：
    情绪 / 行为 / 视觉事件 -> PhysicalBody -> DeviceManager -> DeviceAdapter -> 设备

当前所有动作都经过 DeviceManager，模型不能直接碰设备；
真实硬件在本阶段不允许注册（DeviceManager 默认拒绝），
所以现在最坏情况也只是往模拟设备里写一条内存指令。

没有连上设备时如实返回失败，不编造动作成功。
"""

import time

from events import bus as bus_module

# 每种情绪对应一个设备动作。动作内容由程序定，模型改不了。
SIGNALS = {
    "neutral": {"cmd": "servo", "angle": 90},
    "happy": {"cmd": "led", "on": True},
    "excited": {"cmd": "move", "direction": "forward", "speed": 40},
    "sad": {"cmd": "servo", "angle": 30},
    "angry": {"cmd": "servo", "angle": 150},
    "tired": {"cmd": "led", "on": False},
    "sleepy": {"cmd": "led", "on": False},
    "thinking": {"cmd": "servo", "angle": 90},
    "curious": {"cmd": "servo", "angle": 120},
    "calm": {"cmd": "servo", "angle": 90},
}

# 只有这些行为会动身体。小动作（眨眼、呼吸）不打扰设备。
REACTIVE_KINDS = ("speak", "interact", "remind", "sleep")

# 看到什么会有反应。看不到之后回到中立。
VISION_REACTIONS = {
    "person": "happy",
    "cat": "excited",
    "dog": "excited",
    "cup": "curious",
    "book": "curious",
}


class PhysicalBody:
    """桌宠的实体身体。只说话给 DeviceManager，自己不碰硬件。"""

    def __init__(self, devices=None, logger=None, name="", enabled=True, clock=time.time):
        self.devices = devices
        self.logger = logger
        self.name = str(name or "")
        self.enabled = bool(enabled)
        self.clock = clock
        self.sent_count = 0
        self.failed_count = 0
        self.skipped_count = 0
        self.last_signal = None
        self.last_result = None
        self.last_at = None

    # ---------- 订阅 ----------
    def attach(self, bus):
        """订阅行为和视觉事件。返回退订函数列表。"""
        if bus is None:
            return []
        handles = []
        for event_name, handler in (
            (bus_module.BEHAVIOR, self.on_behavior),
            (bus_module.VISION_DETECTED, self.on_detection),
            (bus_module.VISION_CLEARED, self.on_cleared),
        ):
            try:
                handles.append(bus.subscribe(event_name, handler))
            except Exception as exc:
                self._warn("body subscribe failed for %s: %s", event_name, exc)
        return handles

    def on_behavior(self, event):
        payload = _payload(event)
        kind = str(payload.get("kind") or "")
        if kind not in REACTIVE_KINDS:
            return None
        emotion = str(payload.get("emotion") or "")
        if not emotion:
            return None
        return self.express(emotion, reason=str(payload.get("reason") or kind))

    def on_detection(self, event):
        payload = _payload(event)
        label = str(payload.get("label") or "").strip().lower()
        emotion = VISION_REACTIONS.get(label)
        if not emotion:
            return None
        return self.express(emotion, reason=f"看到{label}")

    def on_cleared(self, event):
        return self.express("neutral", reason="看不到了")

    # ---------- 动作 ----------
    def available(self):
        return self._device_name() != ""

    def express(self, emotion, reason=""):
        """按情绪做一个设备动作，返回真实的执行结果。"""
        signal = SIGNALS.get(str(emotion or "").strip().lower())
        if not self.enabled:
            return self._skip(emotion, "实体身体没有打开")
        if signal is None:
            return self._skip(emotion, f"没有给 {emotion} 准备动作")
        name = self._device_name()
        if not name:
            return self._skip(emotion, "现在没有连上实体设备")
        if not self._connected(name):
            return self._skip(emotion, "实体设备没有连上")
        try:
            result = self.devices.execute(name, dict(signal))
        except Exception as exc:
            return self._skip(emotion, f"设备执行失败：{exc}")
        self.last_at = self.clock()
        self.last_signal = {
            "emotion": str(emotion),
            "reason": str(reason),
            "device": name,
            "command": dict(signal),
        }
        self.last_result = dict(result) if isinstance(result, dict) else {}
        if self.last_result.get("ok"):
            self.sent_count += 1
        else:
            self.failed_count += 1
        return self.last_result

    def describe(self):
        """只给开发者面板看，不发给模型。"""
        return {
            "device": self._device_name() or None,
            "enabled": self.enabled,
            "emotions": sorted(SIGNALS),
            "sent": self.sent_count,
            "failed": self.failed_count,
            "skipped": self.skipped_count,
            "last_signal": dict(self.last_signal) if self.last_signal else None,
        }

    # ---------- 内部 ----------
    def _device_name(self):
        if self.devices is None:
            return ""
        if self.name:
            return self.name if self.devices.get(self.name) is not None else ""
        try:
            names = list(self.devices.names())
        except Exception:
            return ""
        return names[0] if names else ""

    def _connected(self, name):
        adapter = self.devices.get(name) if self.devices else None
        if adapter is None:
            return False
        if getattr(adapter, "connected", False):
            return True
        # 模拟设备直接连上；真实设备本来就不允许注册。
        result = self.devices.connect(name)
        return bool(isinstance(result, dict) and result.get("ok"))

    def _skip(self, emotion, error):
        self.skipped_count += 1
        self.last_signal = None
        self.last_result = {"ok": False, "error": error, "emotion": str(emotion)}
        return self.last_result

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)


def _payload(event):
    payload = getattr(event, "payload", event)
    return payload if isinstance(payload, dict) else {}
