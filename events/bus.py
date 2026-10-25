"""内部事件系统。用于解耦调度器、工具、设备与 Agent。"""

import time
from dataclasses import dataclass, field

# 事件名约定
USER_MESSAGE = "user.message"
TIMER_TICK = "timer.tick"
TOOL_COMPLETED = "tool.completed"
DEVICE_EVENT = "device.event"
SYSTEM_EVENT = "system.event"
COMPANION = "companion.proactive"
BEHAVIOR = "behavior.action"
USER_IDLE = "user.idle"
USER_RETURNED = "user.returned"
FULLSCREEN_ENTERED = "window.fullscreen.entered"
FULLSCREEN_EXITED = "window.fullscreen.exited"
BATTERY_LOW = "environment.battery.low"
BATTERY_OK = "environment.battery.ok"
SCREEN_CHANGED = "screen.changed"
VISION_DETECTED = "vision.detected"
VISION_CLEARED = "vision.cleared"
LIFE_STATE = "life.state"
TODO_DUE = "todo.due"
REMINDER = "reminder.due"

WILDCARD = "*"


@dataclass
class Event:
    name: str
    payload: dict = field(default_factory=dict)
    created_at: float = 0.0

    def to_dict(self):
        return {"name": self.name, "payload": dict(self.payload), "created_at": self.created_at}


class EventBus:
    """订阅发布。单个订阅者出错不影响其他订阅者。"""

    def __init__(self, logger=None, history_limit=50):
        self.logger = logger
        self.history_limit = history_limit
        self._handlers = {}
        self._history = []

    def subscribe(self, name, handler):
        handlers = self._handlers.setdefault(name, [])
        if handler not in handlers:
            handlers.append(handler)

        def unsubscribe():
            self.unsubscribe(name, handler)

        return unsubscribe

    def unsubscribe(self, name, handler):
        handlers = self._handlers.get(name)
        if handlers and handler in handlers:
            handlers.remove(handler)
            return True
        return False

    def publish(self, event):
        if not event.created_at:
            event.created_at = time.time()
        self._history.append(event)
        if len(self._history) > self.history_limit:
            self._history.pop(0)

        handlers = list(self._handlers.get(event.name, [])) + list(
            self._handlers.get(WILDCARD, [])
        )
        results = []
        for handler in handlers:
            try:
                results.append(handler(event))
            except Exception as exc:
                if self.logger:
                    self.logger.warning("event handler failed for %s: %s", event.name, exc)
                results.append({"error": str(exc)})
        return results

    def emit(self, event_name, **payload):
        # 参数不能叫 name：payload 里本来就有 name（比如工具名），会撞车。
        event = Event(name=event_name, payload=payload)
        self.publish(event)
        return event

    def recent(self, limit=20):
        return [event.to_dict() for event in self._history[-limit:]]

    def clear(self):
        self._history.clear()
