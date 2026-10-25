"""P1 验收：协议对齐。

消息类型、拒收状态、统一错误码、设备主动上报、能力发现、重复消息去重、
音频曲目白名单，以及权限那三件事：没授权就拒绝、撤销后立刻生效、
高风险动作要多一次确认。全程用模拟设备，不开串口，不碰用户数据。
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from devices import protocol  # noqa: E402
from devices.actions import DeviceCommandRegistry  # noqa: E402
from devices.manager import DeviceManager  # noqa: E402
from devices.permissions import DevicePermissionService  # noqa: E402
from devices.provider import LoopbackProvider  # noqa: E402
from devices.simulator import SEEN_LIMIT, DeviceSimulator  # noqa: E402
from devices.tasks import DeviceTaskManager  # noqa: E402
from tools.permission import (  # noqa: E402
    GrantMode,
    GrantStore,
    PermissionLevel,
    PermissionManager,
)

LIMITS = {
    "led_channels": {"min": 0, "max": 1},
    "servo_channels": {"min": 0, "max": 1},
    "servo_angle": {"min": 10, "max": 170},
    "sensor_channels": {"min": 0, "max": 0},
    "audio_tracks": ["boot", "confirm"],
    "audio_volume": {"min": 0, "max": 100},
}
CAPS = ("status", "led", "servo", "sensor", "audio", "stop")


class ProtocolFrameTests(unittest.TestCase):
    """报文形状：类型、状态、错误码。对不上就拒，不猜。"""

    def test_a_request_must_say_it_is_a_command(self):
        frame = protocol.make_request("esp32-main", "led.set", {"channel": 0, "on": True})
        self.assertEqual(frame["type"], protocol.TYPE_COMMAND)
        self.assertEqual(protocol.check_request(frame), [])
        frame.pop("type")
        self.assertTrue(protocol.check_request(frame))

    def test_a_reply_must_say_it_is_a_response(self):
        frame = protocol.make_reply("cmd-1", "esp32-main", protocol.STATUS_COMPLETED, {})
        self.assertEqual(frame["type"], protocol.TYPE_RESPONSE)
        self.assertEqual(frame["error_code"], "")
        self.assertEqual(protocol.check_reply(frame, "cmd-1", "esp32-main"), [])
        frame["type"] = protocol.TYPE_EVENT
        self.assertTrue(protocol.check_reply(frame, "cmd-1", "esp32-main"))

    def test_an_event_has_no_message_id_and_must_name_itself(self):
        frame = protocol.make_event("esp32-main", protocol.EVENT_SENSOR, {"channel": 0})
        self.assertNotIn("message_id", frame)
        self.assertEqual(protocol.check_event(frame, "esp32-main"), [])
        self.assertTrue(protocol.is_event(frame))
        bad = protocol.make_event("esp32-main", "随便什么", {})
        self.assertTrue(protocol.check_event(bad, "esp32-main"))

    def test_an_unknown_type_is_not_treated_as_a_command(self):
        frame = protocol.make_request("esp32-main", "led.set", {})
        frame["type"] = "whatever"
        self.assertEqual(protocol.kind_of(frame), "")
        self.assertTrue(protocol.check_request(frame))

    def test_error_codes_come_from_one_table(self):
        reply = protocol.make_reply(
            "cmd-1", "esp32-main", protocol.STATUS_REJECTED,
            error="超出范围", error_code=protocol.ERROR_OUT_OF_RANGE,
        )
        self.assertEqual(protocol.check_reply(reply, "cmd-1"), [])
        reply["error_code"] = "made_up"
        self.assertTrue(protocol.check_reply(reply, "cmd-1"))

    def test_a_rejected_reply_must_explain_itself(self):
        reply = protocol.make_reply("cmd-1", "esp32-main", protocol.STATUS_REJECTED)
        self.assertTrue(protocol.check_reply(reply, "cmd-1"))
        reply["error"] = "设备没有这个能力"
        self.assertEqual(protocol.check_reply(reply, "cmd-1"), [])

    def test_a_completed_reply_carries_no_error(self):
        reply = protocol.make_reply(
            "cmd-1", "esp32-main", protocol.STATUS_COMPLETED, {}, "顺手写了个错"
        )
        self.assertTrue(protocol.check_reply(reply, "cmd-1"))
        reply = protocol.make_reply(
            "cmd-1", "esp32-main", protocol.STATUS_COMPLETED, {},
            error_code=protocol.ERROR_INTERNAL,
        )
        self.assertTrue(protocol.check_reply(reply, "cmd-1"))

    def test_rejected_counts_as_final(self):
        self.assertTrue(protocol.is_final(protocol.STATUS_REJECTED))
        self.assertIn(protocol.STATUS_REJECTED, protocol.STATUSES)


class SimulatorDedupTests(unittest.TestCase):
    """同一单重发不该执行两遍，也不该冒出两个不同结果。"""

    def setUp(self):
        self.sim = DeviceSimulator(device_id="esp32-sim", limits=dict(LIMITS))

    def send(self, command, params, message_id="cmd-fixed-1"):
        frame = protocol.make_request("esp32-sim", command, params, message_id)
        self.sim.handle(protocol.encode(frame))
        return protocol.decode(self.sim.read_all()[0])

    def test_the_same_message_id_gets_the_same_answer(self):
        first = self.send("led.set", {"channel": 0, "on": True})
        plays = self.sim.accepted
        second = self.send("led.set", {"channel": 0, "on": False})
        self.assertEqual(first["status"], protocol.STATUS_COMPLETED)
        self.assertEqual(second["status"], protocol.STATUS_COMPLETED)
        self.assertTrue(second["result"]["on"], "重发不该改结果，要还上次那一帧")
        self.assertEqual(self.sim.accepted, plays, "重发不该再执行一遍")

    def test_a_different_message_id_runs_again(self):
        self.send("led.set", {"channel": 0, "on": True}, message_id="cmd-a")
        second = self.send("led.set", {"channel": 0, "on": False}, message_id="cmd-b")
        self.assertFalse(second["result"]["on"])

    def test_the_dedup_table_does_not_grow_forever(self):
        for index in range(SEEN_LIMIT + 12):
            self.send("led.set", {"channel": 0, "on": True}, message_id="cmd-%03d" % index)
        self.assertLessEqual(len(self.sim.seen), SEEN_LIMIT)


class CapabilityTests(unittest.TestCase):
    def test_the_device_reports_its_own_capabilities(self):
        sim = DeviceSimulator(device_id="esp32-sim", limits=dict(LIMITS), version="fw-1.2.3")
        frame = protocol.make_request("esp32-sim", "device.get_capabilities", {})
        sim.handle(protocol.encode(frame))
        result = protocol.decode(sim.read_all()[0])["result"]
        self.assertEqual(result["protocol_version"], protocol.PROTOCOL_VERSION)
        self.assertEqual(result["firmware"], "fw-1.2.3")
        self.assertEqual(sorted(result["capabilities"]), sorted(CAPS))
        self.assertEqual(result["limits"]["audio_tracks"], ["boot", "confirm"])

    def test_capabilities_are_refused_without_the_status_capability(self):
        sim = DeviceSimulator(device_id="esp32-sim", capabilities=("led",), limits=dict(LIMITS))
        frame = protocol.make_request("esp32-sim", "device.get_capabilities", {})
        sim.handle(protocol.encode(frame))
        reply = protocol.decode(sim.read_all()[0])
        self.assertEqual(reply["status"], protocol.STATUS_REJECTED)
        self.assertEqual(reply["error_code"], protocol.ERROR_UNSUPPORTED)


class AudioTests(unittest.TestCase):
    """只能点已登记的曲目，音量范围听设备的。"""

    def setUp(self):
        self.sim = DeviceSimulator(device_id="esp32-sim", limits=dict(LIMITS))

    def send(self, params):
        frame = protocol.make_request("esp32-sim", "audio.play", params)
        self.sim.handle(protocol.encode(frame))
        return protocol.decode(self.sim.read_all()[0])

    def test_only_registered_tracks_are_accepted(self):
        good = self.send({"track_id": "boot"})
        self.assertEqual(good["status"], protocol.STATUS_COMPLETED)
        self.assertTrue(good["result"]["playing"])
        bad = self.send({"track_id": "../../etc/passwd"})
        self.assertEqual(bad["status"], protocol.STATUS_REJECTED)
        self.assertIn("只能是", bad["error"])

    def test_volume_range_comes_from_the_device(self):
        self.assertEqual(self.send({"track_id": "boot", "volume": 50})["status"], protocol.STATUS_COMPLETED)
        too_loud = self.send({"track_id": "boot", "volume": 200})
        self.assertEqual(too_loud["status"], protocol.STATUS_REJECTED)
        self.assertEqual(too_loud["error_code"], protocol.ERROR_OUT_OF_RANGE)

    def test_a_device_without_a_volume_range_still_plays(self):
        limits = dict(LIMITS)
        limits.pop("audio_volume")
        sim = DeviceSimulator(device_id="esp32-sim", limits=limits)
        frame = protocol.make_request("esp32-sim", "audio.play", {"track_id": "boot"})
        sim.handle(protocol.encode(frame))
        self.assertEqual(protocol.decode(sim.read_all()[0])["status"], protocol.STATUS_COMPLETED)
        frame = protocol.make_request("esp32-sim", "audio.play", {"track_id": "boot", "volume": 30})
        sim.handle(protocol.encode(frame))
        self.assertEqual(protocol.decode(sim.read_all()[0])["status"], protocol.STATUS_REJECTED)


class EventTests(unittest.TestCase):
    """设备主动上报：只有登记过的名字能发，收到就交给外面。"""

    def make_provider(self, seen):
        provider = LoopbackProvider(
            name="sim-1",
            capabilities=CAPS,
            limits=dict(LIMITS),
            on_event=lambda device, frame: seen.append((device, frame)),
        )
        provider.connect()
        return provider

    def test_unregistered_event_names_are_not_sent(self):
        sim = DeviceSimulator(device_id="esp32-sim")
        self.assertIsNone(sim.push_event("secret.exfiltrate", {"data": "x"}))
        self.assertEqual(sim.read_all(), [])

    def test_a_pushed_event_reaches_the_handler(self):
        seen = []
        provider = self.make_provider(seen)
        frame = protocol.make_event(provider.device_id, protocol.EVENT_BUTTON, {"index": 0})
        provider.transport.inbox.append(protocol.encode(frame))
        got = provider.poll()
        self.assertEqual(len(got), 1)
        self.assertEqual(seen[0][0], "sim-1")
        self.assertEqual(seen[0][1]["event"], protocol.EVENT_BUTTON)
        self.assertEqual(provider.stray, 0, "设备主动上报不该被算成串单")

    def test_a_malformed_event_is_counted_not_delivered(self):
        seen = []
        provider = self.make_provider(seen)
        bad = protocol.make_event(provider.device_id, protocol.EVENT_BUTTON, {})
        bad["event"] = "no-dots-allowed"
        provider.transport.inbox.append(protocol.encode(bad))
        self.assertEqual(provider.poll(), [])
        self.assertEqual(seen, [])
        self.assertEqual(provider.stray, 1)

    def test_an_event_does_not_disturb_the_pending_command(self):
        seen = []
        provider = self.make_provider(seen)
        provider.simulator.push_event(protocol.EVENT_STATE, {"led": 1})
        reply = provider.send("led.set", {"channel": 0, "on": True})
        self.assertTrue(reply["ok"], reply)
        self.assertEqual([item["event"] for _device, item in seen], [protocol.EVENT_STATE])


def make_tasks(provider, permissions=None, registry=None):
    manager = DeviceManager(allow_real=False)
    manager.register(provider)
    return DeviceTaskManager(
        devices=manager,
        registry=registry or DeviceCommandRegistry(),
        permissions=permissions
        or DevicePermissionService(permissions=PermissionManager(confirm_callback=lambda *a: True)),
    )


class PermissionTests(unittest.TestCase):
    """没授权就不动，撤销立刻生效，高风险动作多问一次。"""

    def make_provider(self):
        provider = LoopbackProvider(name="sim-1", capabilities=CAPS, limits=dict(LIMITS))
        provider.connect()
        return provider

    def test_without_a_permission_service_nothing_runs(self):
        provider = self.make_provider()
        manager = DeviceManager(allow_real=False)
        manager.register(provider)
        tasks = DeviceTaskManager(
            devices=manager,
            registry=DeviceCommandRegistry(),
            permissions=DevicePermissionService(permissions=None),
        )
        outcome = tasks.submit(device="sim-1", action="led.set", params={"channel": 0, "on": True})
        self.assertFalse(outcome["ok"])
        self.assertIn("权限", outcome["error"])

    def test_a_denied_grant_blocks_the_action(self):
        provider = self.make_provider()
        with tempfile.TemporaryDirectory() as tmp:
            service = DevicePermissionService(
                permissions=PermissionManager(
                    confirm_callback=lambda *a: True,
                    grants=GrantStore(Path(tmp) / "grants.json"),
                )
            )
            remembered = service.remember("sim-1", "led.set", mode=GrantMode.DENY.value)
            self.assertTrue(remembered["ok"], remembered)
            tasks = make_tasks(provider, permissions=service)
            outcome = tasks.submit(
                device="sim-1", action="led.set", params={"channel": 0, "on": True}
            )
            self.assertFalse(outcome["ok"])
            self.assertIn("拒绝", outcome["error"])

    def test_revoking_takes_effect_right_away(self):
        provider = self.make_provider()
        with tempfile.TemporaryDirectory() as tmp:
            service = DevicePermissionService(
                permissions=PermissionManager(
                    confirm_callback=lambda *a: False,
                    grants=GrantStore(Path(tmp) / "grants.json"),
                )
            )
            tasks = make_tasks(provider, permissions=service)
            denied = tasks.submit(
                device="sim-1", action="servo.set_angle", params={"channel": 0, "angle_deg": 90}
            )
            self.assertFalse(denied["ok"], "没有确认通道时舵机不该过")

            service.remember("sim-1", "servo.set_angle", mode=GrantMode.PERSISTENT.value)
            allowed = tasks.submit(
                device="sim-1", action="servo.set_angle", params={"channel": 0, "angle_deg": 90}
            )
            self.assertTrue(allowed["ok"], allowed)

            service.revoke("sim-1", "servo.set_angle")
            again = tasks.submit(
                device="sim-1", action="servo.set_angle", params={"channel": 0, "angle_deg": 90}
            )
            self.assertFalse(again["ok"], "撤销之后要立刻不放行")

    def test_low_risk_needs_no_confirmation_but_servo_does(self):
        provider = self.make_provider()
        service = DevicePermissionService(
            permissions=PermissionManager(confirm_callback=lambda *a: False)
        )
        spec = DeviceCommandRegistry().get("servo.set_angle")
        allowed, needs_confirmation, level, _why = service.check_action(spec, "sim-1")
        self.assertFalse(allowed)
        self.assertTrue(needs_confirmation)
        self.assertGreaterEqual(
            PermissionLevel(level).value, PermissionLevel.MEDIUM.value
        )
        led = DeviceCommandRegistry().get("led.set")
        allowed, needs_confirmation, _level, _why = service.check_action(led, "sim-1")
        self.assertTrue(allowed)
        self.assertFalse(needs_confirmation)

    def test_the_skill_cannot_bypass_the_permission_layer(self):
        provider = self.make_provider()
        service = DevicePermissionService(permissions=None)
        tasks = make_tasks(provider, permissions=service)
        outcome = tasks.submit(device="sim-1", action="device.stop", params={})
        self.assertFalse(outcome["ok"])
        self.assertIn("权限", outcome["error"])

    def test_an_unregistered_action_is_refused_before_anything_else(self):
        provider = self.make_provider()
        tasks = make_tasks(provider)
        outcome = tasks.submit(device="sim-1", action="gpio.write", params={"pin": 2, "value": 1})
        self.assertFalse(outcome["ok"])
        self.assertIn("没有登记", outcome["error"])


if __name__ == "__main__":
    unittest.main()
