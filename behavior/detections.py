"""视觉检测事件层（YOLO 这一类事件）。当前只做事件管道，不接真实模型。

        检测来源 -> DetectionWatcher -> VISION_DETECTED / VISION_CLEARED -> EventBus

当前阶段的安全边界：
    不保存任何画面，不把画面或数据发给模型，只把标签与置信度变成事件。
    只有白名单里的标签会发出去，其他一律丢弃。
    没有检测来源时保持安静，绝不编造「我看到什么」。
    检测到了也不主动说话，只进事件总线，避免打扰。

以后接 YOLO 或摄像头：只加一个 DetectionProvider 实现，
界面、调度器与 Agent Core 都不用改。
"""

import time
from abc import ABC, abstractmethod

from events import bus as bus_module

DEFAULT_LABELS = ("person", "cup", "cat", "dog", "book")
DEFAULT_MIN_CONFIDENCE = 0.5
CLEAR_AFTER_MISSES = 2


class DetectionProvider(ABC):
    """检测来源。只给标签与置信度，不给画面。"""

    name = "base"
    is_mock = False

    @abstractmethod
    def is_available(self):
        """能不能做检测。"""

    @abstractmethod
    def detect(self):
        """返回 {"ok", "detections": [{"label", "confidence"}], "error"}。"""


class NullDetectionProvider(DetectionProvider):
    """没有接视觉检测时的默认实现：什么也看不到，也不编造。"""

    name = "null"

    def is_available(self):
        return False

    def detect(self):
        return {"ok": False, "detections": [], "error": "还没有接视觉检测"}

    def describe(self):
        return {"provider": self.name, "available": False, "note": "没有接入视觉检测"}


class MockDetectionProvider(DetectionProvider):
    """内存里的假检测，只用来验证事件管道，不读摄像头。"""

    name = "mock"
    is_mock = True

    def __init__(self, detections=()):
        self.scripted = [dict(item) for item in detections]
        self.calls = 0

    def is_available(self):
        return True

    def detect(self):
        self.calls += 1
        return {"ok": True, "detections": [dict(item) for item in self.scripted], "error": ""}

    def describe(self):
        return {
            "provider": self.name,
            "available": True,
            "note": "模拟检测，不读摄像头，也不训练模型",
        }


class DetectionWatcher:
    """按固定节奏看一次检测结果，只在出现 / 消失时发事件，不做判断也不问模型。"""

    def __init__(
        self,
        provider=None,
        bus=None,
        logger=None,
        labels=DEFAULT_LABELS,
        min_confidence=DEFAULT_MIN_CONFIDENCE,
        clock=time.time,
    ):
        self.provider = provider or NullDetectionProvider()
        self.bus = bus
        self.logger = logger
        self.labels = tuple(str(label) for label in labels)
        self.min_confidence = float(min_confidence)
        self.clock = clock
        self.visible = {}
        self.misses = {}
        self.detected_count = 0
        self.cleared_count = 0
        self.dropped_count = 0
        self.last_at = None

    # ---------- 状态 ----------
    def available(self):
        try:
            return bool(self.provider.is_available())
        except Exception as exc:
            self._warn("detection provider probe failed: %s", exc)
            return False

    # ---------- 走一拍 ----------
    def update(self):
        """看一次检测结果，返回本次发出的事件名列表。"""
        try:
            result = self.provider.detect() or {}
        except Exception as exc:
            self._warn("detection read failed: %s", exc)
            return []
        if not result.get("ok"):
            return []
        self.last_at = self.clock()
        found = self._filter(result.get("detections") or [])
        events = []
        for label, confidence in found.items():
            if label in self.visible:
                self.visible[label] = confidence
                self.misses[label] = 0
                continue
            self.visible[label] = confidence
            self.misses[label] = 0
            self.detected_count += 1
            events.append(self._publish(bus_module.VISION_DETECTED, label, confidence))
        for label in list(self.visible):
            if label in found:
                continue
            self.misses[label] = self.misses.get(label, 0) + 1
            if self.misses[label] < CLEAR_AFTER_MISSES:
                continue
            confidence = self.visible.pop(label)
            self.misses.pop(label, None)
            self.cleared_count += 1
            events.append(self._publish(bus_module.VISION_CLEARED, label, confidence))
        return [name for name in events if name]

    def describe(self):
        """只给开发者面板看，绝不发给模型。"""
        return {
            "provider": getattr(self.provider, "name", "?"),
            "available": self.available(),
            "visible": sorted(self.visible),
            "detected_count": self.detected_count,
            "cleared_count": self.cleared_count,
            "dropped_count": self.dropped_count,
            "min_confidence": self.min_confidence,
        }

    # ---------- 内部 ----------
    def _filter(self, detections):
        """只保留白名单里、置信度够高的标签；其余计数丢弃。"""
        found = {}
        for item in detections:
            if not isinstance(item, dict):
                self.dropped_count += 1
                continue
            label = str(item.get("label") or "").strip().lower()
            if not label or label not in self.labels:
                self.dropped_count += 1
                continue
            try:
                confidence = float(item.get("confidence"))
            except (TypeError, ValueError):
                self.dropped_count += 1
                continue
            if confidence < self.min_confidence:
                self.dropped_count += 1
                continue
            found[label] = max(confidence, found.get(label, 0.0))
        return found

    def _publish(self, event_name, label, confidence):
        if self.bus is None:
            return ""
        try:
            self.bus.emit(event_name, label=label, confidence=round(float(confidence), 3))
        except Exception as exc:
            self._warn("detection publish failed: %s", exc)
            return ""
        return event_name

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)
