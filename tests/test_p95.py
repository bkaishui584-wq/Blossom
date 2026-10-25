"""阶段 V11 验收：ESP32 设备接入层。

协议、白名单、参数范围、设备授权、任务状态机、取消与超时、事件桥、界面接口。
全程用模拟设备：不开串口、不连真硬件。真串口只验证「没配端口不连」「allow_real
没打开就拒收」。测试用临时目录，不碰用户数据。
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from devices import protocol  # noqa: E402
from devices.actions import DeviceCommandRegistry  # noqa: E402
from devices.bridge import DeviceEventBridge  # noqa: E402
from devices.manager import DeviceManager  # noqa: E402
from devices.interface import MockDevice  # noqa: E402
from devices.manifest import DeviceSpec  # noqa: E402
from devices.permissions import DevicePermissionService, action_key  # noqa: E402
from devices.provider import (  # noqa: E402
    LoopbackProvider,
    SerialProvider,
    build_provider,
    safe_device_id,
)
from devices.registry import DeviceRegistry  # noqa: E402
from devices.simulator import DeviceSimulator  # noqa: E402
from devices.stack import build_stack, register_simulator  # noqa: E402
from devices.tasks import DeviceTaskManager  # noqa: E402
from events.bus import DEVICE_EVENT, EventBus  # noqa: E402
from app.main import build_agent  # noqa: E402
from database.db import Database  # noqa: E402
from security.config import Config  # noqa: E402
from skills import build_skills  # noqa: E402
from tools.device_actions import LIST_TOOL, RUN_TOOL, register_device_tools  # noqa: E402
from tools.permission import GrantMode, PermissionManager  # noqa: E402
from tools.registry import ToolExecutor, ToolRegistry  # noqa: E402

LIMITS = {
    "led_channels": {"min": 0, "max": 1},
    "servo_channels": {"min": 0, "max": 1},
    "servo_angle": {"min": 10, "max": 170},
    "sensor_channels": {"min": 0, "max": 0},
}
CAPS = ("status", "led", "servo", "sensor", "audio", "stop")


def make_provider(name="sim-1", capabilities=CAPS, **kwargs):
    kwargs.setdefault("limits", dict(LIMITS))
    return LoopbackProvider(name=name, capabilities=capabilities, **kwargs)


class _Realish(LoopbackProvider):
    """假装真机：只把 is_mock 标成 False，用来验证 allow_real 闸门。"""

    is_mock = False


class _LegacyAdapter:
    """老式适配层：没有 send，任务管理必须如实拒绝，不能当它能跑动作。"""

    name = "old-1"
    is_mock = True
    connected = True

    def connect(self):
        return {"ok": True}

    def disconnect(self):
        return {"ok": True}

    def get_state(self):
        return {"device": self.name}

    def execute(self, command):
        return {"ok": True}


def wait_for(predicate, seconds=3.0, step=0.02):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return bool(predicate())


class ProtocolTests(unittest.TestCase):
    def test_request_round_trip(self):
        frame = protocol.make_request("esp32-main", "led.set", {"channel": 0, "on": True})
        line = protocol.encode(frame)
        self.assertEqual(protocol.decode(line), frame)
        self.assertEqual(protocol.check_request(frame), [])

    def test_message_ids_are_unique(self):
        ids = {protocol.new_message_id() for _ in range(500)}
        self.assertEqual(len(ids), 500)

    def test_version_must_match(self):
        frame = protocol.make_request("esp32-main", "led.set")
        frame["version"] = 2
        self.assertTrue(protocol.check_request(frame))

    def test_bad_identifiers_are_rejected(self):
        frame = protocol.make_request("esp32-main", "led.set")
        frame["message_id"] = "报文 id"
        self.assertTrue(protocol.check_request(frame))
        frame = protocol.make_request("esp32-main", "led.set")
        frame["device_id"] = ""
        self.assertTrue(protocol.check_request(frame))
        frame = protocol.make_request("esp32-main", "led.set")
        frame["message_id"] = "x" * 80
        self.assertTrue(protocol.check_request(frame))

    def test_command_shape_is_enforced(self):
        for name in ("LED", "led", "", "led.set; rm", "led.set "):
            frame = protocol.make_request("esp32-main", name)
            self.assertTrue(protocol.check_request(frame), name)

    def test_params_must_be_object(self):
        frame = protocol.make_request("esp32-main", "led.set")
        frame["params"] = [1, 2]
        self.assertTrue(protocol.check_request(frame))

    def test_too_long_line_is_refused(self):
        limit = protocol.MAX_LINE_BYTES
        frame = protocol.make_request("esp32-main", "led.set", {"blob": "x" * (limit + 50)})
        with self.assertRaises(protocol.ProtocolError):
            protocol.encode(frame)
        ok = protocol.make_request("esp32-main", "led.set", {"blob": "x" * 16})
        self.assertLessEqual(len(protocol.encode(ok).encode("utf-8")), limit)

    def test_reply_validation(self):
        reply = protocol.make_reply("msg-1", "esp32-main", protocol.STATUS_COMPLETED, {"angle": 90})
        self.assertEqual(protocol.check_reply(reply, "msg-1", "esp32-main"), [])
        self.assertTrue(protocol.check_reply(reply, "msg-2"))
        self.assertTrue(protocol.check_reply(reply, "msg-1", "other-board"))
        bad = protocol.make_reply("msg-1", "esp32-main", "finished", {})
        self.assertTrue(protocol.check_reply(bad))
        bad = protocol.make_reply("msg-1", "esp32-main", protocol.STATUS_COMPLETED, {}, "不该有错")
        self.assertTrue(protocol.check_reply(bad))
        bad = protocol.make_reply("msg-1", "esp32-main", protocol.STATUS_FAILED, {})
        self.assertTrue(protocol.check_reply(bad))

    def test_status_helpers(self):
        self.assertTrue(protocol.is_final(protocol.STATUS_TIMEOUT))
        self.assertFalse(protocol.is_final(protocol.STATUS_ACCEPTED))
        self.assertEqual(protocol.status_label(protocol.STATUS_DISCONNECTED), "设备已断开")
        self.assertEqual(protocol.status_label(protocol.STATUS_REJECTED), "设备拒收")
        self.assertEqual(len(protocol.STATUSES), 8)
        self.assertTrue(protocol.is_final(protocol.STATUS_REJECTED))
        self.assertEqual(protocol.error_label(protocol.ERROR_OUT_OF_RANGE), "参数超出设备声明的范围")


class ActionRegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = DeviceCommandRegistry()

    def test_default_actions(self):
        self.assertEqual(
            self.registry.names(),
            [
                "device.get_status",
                "device.get_capabilities",
                "sensor.read",
                "led.set",
                "audio.play",
                "servo.set_angle",
                "device.stop",
            ],
        )

    def test_unknown_action_and_param(self):
        self.assertTrue(self.registry.validate("motor.spin", {})[1])
        self.assertTrue(self.registry.validate("led.set", {"channel": 0, "on": True, "extra": 1})[1])

    def test_missing_and_wrong_typed_params(self):
        self.assertTrue(self.registry.validate("led.set", {"channel": 0})[1])
        self.assertTrue(self.registry.validate("led.set", {"channel": 0, "on": "yes"}, LIMITS)[1])
        self.assertTrue(self.registry.validate("led.set", {"channel": True, "on": True}, LIMITS)[1])
        self.assertTrue(self.registry.validate("sensor.read", {"channel": "左"}, LIMITS)[1])

    def test_servo_range_must_be_declared_by_the_device(self):
        clean, error = self.registry.validate("servo.set_angle", {"channel": 0, "angle_deg": 90})
        self.assertIsNone(clean)
        self.assertIn("范围", error)
        clean, error = self.registry.validate(
            "servo.set_angle", {"channel": 0, "angle_deg": 90}, LIMITS
        )
        self.assertEqual(error, "")
        self.assertEqual(clean, {"channel": 0, "angle_deg": 90})

    def test_declared_range_is_enforced(self):
        self.assertTrue(
            self.registry.validate("servo.set_angle", {"channel": 0, "angle_deg": 180}, LIMITS)[1]
        )
        self.assertTrue(
            self.registry.validate("servo.set_angle", {"channel": 0, "angle_deg": 5}, LIMITS)[1]
        )
        self.assertTrue(
            self.registry.validate("servo.set_angle", {"channel": 2, "angle_deg": 90}, LIMITS)[1]
        )
        self.assertEqual(
            self.registry.validate("servo.set_angle", {"channel": 0, "angle_deg": "90"}, LIMITS)[1], ""
        )

    def test_devices_without_status_capability(self):
        ok, reason = self.registry.supports("device.stop", ("led",))
        self.assertFalse(ok)
        self.assertIn("stop", reason)
        ok, reason = self.registry.supports("led.set", ("led",))
        self.assertTrue(ok)

    def test_available_for_marks_missing_ranges(self):
        rows = self.registry.available_for(CAPS, {"led_channels": {"min": 0, "max": 1}})
        by_name = {row["name"]: row for row in rows}
        self.assertFalse(by_name["servo.set_angle"]["available"])
        self.assertIn("范围", by_name["servo.set_angle"]["reason"])
        self.assertTrue(by_name["led.set"]["available"])
        self.assertFalse(by_name["sensor.read"]["available"])

    def test_catalog_reports_ranges(self):
        rows = {row["name"]: row for row in self.registry.catalog()}
        angle = [p for p in rows["servo.set_angle"]["params"] if p["name"] == "angle_deg"][0]
        self.assertTrue(angle["declared"])
        self.assertNotIn("min", angle)  # 设备还没声明范围，这里不许自己填一个
        live = {row["name"]: row for row in self.registry.available_for(CAPS, LIMITS)}
        resolved = [p for p in live["led.set"]["params"] if p["name"] == "channel"][0]
        self.assertEqual(resolved["min"], 0)
        self.assertEqual(resolved["max"], 1)
        self.assertTrue(rows["servo.set_angle"]["needs_confirmation"])
        self.assertIn("不自动重试", rows["servo.set_angle"]["on_failure"])
        self.assertTrue(rows["servo.set_angle"]["on_disconnect"])

def make_tasks(manager, permissions=None, **kwargs):
    """带默认授权的任务管理。默认全部批准，方便测状态机本身。"""
    if permissions is None:
        permissions = DevicePermissionService(
            permissions=PermissionManager(confirm_callback=lambda *args: True)
        )
    return DeviceTaskManager(
        devices=manager, registry=DeviceCommandRegistry(), permissions=permissions, **kwargs
    )


class SimulatorTests(unittest.TestCase):
    def setUp(self):
        self.sim = DeviceSimulator(
            device_id="esp32-sim", limits=dict(LIMITS), sensor_reader=lambda channel: 42
        )

    def exchange(self, command, params=None, device_id="esp32-sim", message_id=None):
        frame = protocol.make_request(device_id, command, params or {}, message_id)
        self.sim.handle(protocol.encode(frame))
        lines = self.sim.read_all()
        return frame, lines

    def reply(self, lines):
        self.assertEqual(len(lines), 1)
        return protocol.decode(lines[0])

    def test_led_reports_the_real_state(self):
        frame, lines = self.exchange("led.set", {"channel": 1, "on": True})
        reply = self.reply(lines)
        self.assertEqual(reply["status"], protocol.STATUS_COMPLETED)
        self.assertEqual(reply["message_id"], frame["message_id"])
        self.assertEqual(reply["result"]["channel"], 1)
        self.assertTrue(self.sim.led[1])

    def test_out_of_range_angle_is_refused_by_the_device(self):
        _, lines = self.exchange("servo.set_angle", {"channel": 0, "angle_deg": 180})
        reply = self.reply(lines)
        self.assertEqual(reply["status"], protocol.STATUS_REJECTED)
        self.assertEqual(reply["error_code"], protocol.ERROR_OUT_OF_RANGE)
        self.assertIn("170", reply["error"])
        self.assertNotIn(0, self.sim.servo)

    def test_unknown_action_is_refused(self):
        _, lines = self.exchange("motor.spin", {})
        reply = self.reply(lines)
        self.assertEqual(reply["status"], protocol.STATUS_REJECTED)
        self.assertEqual(reply["error_code"], protocol.ERROR_UNKNOWN_COMMAND)
        self.assertIn("固件不认", reply["error"])

    def test_version_and_device_must_match(self):
        frame = protocol.make_request("esp32-sim", "led.set", {"channel": 0, "on": True})
        frame["version"] = 2
        self.sim.handle(protocol.encode(frame))
        reply = self.reply(self.sim.read_all())
        self.assertIn("协议版本", reply["error"])

        _, lines = self.exchange("led.set", {"channel": 0, "on": True}, device_id="other-board")
        reply = self.reply(lines)
        self.assertIn("不是发给这台设备", reply["error"])

    def test_broken_frames_get_no_reply(self):
        self.assertFalse(self.sim.handle("{这不是 JSON"))
        self.assertFalse(self.sim.handle('{"message_id":"中文 id","command":"led.set"}'))
        self.assertEqual(self.sim.read_all(), [])
        self.assertGreaterEqual(self.sim.rejected, 2)

    def test_disconnected_device_stays_silent(self):
        self.sim.set_disconnected(True)
        self.assertFalse(self.sim.handle(protocol.encode(protocol.make_request("esp32-sim", "led.set"))))
        self.assertEqual(self.sim.read_all(), [])

    def test_silent_action_produces_no_ack(self):
        sim = DeviceSimulator(device_id="esp32-sim", limits=dict(LIMITS), silent_actions=("led.set",))
        sim.handle(
            protocol.encode(
                protocol.make_request("esp32-sim", "led.set", {"channel": 0, "on": True})
            )
        )
        self.assertEqual(sim.read_all(), [])

    def test_stop_reports_what_it_did(self):
        self.exchange("led.set", {"channel": 0, "on": True})
        _, lines = self.exchange("device.stop", {})
        reply = self.reply(lines)
        self.assertTrue(reply["result"]["stopped"])
        self.assertFalse(self.sim.led[0])

    def test_sensor_never_invents_a_number(self):
        _, lines = self.exchange("sensor.read", {"channel": 0})
        self.assertEqual(self.reply(lines)["result"]["value"], 42)
        blind = DeviceSimulator(device_id="esp32-sim", limits=dict(LIMITS))
        blind.handle(
            protocol.encode(protocol.make_request("esp32-sim", "sensor.read", {"channel": 0}))
        )
        self.assertIsNone(protocol.decode(blind.read_all()[0])["result"]["value"])

    def test_status_reports_what_the_device_really_has(self):
        _, lines = self.exchange("device.get_status", {})
        result = self.reply(lines)["result"]
        self.assertEqual(result["version"], self.sim.version)
        self.assertEqual(sorted(result["capabilities"]), sorted(CAPS))
        self.assertEqual(result["limits"]["servo_angle"]["max"], 170)


class ProviderTests(unittest.TestCase):
    def test_connect_learns_from_the_device(self):
        provider = make_provider(version="fw-9.9")
        result = provider.connect()
        self.assertTrue(result["ok"])
        self.assertEqual(provider.firmware_version, "fw-9.9")
        self.assertEqual(sorted(provider.capabilities), sorted(CAPS))
        self.assertEqual(provider.limits["servo_angle"]["max"], 170)

    def test_send_waits_for_the_device_confirmation(self):
        provider = make_provider()
        provider.connect()
        reply = provider.send("led.set", {"channel": 0, "on": True})
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["status"], protocol.STATUS_COMPLETED)
        self.assertTrue(reply["message_id"].startswith("msg-"))
        self.assertEqual(provider.last_status, protocol.STATUS_COMPLETED)

    def test_send_without_connection_fails(self):
        provider = make_provider()
        reply = provider.send("led.set", {"channel": 0, "on": True})
        self.assertFalse(reply["ok"])
        self.assertIn("未连接", reply["error"])

    def test_replies_for_other_messages_are_ignored(self):
        provider = make_provider()
        provider.connect()
        stray = protocol.make_reply("msg-someone-else", provider.device_id, protocol.STATUS_COMPLETED)
        provider.transport.inbox.append(protocol.encode(stray))
        reply = provider.send("led.set", {"channel": 0, "on": True}, timeout=0.15)
        self.assertEqual(reply["status"], protocol.STATUS_COMPLETED)
        self.assertGreaterEqual(provider.stray, 1)

    def test_silent_device_times_out(self):
        provider = make_provider(silent_actions=("led.set",))
        provider.connect()
        reply = provider.send("led.set", {"channel": 0, "on": True}, timeout=0.15)
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["status"], protocol.STATUS_TIMEOUT)
        self.assertIn("没等到", reply["error"])

    def test_real_devices_are_refused_unless_allowed(self):
        manager = DeviceManager()
        refused = manager.register(_Realish(name="real-1"))
        self.assertFalse(refused["ok"])
        self.assertIn("禁止", refused["error"])
        opened = DeviceManager(allow_real=True)
        self.assertTrue(opened.register(_Realish(name="real-1"))["ok"])
        self.assertIs(DeviceRegistry, DeviceManager)

    def test_serial_provider_without_a_port_does_not_connect(self):
        provider = SerialProvider(name="esp32-main", port="")
        self.assertFalse(provider.connect()["ok"])
        self.assertFalse(provider.is_mock)
        self.assertIn("串口名", provider.last_error)

    def test_build_provider_never_invents_a_real_port(self):
        plain = build_provider(DeviceSpec(name="s", kind="esp32", capabilities=CAPS, options={}))
        self.assertIsInstance(plain, LoopbackProvider)
        self.assertTrue(plain.is_mock)
        wired = build_provider(
            DeviceSpec(name="esp32-main", kind="esp32", capabilities=CAPS, options={"port": "COM3"})
        )
        self.assertIsInstance(wired, SerialProvider)
        self.assertEqual(wired.port, "COM3")

    def test_device_ids_are_protocol_safe(self):
        self.assertEqual(safe_device_id("esp32-main"), "esp32-main")
        self.assertEqual(safe_device_id("小舵机"), "esp32")
        self.assertEqual(safe_device_id(""), "esp32")
        self.assertNotIn(" ", safe_device_id("my board"))


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.registry = DeviceCommandRegistry()
        self.grants_path = os.path.join(tempfile.mkdtemp(prefix="deskpet-v11-"), "permissions.json")

    def service(self, callback=None):
        from tools.permission import GrantStore

        manager = PermissionManager(
            confirm_callback=callback, grants=GrantStore(self.grants_path)
        )
        return DevicePermissionService(permissions=manager)

    def test_without_a_permission_centre_nothing_runs(self):
        service = DevicePermissionService(permissions=None)
        allowed, _, _, reason = service.check_action(self.registry.get("led.set"), "sim-1")
        self.assertFalse(allowed)
        self.assertIn("权限中心", reason)

    def test_servo_needs_a_confirmation_channel(self):
        service = self.service(callback=None)
        self.assertTrue(service.check_action(self.registry.get("led.set"), "sim-1")[0])
        allowed, needs, level, _ = service.check_action(self.registry.get("servo.set_angle"), "sim-1")
        self.assertFalse(allowed)
        self.assertTrue(needs)
        self.assertEqual(level, "MEDIUM")

    def test_user_can_approve_or_refuse(self):
        service = self.service(callback=lambda *args: True)
        self.assertTrue(service.check_action(self.registry.get("servo.set_angle"), "sim-1")[0])
        refusing = self.service(callback=lambda *args: False)
        allowed, needs, _, reason = refusing.check_action(self.registry.get("servo.set_angle"), "sim-1")
        self.assertFalse(allowed)
        self.assertIn("拒绝", reason)

    def test_persistent_grants_are_scoped_to_one_device_and_action(self):
        service = self.service(callback=lambda *args: False)
        service.remember("sim-1", "servo.set_angle", GrantMode.PERSISTENT.value)
        self.assertTrue(service.check_action(self.registry.get("servo.set_angle"), "sim-1")[0])
        self.assertFalse(service.check_action(self.registry.get("servo.set_angle"), "sim-2")[0])
        self.assertEqual(
            [grant["tool"] for grant in service.action_grants()],
            [action_key("sim-1", "servo.set_angle")],
        )
        service.revoke("sim-1", "servo.set_angle")
        self.assertFalse(service.check_action(self.registry.get("servo.set_angle"), "sim-1")[0])

    def test_deny_grant_blocks_even_with_approval(self):
        service = self.service(callback=lambda *args: True)
        service.remember("sim-1", "led.set", GrantMode.DENY.value)
        allowed, _, _, reason = service.check_action(self.registry.get("led.set"), "sim-1")
        self.assertFalse(allowed)
        self.assertIn("拒绝", reason)

    def test_port_authorisation_is_remembered_per_port(self):
        service = self.service(callback=lambda *args: False)
        allowed, needs, _, _ = service.check_connect("COM3")
        self.assertFalse(allowed)
        self.assertTrue(needs)
        from tools.permission import GrantMode as Mode

        store = service.permissions.grants
        store.set("device.connect", Mode.PERSISTENT.value, scope="path:COM3", note="用户确认过")
        self.assertTrue(service.check_connect("COM3")[0])
        self.assertFalse(service.check_connect("COM7")[0])
        self.assertTrue(service.check_connect("COM3")[2])

class TaskManagerTests(unittest.TestCase):
    def build(self, permissions=None, **kwargs):
        manager = DeviceManager()
        self.provider = make_provider()
        self.assertTrue(manager.register(self.provider)["ok"])
        self.provider.connect()
        return make_tasks(manager, permissions=permissions, **kwargs)

    def test_completed_task_records_the_device_result(self):
        tasks = self.build()
        result = tasks.submit("sim-1", "led.set", {"channel": 0, "on": True})
        self.assertEqual(result["status"], protocol.STATUS_COMPLETED)
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["channel"], 0)
        self.assertEqual(result["attempts"], 1)
        self.assertTrue(result["message_id"])
        self.assertGreater(result["finished_wall"], 0)

    def test_failed_action_is_reported_and_not_retried(self):
        provider = make_provider(fail_actions=("led.set",))
        manager = DeviceManager()
        manager.register(provider)
        provider.connect()
        tasks = make_tasks(manager)
        result = tasks.submit("sim-1", "led.set", {"channel": 0, "on": True})
        self.assertEqual(result["status"], protocol.STATUS_FAILED)
        self.assertIn("没做成", result["error"])
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(len(provider.history), 2)  # 连接时的能力查询 + 这一次

    def test_timeout_is_reported_as_timeout(self):
        provider = make_provider(silent_actions=("led.set",))
        manager = DeviceManager()
        manager.register(provider)
        provider.connect()
        tasks = make_tasks(manager)
        result = tasks.submit("sim-1", "led.set", {"channel": 0, "on": True}, timeout=0.15)
        self.assertEqual(result["status"], protocol.STATUS_TIMEOUT)
        self.assertIn("没等到", result["error"])

    def test_unregistered_action_and_unknown_device_are_refused(self):
        tasks = self.build()
        self.assertIn("没有登记", tasks.submit("sim-1", "motor.spin", {})["error"])
        self.assertIn("没有这台设备", tasks.submit("nope", "led.set", {})["error"])

    def test_legacy_adapter_is_refused(self):
        manager = DeviceManager()
        manager.register(_LegacyAdapter())
        tasks = make_tasks(manager)
        self.assertIn("不支持结构化动作", tasks.submit("old-1", "led.set", {})["error"])

    def test_connected_is_required(self):
        manager = DeviceManager()
        provider = make_provider()
        manager.register(provider)
        tasks = make_tasks(manager)
        self.assertIn("未连接", tasks.submit("sim-1", "led.set", {"channel": 0, "on": True})["error"])

    def test_permission_denied_task_is_recorded_but_never_sent(self):
        provider = make_provider()
        manager = DeviceManager()
        manager.register(provider)
        provider.connect()
        before = provider.sent
        service = DevicePermissionService(permissions=PermissionManager(confirm_callback=lambda *a: False))
        tasks = make_tasks(manager, permissions=service)
        result = tasks.submit("sim-1", "servo.set_angle", {"channel": 0, "angle": 90})
        self.assertEqual(result["status"], protocol.STATUS_FAILED)
        self.assertTrue(result["needs_confirmation"])
        self.assertEqual(provider.sent, before)
        self.assertEqual(tasks.counts()["failed"], 1)

    def test_worker_runs_queued_tasks(self):
        tasks = self.build()
        tasks.start()
        try:
            first = tasks.submit("sim-1", "led.set", {"channel": 0, "on": True}, wait=False)
            self.assertIn(first["status"], ("queued", "running", protocol.STATUS_COMPLETED))
            task = tasks.get(first["id"])
            self.assertTrue(wait_for(lambda: task.final))
            self.assertEqual(task.status, protocol.STATUS_COMPLETED)
        finally:
            tasks.stop()

    def test_queue_full_is_refused(self):
        provider = make_provider(silent_actions=("led.set", "device.get_status"))
        manager = DeviceManager()
        manager.register(provider)
        provider.connect()
        tasks = make_tasks(manager, max_queue=1)
        tasks.start()
        try:
            first = tasks.submit("sim-1", "led.set", {"channel": 0, "on": True}, timeout=0.8, wait=False)
            self.assertTrue(wait_for(lambda: tasks.get(first["id"]).status == "running"))
            tasks.submit("sim-1", "led.set", {"channel": 0, "on": True}, timeout=0.8, wait=False)
            third = tasks.submit("sim-1", "led.set", {"channel": 0, "on": True}, timeout=0.8, wait=False)
            self.assertEqual(third["status"], protocol.STATUS_FAILED)
            self.assertIn("队列满", third["error"])
        finally:
            tasks.stop()

    def test_cancelled_queued_task_never_reaches_the_device(self):
        provider = make_provider(silent_actions=("led.set", "device.get_status"))
        manager = DeviceManager()
        manager.register(provider)
        provider.connect()
        tasks = make_tasks(manager, max_queue=4)
        tasks.start()
        try:
            first = tasks.submit("sim-1", "led.set", {"channel": 0, "on": True}, timeout=0.5, wait=False)
            self.assertTrue(wait_for(lambda: tasks.get(first["id"]).status == "running"))
            baseline = provider.sent  # 连上时问过一次能力，那一次也算
            second = tasks.submit("sim-1", "led.set", {"channel": 1, "on": True}, timeout=0.5, wait=False)
            out = tasks.cancel(second["id"])
            self.assertTrue(out["ok"])
            self.assertEqual(tasks.get(second["id"]).status, protocol.STATUS_CANCELLED)
            self.assertTrue(wait_for(lambda: tasks.get(first["id"]).final))
            time.sleep(0.1)
            self.assertEqual(provider.sent, baseline)
        finally:
            tasks.stop()

    def test_cancelling_a_running_task_says_so_plainly(self):
        tasks = self.build()
        tasks.start()
        try:
            first = tasks.submit("sim-1", "led.set", {"channel": 0, "on": True}, wait=False)
            self.assertTrue(wait_for(lambda: tasks.get(first["id"]).final))
            out = tasks.cancel(first["id"])
            self.assertFalse(out["ok"])
            self.assertIn("已经结束", out["error"])
        finally:
            tasks.stop()

    def test_retry_is_manual_and_bounded(self):
        tasks = self.build()
        result = tasks.submit("sim-1", "servo.set_angle", {"channel": 0, "angle": 900})
        self.assertEqual(result["status"], protocol.STATUS_FAILED)
        again = tasks.retry(result["id"])
        self.assertNotEqual(again["id"], result["id"])
        self.assertEqual(again["status"], protocol.STATUS_FAILED)
        third = tasks.retry(result["id"])
        self.assertFalse(third["ok"])
        self.assertIn("重试过", third["error"])

    def test_emergency_stop_reports_every_device(self):
        provider = make_provider()
        manager = DeviceManager()
        manager.register(provider)
        provider.connect()
        blind = make_provider(name="led-only", capabilities=("status", "led"))
        manager.register(blind)
        blind.connect()
        tasks = make_tasks(manager)
        report = tasks.emergency_stop()
        by_device = {item["device"]: item for item in report["devices"]}
        self.assertTrue(by_device["sim-1"]["ok"])
        self.assertFalse(by_device["led-only"]["ok"])
        self.assertIn("停下", by_device["led-only"]["error"])


class BridgeTests(unittest.TestCase):
    def test_task_updates_go_through_the_event_bus(self):
        bus = EventBus()
        seen = []
        bus.subscribe(DEVICE_EVENT, lambda event: seen.append(event.payload))
        manager = DeviceManager()
        provider = make_provider()
        manager.register(provider)
        provider.connect()
        tasks = make_tasks(manager)
        tasks.bridge.bus = bus
        tasks.submit("sim-1", "led.set", {"channel": 0, "on": True})
        self.assertTrue(seen)
        self.assertEqual(seen[-1]["kind"], "device.task")
        self.assertEqual(seen[-1]["status"], protocol.STATUS_COMPLETED)
        self.assertNotIn("message_id", seen[-1])

    def test_bridge_without_a_bus_still_records_history(self):
        bridge = DeviceEventBridge()
        event = bridge.error("sim-1", "掉线了")
        self.assertEqual(event["kind"], "device.error")
        self.assertEqual(bridge.recent(limit=1)[0]["text"], "掉线了")


class ToolAndSkillTests(unittest.TestCase):
    def build(self, callback=lambda *args: True):
        from tools.permission import GrantStore

        store = GrantStore(os.path.join(tempfile.mkdtemp(prefix="deskpet-v11-"), "permissions.json"))
        device_manager = PermissionManager(confirm_callback=callback, grants=store)
        stack = build_stack(permissions=device_manager)
        stack.manager.connect("sim-1")
        registry = ToolRegistry()
        register_device_tools(registry, stack)
        executor = ToolExecutor(registry, PermissionManager(confirm_callback=callback))
        return stack, registry, executor

    def test_device_tools_are_registered(self):
        _, registry, _ = self.build()
        self.assertIn(LIST_TOOL, registry.names())
        self.assertIn(RUN_TOOL, registry.names())

    def test_listing_stays_compact(self):
        _, _, executor = self.build()
        outcome = executor.execute(LIST_TOOL)
        self.assertTrue(outcome.ok)
        self.assertFalse(outcome.truncated)
        data = json.loads(outcome.result)
        self.assertEqual(data["devices"][0]["name"], "sim-1")
        self.assertIn("led.set", data["devices"][0]["ready"])

    def test_running_an_unregistered_action_is_an_error(self):
        _, _, executor = self.build()
        outcome = executor.execute(RUN_TOOL, {"device": "sim-1", "action": "motor.spin"})
        self.assertFalse(outcome.ok)
        self.assertIn("没有登记", outcome.error)

    def test_confirmation_is_required_for_servo(self):
        _, _, executor = self.build(callback=lambda *args: False)
        outcome = executor.execute(
            RUN_TOOL, {"device": "sim-1", "action": "servo.set_angle", "params": {"channel": 0, "angle": 90}}
        )
        self.assertFalse(outcome.ok)
        self.assertIn("权限未通过", outcome.error)

    def test_led_runs_without_a_confirmation_channel(self):
        _, _, executor = self.build(callback=None)
        outcome = executor.execute(
            RUN_TOOL, {"device": "sim-1", "action": "led.set", "params": {"channel": 0, "on": True}}
        )
        self.assertTrue(outcome.ok)

    def test_skill_runs_the_real_actions(self):
        _, _, executor = self.build()
        skills = build_skills(tools=executor)
        self.assertIn("device-control", skills.names())
        result = skills.run(
            "device-control",
            request="把灯打开",
            inputs={"device": "sim-1", "action": "led.set", "params": {"channel": 0, "on": True}},
        )
        self.assertTrue(result.ok, result.error)
        self.assertIn("sim-1", result.summary)
        self.assertIn(RUN_TOOL, result.tools)

    def test_skill_reports_blocked_actions_instead_of_guessing(self):
        _, _, executor = self.build(callback=lambda *args: False)
        skills = build_skills(tools=executor)
        result = skills.run(
            "device-control",
            request="转一下舵机",
            inputs={"device": "sim-1", "action": "servo.set_angle", "params": {"channel": 0, "angle": 90}},
        )
        self.assertFalse(result.ok)
        self.assertIn("权限", result.error)

    def test_skill_declares_only_the_device_tools(self):
        _, _, _ = self.build()
        from skills.loader import SkillLoader
        from skills.schema import scan_untrusted

        loader = SkillLoader()
        meta = loader.load("device-control")
        self.assertIsNotNone(meta)
        self.assertEqual(tuple(meta.tools), (LIST_TOOL, RUN_TOOL))
        self.assertEqual(scan_untrusted(meta.raw), ())
        self.assertTrue(meta.steps)


class StackTests(unittest.TestCase):
    def test_build_stack_installs_a_simulator(self):
        stack = build_stack()
        names = stack.manager.names()
        self.assertIn("sim-1", names)
        rows = stack.catalog()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertTrue(row["simulated"])
        self.assertFalse(row["connected"])
        self.assertTrue(row["structured"])
        self.assertIn("设备", stack.status_text())
        stack.manager.connect("sim-1")
        self.assertTrue(stack.catalog()[0]["connected"])

    def test_manifest_without_a_port_stays_simulated(self):
        root = tempfile.mkdtemp(prefix="deskpet-v11-")
        manifest = {
            "devices": [
                {
                    "name": "bench",
                    "kind": "esp32",
                    "capabilities": ["status", "led"],
                    "options": {"limits": {"led_channels": {"min": 0, "max": 0}}},
                }
            ]
        }
        with open(os.path.join(root, "devices.json"), "w", encoding="utf-8") as handle:
            json.dump(manifest, handle)
        stack = build_stack(devices_dir=root)
        rows = {row["name"]: row for row in stack.catalog()}
        self.assertIn("bench", rows)
        self.assertTrue(rows["bench"]["simulated"])
        self.assertEqual(rows["bench"]["limits"]["led_channels"]["max"], 0)

    def test_register_simulator_twice_keeps_one_device(self):
        stack = build_stack(with_simulator=False)
        register_simulator(stack.manager, registry=stack.actions)
        register_simulator(stack.manager, registry=stack.actions)
        self.assertEqual(stack.manager.names(), ["sim-1"])


class FirmwareAssetTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / "firmware" / "esp32_device"
        self.ino = (self.root / "esp32_device.ino").read_text(encoding="utf-8")
        self.config = (self.root / "config.h").read_text(encoding="utf-8")
        self.readme = (self.root / "README.md").read_text(encoding="utf-8")

    # 这份 Arduino 参考固件只做了下面这几样。完整动作白名单以 firmware/esp32
    # （ESP-IDF 工程）为准，那份的覆盖检查在 test_p98 里做。
    ARDUINO_ACTIONS = (
        "device.get_status",
        "led.set",
        "servo.set_angle",
        "sensor.read",
        "device.stop",
    )

    def test_arduino_reference_covers_the_actions_it_claims(self):
        for name in self.ARDUINO_ACTIONS:
            self.assertIn('"%s"' % name, self.ino, name)

    def test_actions_the_arduino_build_lacks_are_written_down(self):
        """它没实现的动作，README 里必须写明，不能让人以为它支持。"""
        for name in DeviceCommandRegistry().names():
            if name in self.ARDUINO_ACTIONS:
                continue
            self.assertIn(name, self.readme, name)

    def test_firmware_declares_its_version_and_capabilities(self):
        self.assertIn("FIRMWARE_VERSION", self.config)
        self.assertIn("DEVICE_ID", self.config)
        self.assertIn("SERVO_MIN_ANGLE", self.config)
        self.assertIn("SERVO_MAX_ANGLE", self.config)
        self.assertIn("SAFETY_IDLE_MS", self.config)

    def test_firmware_validates_on_its_own(self):
        self.assertIn("message_id", self.ino)
        self.assertIn("协议版本", self.ino)
        self.assertIn("固件不认这个动作", self.ino)
        self.assertIn("超出机械范围", self.ino)

    def test_readme_says_what_is_not_verified(self):
        self.assertIn("没有在真机上", self.readme)
        self.assertIn("device.stop", self.readme)
        self.assertIn("设备清单", self.readme)


class SharedRegistryTests(unittest.TestCase):
    """设备只有一份注册表：设备中心、设备工具、实体身体看的是同一份清单。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manager = DeviceManager(allow_real=False)
        self.manager.register(MockDevice(name="mock-1", capabilities=("servo", "sensor")))

    def build(self):
        return build_stack(manager=self.manager)

    def test_stack_reuses_the_given_registry(self):
        stack = self.build()
        self.assertIs(stack.manager, self.manager)
        self.assertIn("sim-1", self.manager.names(), "模拟设备要装进同一份注册表")

    def test_device_center_and_registry_agree(self):
        stack = self.build()
        catalog = {row["name"] for row in stack.catalog()}
        self.assertEqual(catalog, set(self.manager.names()))
        self.assertIn("mock-1", catalog)

    def test_legacy_summary_lines_up_with_the_center(self):
        """老工具报的设备名，和设备中心列的必须是同一批。"""
        stack = self.build()
        state = self.manager.get_state()
        self.assertTrue(state["ok"])
        legacy = {item.get("device") for item in state["devices"]}
        self.assertEqual(legacy, {row["name"] for row in stack.catalog()})

    def test_device_tool_lists_the_same_devices(self):
        stack = self.build()
        registry = ToolRegistry()
        register_device_tools(registry, stack)
        executor = ToolExecutor(
            registry, PermissionManager(confirm_callback=lambda *a, **k: True)
        )
        out = executor.execute(LIST_TOOL, {})
        self.assertTrue(out.ok, out.error or out.result)
        blob = str(out.result)
        for name in {row["name"] for row in stack.catalog()}:
            self.assertIn(name, blob)

    def test_the_app_wires_one_device_registry(self):
        """按真实启动流程装配一遍：设备栈、设备工具、实体身体读的是同一份注册表。"""
        keys = ("DESKPET_ROOT", "DESKPET_DEVICES_DIR", "DESKPET_ALLOW_REAL_DEVICES")
        with tempfile.TemporaryDirectory() as tmp:
            saved = {key: os.environ.get(key) for key in keys}
            os.environ["DESKPET_ROOT"] = tmp
            os.environ.pop("DESKPET_DEVICES_DIR", None)
            os.environ["DESKPET_ALLOW_REAL_DEVICES"] = "0"
            try:
                config = Config(load_env=False)
                database = Database(config.db_path)
                database.migrate()
                agent, _memory = build_agent(config, database, None, None, bus=EventBus())
            finally:
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
        stack = agent.device_stack
        self.assertIsNotNone(stack, "启动装配里要装上设备栈")
        self.assertIs(stack.manager, agent.devices, "设备栈要和身体用同一份注册表")
        names = set(agent.devices.names())
        self.assertEqual({row["name"] for row in stack.catalog()}, names)
        self.assertIn("sim-1", names)
        self.assertIn("mock-1", names)


if __name__ == "__main__":
    unittest.main()
