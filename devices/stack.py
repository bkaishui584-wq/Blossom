"""按设备清单把设备栈装配起来。界面、工具、技能都从这里拿同一份实例。

没接硬件时默认装一台模拟设备，链路照样能跑通；真串口要清单里写了端口、
而且 allow_real 打开，才会真的去开端口。
"""

import time

from devices import protocol
from devices.actions import DeviceCommandRegistry, copy_limits
from devices.bridge import DeviceEventBridge
from devices.manager import DeviceManager
from devices.manifest import install as install_specs
from devices.manifest import load_manifest
from devices.permissions import DevicePermissionService
from devices.provider import LoopbackProvider, build_provider
from devices.tasks import DeviceTaskManager

DEFAULT_SIM_NAME = "sim-1"
DEFAULT_SIM_LIMITS = {
    "led_channels": {"min": 0, "max": 1},
    "servo_channels": {"min": 0, "max": 1},
    "servo_angle": {"min": 10, "max": 170},
    "servo_speed": {"min": 10, "max": 360},
    "servo_duration_ms": {"min": 50, "max": 2000},
    "sensor_channels": {"min": 0, "max": 0},
    "audio_tracks": ["boot", "confirm", "done"],
    "audio_volume": {"min": 0, "max": 100},
}


class DeviceStack:
    """一套设备组件。assembled 之后 register 模拟设备、连接、交给界面用。"""

    def __init__(self, manager, actions, permissions, tasks, bridge, logger=None):
        self.manager = manager
        self.actions = actions
        self.permissions = permissions
        self.tasks = tasks
        self.bridge = bridge
        self.logger = logger
        # 设备主动上报时交给谁。build_stack 里接上事件桥，没有就只是记着。
        self.event_handler = None

    # ---------- 只读视图 ----------
    def catalog(self):
        rows = []
        for name in self.manager.names():
            adapter = self.manager.get(name)
            state = {}
            try:
                state = adapter.get_state() or {}
            except Exception:
                state = {}
            rows.append(
                {
                    "name": name,
                    "kind": getattr(adapter, "kind", "device"),
                    "simulated": bool(getattr(adapter, "is_mock", True)),
                    "connected": bool(getattr(adapter, "connected", False)),
                    "transport": getattr(adapter, "transport_name", ""),
                    "port": getattr(adapter, "port", ""),
                    "firmware": getattr(adapter, "firmware_version", ""),
                    "capabilities": list(getattr(adapter, "capabilities", ()) or ()),
                    "limits": copy_limits(getattr(adapter, "limits", {}) or {}),
                    "actions": state.get("actions") or [],
                    "last_status": state.get("last_status", ""),
                    "last_error": state.get("last_error", ""),
                    "structured": callable(getattr(adapter, "send", None)),
                }
            )
        return rows

    def status_text(self):
        rows = self.catalog()
        if not rows:
            return "还没有接入任何设备。"
        parts = []
        for row in rows:
            state = "已连接" if row["connected"] else "未连接"
            kind = "模拟设备" if row["simulated"] else "真机"
            parts.append("%s（%s，%s）" % (row["name"], kind, state))
        return "设备：" + "；".join(parts) + "。"

    def logs(self, limit=40):
        rows = []
        for name in self.manager.names():
            adapter = self.manager.get(name)
            simulator = getattr(adapter, "simulator", None)
            if simulator is None:
                continue
            for note in simulator.log[-limit:]:
                rows.append("%s: %s" % (name, note))
        return rows


def register_simulator(
    manager,
    name=DEFAULT_SIM_NAME,
    registry=None,
    logger=None,
    capabilities=None,
    limits=None,
    on_event=None,
):
    """装一台模拟设备。开发、测试、界面演示都靠它。"""
    provider = LoopbackProvider(
        name=name,
        capabilities=capabilities,
        limits=dict(limits or DEFAULT_SIM_LIMITS),
        registry=registry,
        logger=logger,
        on_event=on_event,
    )
    result = manager.register(provider)
    return result, provider


def device_event_handler(bridge):
    """设备主动上报 → 事件桥。按登记过的事件名翻译，没登记的就报一声。"""

    def handle(device, frame):
        if bridge is None:
            return None
        event = str((frame or {}).get("event") or "")
        data = (frame or {}).get("data") or {}
        if event == protocol.EVENT_SENSOR:
            return bridge.reading(device, data, note="设备主动上报")
        if event == protocol.EVENT_BUTTON:
            return bridge.alert(device, "设备上的按钮被按下了", level="info")
        if event == protocol.EVENT_STATE:
            return bridge.alert(device, "设备状态变了", level="info")
        return bridge.error(device, "设备发来没登记的事件：%s" % event)

    return handle


def build_stack(
    devices_dir="",
    logger=None,
    audit=None,
    permissions=None,
    bus=None,
    allow_real=False,
    clock=time.monotonic,
    with_simulator=True,
    simulator_name=DEFAULT_SIM_NAME,
    start_worker=False,
    manager=None,
):
    """装配整套设备栈。任何一步失败都不该拖垮桌宠启动。

    manager 传进来就复用那一份注册表：设备中心、设备工具和实体身体
    看到的是同一份清单，不会各报各的。
    """
    if manager is None:
        manager = DeviceManager(logger=logger, audit=audit, allow_real=allow_real, clock=clock)
    actions = DeviceCommandRegistry()
    bridge = DeviceEventBridge(bus=bus, logger=logger)
    device_permissions = DevicePermissionService(permissions=permissions, logger=logger)
    tasks = DeviceTaskManager(
        devices=manager,
        registry=actions,
        permissions=device_permissions,
        bridge=bridge,
        logger=logger,
        audit=audit,
        clock=clock,
    )
    stack = DeviceStack(manager, actions, device_permissions, tasks, bridge, logger=logger)
    stack.event_handler = device_event_handler(bridge)
    if with_simulator:
        register_simulator(
            manager,
            name=simulator_name,
            registry=actions,
            logger=logger,
            on_event=stack.event_handler,
        )
    if devices_dir:
        try:
            specs, errors = load_manifest_from(devices_dir)
            if specs:
                results = []
                for spec in specs:
                    try:
                        adapter = build_provider(
                            spec,
                            registry=actions,
                            logger=logger,
                            on_event=stack.event_handler,
                        )
                    except Exception as exc:
                        results.append({"name": spec.name, "ok": False, "error": str(exc)})
                        continue
                    outcome = manager.register(adapter)
                    results.append(
                        {"name": spec.name, "ok": bool(outcome.get("ok")), "error": outcome.get("error", "")}
                    )
                stack.manifest_results = results
                for problem in errors:
                    if logger:
                        logger.warning("device manifest: %s", problem)
        except Exception as exc:
            if logger:
                logger.warning("device manifest skipped: %s", exc)
    if start_worker:
        tasks.start()
    return stack


def load_manifest_from(root):
    """读设备清单，返回 (specs, errors)。读不动就当没有清单，不是致命错误。"""
    from devices.manifest import find_manifest

    path = find_manifest(root)
    if not path:
        return [], []
    return load_manifest(path)
