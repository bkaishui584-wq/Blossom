"""设备事件桥：把设备的真实状态转成 EventBus 事件。

渲染层、桌宠状态、任务面板都只订阅事件，谁也不直接问设备。这一层不调用模型，
也不改设备状态。
"""

from events.bus import DEVICE_EVENT

KIND_TASK = "device.task"
KIND_READING = "device.reading"
KIND_CONNECTION = "device.connection"
KIND_ERROR = "device.error"
KIND_ALERT = "device.alert"


class DeviceEventBridge:
    """设备 → EventBus 的单向桥。没有总线时静默降级，不影响设备本身的工作。"""

    def __init__(self, bus=None, logger=None, event_name=DEVICE_EVENT, history_limit=50):
        self.bus = bus
        self.logger = logger
        self.event_name = str(event_name or DEVICE_EVENT)
        self.history_limit = max(1, int(history_limit or 50))
        self.history = []

    def publish(self, kind, device="", payload=None, text=""):
        event = {
            "kind": str(kind or ""),
            "device": str(device or ""),
            "text": str(text or ""),
        }
        if isinstance(payload, dict):
            event.update(payload)
        self.history.append(event)
        if len(self.history) > self.history_limit:
            del self.history[: len(self.history) - self.history_limit]
        if self.bus is not None:
            try:
                self._send(event)
            except Exception as exc:
                self._warn("device event not published: %s", exc)
        return event

    def _send(self, event):
        """总线只认 Event 对象；能用 emit 就用 emit，两个入口都试一次。"""
        emit = getattr(self.bus, "emit", None)
        if callable(emit):
            return emit(self.event_name, **event)
        from events.bus import Event

        return self.bus.publish(Event(name=self.event_name, payload=dict(event)))

    def task_update(self, task):
        """任务状态变了。只带状态和结果，不带密钥与整条串口日志。"""
        row = task.to_dict() if hasattr(task, "to_dict") else dict(task or {})
        return self.publish(
            KIND_TASK,
            device=row.get("device", ""),
            payload={
                "task_id": row.get("id", ""),
                "action": row.get("action", ""),
                "status": row.get("status", ""),
                "error": row.get("error", ""),
                "result": row.get("result") or {},
            },
            text="%s %s" % (row.get("action", ""), row.get("status", "")),
        )

    def reading(self, device, values, note=""):
        return self.publish(
            KIND_READING, device=device, payload={"values": dict(values or {})}, text=str(note or "")
        )

    def connection(self, device, connected, detail=""):
        return self.publish(
            KIND_CONNECTION,
            device=device,
            payload={"connected": bool(connected)},
            text=detail or ("已连接" if connected else "已断开"),
        )

    def error(self, device, message):
        return self.publish(KIND_ERROR, device=device, text=str(message or ""))

    def alert(self, device, message, level="warn"):
        return self.publish(KIND_ALERT, device=device, payload={"level": str(level)}, text=str(message or ""))

    def recent(self, limit=20, kind=None):
        rows = [row for row in self.history if not kind or row.get("kind") == kind]
        return rows[-max(1, int(limit or 20)):]

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)