"""设备管理器。设备操作只能从这里走，模型不能直接控制设备。"""

import time

from devices.interface import MockDevice  # noqa: F401  (便于外部按需引用)

# 设备多久没有回应就算离线（只是健康判断，不影响指令）。
DEFAULT_STALE_SECONDS = 900


class DeviceManager:
    """默认只接受模拟设备。真实设备需要显式打开 allow_real。"""

    def __init__(
        self,
        logger=None,
        audit=None,
        allow_real=False,
        clock=None,
        stale_after=DEFAULT_STALE_SECONDS,
    ):
        self.logger = logger
        self.audit = audit
        self.allow_real = bool(allow_real)
        self.clock = clock or time.time
        self.stale_after = max(0, int(stale_after or 0))
        self._devices = {}
        self._seen = {}

    def register(self, adapter):
        name = getattr(adapter, "name", "") or ""
        if not name:
            return {"ok": False, "error": "设备必须有名字"}
        if not getattr(adapter, "is_mock", False) and not self.allow_real:
            return {"ok": False, "error": f"当前阶段禁止接入真实设备：{name}"}
        self._devices[name] = adapter
        return {"ok": True, "device": name, "mock": bool(getattr(adapter, "is_mock", False))}

    def get(self, name):
        return self._devices.get(str(name or ""))

    def names(self):
        return sorted(self._devices)

    def list_devices(self):
        return [adapter.get_state() for adapter in self._devices.values()]

    # ---------- 设备健康 ----------
    def mark_seen(self, name):
        """记下这台设备最近一次有回应的时刻。"""
        self._seen[str(name or "")] = self.clock()

    def last_seen(self, name):
        return self._seen.get(str(name or ""))

    def is_stale(self, name):
        """很久没有回应就算离线；一次都没回应过也算。"""
        seen = self.last_seen(name)
        if seen is None:
            return True
        if not self.stale_after:
            return False
        return (self.clock() - seen) > self.stale_after

    def connected_names(self):
        return sorted(
            name for name, adapter in self._devices.items()
            if getattr(adapter, "connected", False)
        )

    def stale_names(self):
        return sorted(name for name in self._devices if self.is_stale(name))

    def connect_all(self, names=None):
        """按名单连接。名单为空就一台都不连。返回每台的结果，不抛异常。"""
        return {name: self.connect(name) for name in list(names or [])}

    def disconnect_all(self):
        return {name: self.disconnect(name) for name in self.names()}

    def describe(self):
        """开发者面板用的只读视图。绝不发给模型。"""
        return {
            "count": len(self._devices),
            "names": self.names(),
            "connected": self.connected_names(),
            "stale": self.stale_names(),
            "stale_after": self.stale_after,
            "allow_real": self.allow_real,
        }

    def connect(self, name):
        return self._run(name, "connect")

    def disconnect(self, name):
        return self._run(name, "disconnect")

    def get_state(self, name=None):
        if name is None:
            return {"ok": True, "devices": self.list_devices()}
        adapter = self.get(name)
        if adapter is None:
            return {"ok": False, "error": f"没有这个设备：{name}"}
        try:
            state = adapter.get_state()
        except Exception as exc:
            self._audit("device_error", name, "get_state", str(exc))
            return {"ok": False, "device": name, "error": f"读取设备状态失败：{exc}"}
        self.mark_seen(name)
        return {"ok": True, "device": state}

    def execute(self, name, command):
        return self._run(name, "execute", command)

    def _run(self, name, action, *args):
        adapter = self.get(name)
        if adapter is None:
            self._audit("device_missing", name, action, "设备不存在")
            return {"ok": False, "error": f"没有这个设备：{name}"}
        try:
            result = getattr(adapter, action)(*args)
        except Exception as exc:
            self._audit("device_error", name, action, str(exc))
            return {"ok": False, "device": name, "error": f"设备操作失败：{exc}"}
        self.mark_seen(name)
        self._audit(
            "device_ok" if result.get("ok") else "device_failed",
            name,
            action,
            str(result)[:200],
        )
        return result

    def _audit(self, kind, name, action, detail):
        if self.audit:
            self.audit.record(
                kind=kind, name=str(name), level="DEVICE", decision=action, detail=detail
            )
        if self.logger:
            self.logger.info("%s %s %s: %s", kind, name, action, detail)
