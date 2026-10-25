"""设备任务管理：排队、取消、超时、记录。

一条任务只有在收到设备自己的确认之后才算完成；只把报文发出去不算。失败不自动
重试：设备那边出了状况，先停下来说清楚，比悄悄再发一遍安全。

取消是尽力而为：已经发出去的动作没法凭空收回，如实标注「设备可能已经做完」。
紧急停止只有固件真的实现了 device.stop 才算数，桌面界面不单独承担这件事。
"""

import queue
import threading
import time
import uuid
from dataclasses import dataclass, field

from devices import protocol
from devices.bridge import DeviceEventBridge

STATUS_QUEUED = "queued"
TASK_STATUSES = (STATUS_QUEUED, "running") + protocol.STATUSES

TASK_LABELS = {
    STATUS_QUEUED: "排队中",
    "running": "执行中",
}
TASK_LABELS.update(protocol.STATUS_LABELS)

MAX_RETRIES = 1


@dataclass
class DeviceTask:
    """一条设备动作任务。字段就是界面上看到的那几样，没有藏起来的状态。"""

    id: str
    device: str
    action: str
    params: dict = field(default_factory=dict)
    source: str = "user"
    reason: str = ""
    status: str = STATUS_QUEUED
    result: dict = field(default_factory=dict)
    error: str = ""
    needs_confirmation: bool = False
    created_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0
    message_id: str = ""
    attempts: int = 0
    # 手动重试的次数。只认手动，失败不会自己再发一遍。
    retries: int = 0
    timeout: float = 0.0
    cancel_requested: bool = False
    # 界面上要显示的是钟表时间，秒数用上面的单调时钟算，两者不混。
    created_wall: float = 0.0
    started_wall: float = 0.0
    finished_wall: float = 0.0

    @property
    def final(self):
        return self.status in protocol.FINAL_STATUSES

    def seconds(self):
        if not self.started_at:
            return 0.0
        end = self.finished_at or time.monotonic()
        return max(0.0, end - self.started_at)

    def to_dict(self):
        return {
            "id": self.id,
            "device": self.device,
            "action": self.action,
            "params": dict(self.params),
            "source": self.source,
            "reason": self.reason,
            "status": self.status,
            "status_label": TASK_LABELS.get(self.status, self.status),
            "ok": self.status == protocol.STATUS_COMPLETED,
            "result": dict(self.result),
            "error": self.error,
            "needs_confirmation": bool(self.needs_confirmation),
            "message_id": self.message_id,
            "attempts": int(self.attempts),
            "retries": int(self.retries),
            "seconds": round(self.seconds(), 3),
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "created_wall": self.created_wall,
            "finished_wall": self.finished_wall,
        }


class DeviceTaskManager:
    """设备动作的唯一入口。所有工具、界面、技能都从这里走。"""

    def __init__(
        self,
        devices=None,
        registry=None,
        permissions=None,
        bus=None,
        bridge=None,
        logger=None,
        audit=None,
        clock=time.monotonic,
        max_queue=32,
        history_limit=60,
        heartbeat_seconds=5.0,
    ):
        self.devices = devices
        self.registry = registry
        self.permissions = permissions
        self.logger = logger
        self.audit = audit
        self.clock = clock
        self.history_limit = max(1, int(history_limit or 60))
        self.bridge = bridge if bridge is not None else DeviceEventBridge(bus=bus, logger=logger)
        self._queue = queue.Queue(maxsize=max(1, int(max_queue or 32)))
        self._lock = threading.RLock()
        self._live = {}
        self._history = []
        self._current = None
        self._worker = None
        self._stop = threading.Event()
        # 心跳间隔：只用来确认设备还在、顺手收设备主动上报，不做高频轮询。
        self.heartbeat_seconds = max(0.0, float(heartbeat_seconds or 0.0))
        self.heartbeats = 0
        # 建好之后先等一个间隔，别一上来就打扰设备。
        self._next_heartbeat = self.clock() + self.heartbeat_seconds

    # ---------- 提交 ----------
    def submit(self, device, action, params=None, source="user", reason="", timeout=None, wait=True):
        """登记并执行一条动作。wait=False 时进队列，由后台线程跑。"""
        device = str(device or "").strip()
        name = str(action or "").strip()
        spec = self.registry.get(name) if self.registry else None
        if spec is None:
            return self._refuse(device, name, params, "没有登记这个动作：%s" % (name or "(空)"), source)
        provider = self._provider(device)
        if provider is None:
            return self._refuse(device, name, params, "没有这台设备，或者它不支持结构化动作：%s" % device, source)
        allowed, needs_confirmation, level, why = self._permission_check(spec, device, reason)
        if not allowed:
            return self._refuse(
                device, name, params, "权限未通过：%s" % why, source, needs_confirmation=needs_confirmation
            )
        if not getattr(provider, "connected", False):
            return self._refuse(device, name, params, "设备未连接：%s" % device, source)
        task = DeviceTask(
            id=uuid.uuid4().hex[:12],
            device=device,
            action=name,
            params=dict(params or {}),
            source=str(source or "user"),
            reason=str(reason or ""),
            created_at=self.clock(),
            created_wall=time.time(),
            timeout=float(timeout or spec.timeout or 0.0) or float(spec.timeout or 3.0),
        )
        self._remember(task)
        if not wait and self._worker is not None:
            try:
                self._queue.put_nowait(task)
            except queue.Full:
                with self._lock:
                    task.status = protocol.STATUS_FAILED
                    task.error = "设备任务队列满了，先等前面的做完"
                    task.finished_at = self.clock()
                    task.finished_wall = time.time()
                self._notify(task)
                return task.to_dict()
            self._notify(task)
            return task.to_dict()
        self._run(task, provider, spec)
        return task.to_dict()

    def retry(self, task_id):
        """手动重试一次。只重试真失败过的，且最多一次。"""
        task = self.get(task_id)
        if task is None:
            return {"ok": False, "error": "没有这条任务"}
        if task.status not in (protocol.STATUS_FAILED, protocol.STATUS_TIMEOUT):
            return {"ok": False, "error": "这条任务不是失败状态，不重试"}
        if task.retries >= MAX_RETRIES:
            return {"ok": False, "error": "这条已经重试过了，先看看设备那边怎么回事"}
        with self._lock:
            task.retries += 1
        return self.submit(
            task.device, task.action, task.params, source=task.source, reason="用户手动重试", wait=True
        )

    # ---------- 取消 ----------
    def cancel(self, task_id):
        with self._lock:
            task = self._live.get(str(task_id or ""))
            if task is None:
                return {"ok": False, "error": "没有这条任务"}
            if task.final:
                return {"ok": False, "error": "这条任务已经结束了"}
            task.cancel_requested = True
            if task.status == STATUS_QUEUED:
                task.status = protocol.STATUS_CANCELLED
                task.error = "取消时还没开始执行"
            else:
                task.status = protocol.STATUS_CANCELLED
                task.error = "已取消；这一步可能已经发给设备，必要时请再让它停下"
            task.finished_at = self.clock()
            task.finished_wall = time.time()
        self._notify(task)
        self._audit("device_task_cancelled", task)
        return {"ok": True, "task": task.to_dict()}

    def cancel_queued(self):
        """把还在排队的一律取消。已经在跑的那条交给 cancel 单独处理。"""
        cancelled = []
        while True:
            try:
                task = self._queue.get_nowait()
            except queue.Empty:
                break
            if task is None:
                continue
            with self._lock:
                if not task.final:
                    task.status = protocol.STATUS_CANCELLED
                    task.error = "取消时还没开始执行"
                    task.finished_at = self.clock()
                    task.finished_wall = time.time()
                    cancelled.append(task.id)
            self._notify(task)
        return cancelled

    def emergency_stop(self):
        """紧急停止：能停的设备都发一次停下，停不了的照实说。"""
        results = self.cancel_queued()
        report = []
        for name in self.device_names():
            provider = self._provider(name)
            if provider is None or not getattr(provider, "connected", False):
                continue
            spec = self.registry.get("device.stop") if self.registry else None
            if spec is None:
                report.append({"device": name, "ok": False, "error": "没有登记停下这个动作"})
                continue
            ok, why = self.registry.supports("device.stop", getattr(provider, "capabilities", ()))
            if not ok:
                report.append({"device": name, "ok": False, "error": "这台设备没有停下的能力：%s" % why})
                continue
            reply = provider.send("device.stop", {}, timeout=spec.timeout)
            report.append({"device": name, "ok": bool(reply.get("ok")), "error": reply.get("error") or ""})
        with self._lock:
            running = self._current
        if running is not None:
            self.cancel(running.id)
        return {"cancelled": results, "devices": report}

    # ---------- 读 ----------
    def get(self, task_id):
        with self._lock:
            return self._live.get(str(task_id or ""))

    def recent(self, limit=20):
        with self._lock:
            rows = list(self._history)
        return [task.to_dict() for task in rows[-max(1, int(limit or 20)):]][::-1]

    def counts(self):
        with self._lock:
            rows = list(self._history)
        out = {status: 0 for status in TASK_STATUSES}
        for task in rows:
            out[task.status] = out.get(task.status, 0) + 1
        return out

    def current(self):
        with self._lock:
            task = self._current
        return task.to_dict() if task is not None else None

    def device_names(self):
        names = getattr(self.devices, "names", None)
        if not callable(names):
            return []
        try:
            return list(names())
        except Exception:
            return []

    # ---------- 后台线程 ----------
    def start(self):
        """起一个后台线程跑异步任务。界面不阻塞，但顺序仍然是一条一条来。"""
        if self._worker is not None:
            return False
        self._stop.clear()
        self._next_heartbeat = self.clock() + self.heartbeat_seconds
        self._worker = threading.Thread(target=self._loop, name="device-tasks", daemon=True)
        self._worker.start()
        return True

    def stop(self, timeout=1.0):
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        worker, self._worker = self._worker, None
        if worker is not None and worker.is_alive():
            worker.join(timeout=max(0.0, float(timeout or 0.0)))
        return True

    def _loop(self):
        while not self._stop.is_set():
            try:
                task = self._queue.get(timeout=0.2)
            except queue.Empty:
                self._maybe_heartbeat()
                continue
            if task is None:
                break
            if task.final:
                continue
            provider = self._provider(task.device)
            spec = self.registry.get(task.action) if self.registry else None
            if provider is None or spec is None:
                with self._lock:
                    task.status = protocol.STATUS_FAILED
                    task.error = "设备或动作已经不在了"
                    task.finished_at = self.clock()
                    task.finished_wall = time.time()
                self._notify(task)
                continue
            self._run(task, provider, spec)

    def _maybe_heartbeat(self):
        """到点了就问一遍设备还在不在。没到点什么都不做。"""
        if self.heartbeat_seconds <= 0:
            return []
        now = self.clock()
        if now < self._next_heartbeat:
            return []
        self._next_heartbeat = now + self.heartbeat_seconds
        return self.heartbeat_devices()

    def heartbeat_devices(self):
        """问一遍连着设备的现状，顺手把设备主动上报的内容收上来。

        只问连着的；没连的一台都不问，也不因此报错。
        """
        rows = []
        for name in self.device_names():
            provider = self._provider(name)
            if provider is None or not getattr(provider, "connected", False):
                continue
            poll = getattr(provider, "poll", None)
            if callable(poll):
                try:
                    poll()
                except Exception as exc:
                    if self.logger:
                        self.logger.warning("device %s 收事件失败：%s", name, exc)
            beat = getattr(provider, "heartbeat", None)
            if callable(beat):
                try:
                    rows.append({"device": name, "result": beat()})
                except Exception as exc:
                    rows.append({"device": name, "result": {"ok": False, "error": str(exc)}})
        self.heartbeats += 1
        return rows

    # ---------- 内部 ----------
    def _run(self, task, provider, spec):
        with self._lock:
            if task.status in protocol.FINAL_STATUSES:
                return
            task.status = "running"
            task.started_at = self.clock()
            task.started_wall = time.time()
            task.attempts += 1
            self._current = task
        self._notify(task)
        self._audit("device_task_start", task)
        try:
            reply = provider.send(task.action, task.params, timeout=task.timeout)
        except Exception as exc:
            reply = {"ok": False, "status": protocol.STATUS_FAILED, "error": "设备操作抛异常：%s" % exc}
        with self._lock:
            if task.cancel_requested:
                task.status = protocol.STATUS_CANCELLED
                task.error = task.error or "已取消"
            else:
                task.status = str(reply.get("status") or protocol.STATUS_FAILED)
                task.result = dict(reply.get("result") or {})
                task.error = str(reply.get("error") or "")
                task.message_id = str(reply.get("message_id") or "")
            task.finished_at = self.clock()
            task.finished_wall = time.time()
            self._current = None
        self._notify(task)
        self._audit("device_task_done", task)

    def _permission_check(self, spec, device, reason):
        if self.permissions is None:
            from devices.permissions import DevicePermissionService

            service = DevicePermissionService(permissions=getattr(self, "permission_manager", None))
            return service.check_action(spec, device, reason)
        return self.permissions.check_action(spec, device, reason)

    def _provider(self, device):
        getter = getattr(self.devices, "get", None)
        if not callable(getter):
            return None
        try:
            adapter = getter(device)
        except Exception:
            return None
        if adapter is None:
            return None
        if callable(getattr(adapter, "send", None)) and hasattr(adapter, "capabilities"):
            return adapter
        return None

    def _remember(self, task):
        with self._lock:
            self._live[task.id] = task
            self._history.append(task)
            if len(self._history) > self.history_limit:
                del self._history[: len(self._history) - self.history_limit]
        return task

    def _refuse(self, device, action, params, message, source="user", needs_confirmation=False):
        task = DeviceTask(
            id=uuid.uuid4().hex[:12],
            device=str(device or ""),
            action=str(action or ""),
            params=dict(params or {}),
            source=str(source or "user"),
            status=protocol.STATUS_FAILED,
            error=str(message),
            needs_confirmation=bool(needs_confirmation),
            created_at=self.clock(),
            finished_at=self.clock(),
            created_wall=time.time(),
            finished_wall=time.time(),
        )
        self._remember(task)
        self._notify(task)
        self._audit("device_task_refused", task)
        return task.to_dict()

    def _notify(self, task):
        if self.bridge is None:
            return None
        try:
            return self.bridge.task_update(task)
        except Exception as exc:
            if self.logger:
                self.logger.warning("device task event failed: %s", exc)
            return None

    def _audit(self, kind, task):
        if self.audit is not None:
            try:
                self.audit.record(
                    kind=kind,
                    name=task.device,
                    level="DEVICE",
                    decision=task.status,
                    detail="%s %s %s" % (task.action, task.params, task.error),
                )
            except Exception:
                pass
        if self.logger:
            self.logger.info("%s %s %s -> %s", kind, task.device, task.action, task.status)
