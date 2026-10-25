"""P3-3：实体身体。情绪和视觉事件转成设备动作，全部经过 DeviceManager。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from behavior import actions as actions_module
from behavior.body import REACTIVE_KINDS, SIGNALS, VISION_REACTIONS, PhysicalBody
from devices.interface import DeviceAdapter, MockDevice
from devices.manager import DeviceManager
from events import bus as bus_module
from events.bus import EventBus


class FakeLogger:
    def __init__(self):
        self.warnings = []

    def warning(self, message, *args):
        self.warnings.append(message % args if args else message)


class FailingDevice(DeviceAdapter):
    is_mock = True

    def __init__(self, name="failing-1"):
        self.name = name
        self.connected = True

    def connect(self):
        self.connected = True
        return {"ok": True}

    def disconnect(self):
        self.connected = False
        return {"ok": True}

    def get_state(self):
        return {"device": self.name, "connected": self.connected}

    def execute(self, command):
        return {"ok": False, "device": self.name, "error": "舵机卡住了"}


class NoAdapterDevice(DeviceAdapter):
    is_mock = True

    def __init__(self, name="angry-1"):
        self.name = name
        self.connected = True

    def connect(self):
        return {"ok": True}

    def disconnect(self):
        return {"ok": True}

    def get_state(self):
        return {"device": self.name, "connected": True}

    def execute(self, command):
        raise RuntimeError("线松了")


def with_mock():
    devices = DeviceManager()
    devices.register(MockDevice(name="mock-1", capabilities=("servo", "led", "move")))
    return devices, devices.get("mock-1")


class IdleTests(unittest.TestCase):
    def test_without_devices_nothing_happens(self):
        body = PhysicalBody(devices=None)
        result = body.express("happy")
        self.assertFalse(result["ok"])
        self.assertIn("没有", result["error"])
        self.assertFalse(body.available())
        self.assertEqual(body.sent_count, 0)
        self.assertEqual(body.skipped_count, 1)

    def test_unknown_emotion_is_skipped(self):
        devices, adapter = with_mock()
        body = PhysicalBody(devices=devices)
        result = body.express("mysterious")
        self.assertFalse(result["ok"])
        self.assertIn("没有给", result["error"])
        self.assertEqual(adapter.commands, [])
        self.assertEqual(body.skipped_count, 1)

    def test_disabled_body_stays_off(self):
        devices, adapter = with_mock()
        body = PhysicalBody(devices=devices, enabled=False)
        result = body.express("happy")
        self.assertFalse(result["ok"])
        self.assertIn("没有打开", result["error"])
        self.assertEqual(adapter.commands, [])
        self.assertEqual(adapter.connected, False)

    def test_every_signal_is_a_known_shape(self):
        for emotion, command in SIGNALS.items():
            self.assertIn("cmd", command, emotion)


class SignalTests(unittest.TestCase):
    def test_happy_turns_the_light_on(self):
        devices, adapter = with_mock()
        body = PhysicalBody(devices=devices)
        result = body.express("happy", reason="用户点了它一下")
        self.assertTrue(result["ok"])
        self.assertEqual(adapter.last_command, {"cmd": "led", "on": True})
        self.assertEqual(body.sent_count, 1)
        self.assertEqual(body.last_signal["reason"], "用户点了它一下")
        self.assertEqual(body.last_signal["device"], "mock-1")

    def test_excited_moves_the_body(self):
        devices, adapter = with_mock()
        body = PhysicalBody(devices=devices)
        body.express("excited")
        self.assertEqual(adapter.last_command["cmd"], "move")

    def test_device_failure_is_reported(self):
        devices = DeviceManager()
        devices.register(FailingDevice())
        body = PhysicalBody(devices=devices)
        result = body.express("happy")
        self.assertFalse(result["ok"])
        self.assertIn("舵机卡住了", result["error"])
        self.assertEqual(body.failed_count, 1)
        self.assertEqual(body.sent_count, 0)

    def test_broken_device_is_contained(self):
        devices = DeviceManager()
        devices.register(NoAdapterDevice())
        logger = FakeLogger()
        body = PhysicalBody(devices=devices, logger=logger)
        result = body.express("happy")
        self.assertFalse(result["ok"])
        self.assertEqual(body.failed_count, 1)

    def test_named_device_is_used(self):
        devices, _ = with_mock()
        devices.register(MockDevice(name="second-1", capabilities=("led",)))
        body = PhysicalBody(devices=devices, name="second-1")
        body.express("happy")
        self.assertEqual(devices.get("second-1").commands, [{"cmd": "led", "on": True}])
        self.assertEqual(body.describe()["device"], "second-1")

    def test_missing_named_device_is_skipped(self):
        devices, adapter = with_mock()
        body = PhysicalBody(devices=devices, name="ghost-1")
        self.assertFalse(body.express("happy")["ok"])
        self.assertEqual(adapter.commands, [])


class EventTests(unittest.TestCase):
    def test_speaking_moves_the_body(self):
        devices, adapter = with_mock()
        body = PhysicalBody(devices=devices)
        action = actions_module.speak_action("我在这儿", emotion="happy")
        body.on_behavior(action.to_dict())
        self.assertEqual(adapter.last_command, {"cmd": "led", "on": True})

    def test_small_idle_motions_do_not_bother_the_device(self):
        devices, adapter = with_mock()
        body = PhysicalBody(devices=devices)
        for motion in actions_module.IDLE_MOTIONS:
            self.assertIsNone(body.on_behavior(motion.to_dict()))
        self.assertEqual(adapter.commands, [])
        self.assertNotIn(actions_module.IDLE, REACTIVE_KINDS)

    def test_sleep_is_tired(self):
        devices, adapter = with_mock()
        body = PhysicalBody(devices=devices)
        body.on_behavior(actions_module.SLEEP_ACTION.to_dict())
        self.assertEqual(adapter.last_command, {"cmd": "led", "on": False})

    def test_seeing_a_person_makes_it_happy(self):
        devices, adapter = with_mock()
        body = PhysicalBody(devices=devices)
        body.on_detection({"label": "person", "confidence": 0.9})
        self.assertEqual(adapter.last_command["cmd"], "led")
        self.assertIn("person", VISION_REACTIONS)

    def test_unknown_label_is_ignored(self):
        devices, adapter = with_mock()
        body = PhysicalBody(devices=devices)
        self.assertIsNone(body.on_detection({"label": "spaceship"}))
        self.assertEqual(adapter.commands, [])

    def test_losing_sight_returns_to_neutral(self):
        devices, adapter = with_mock()
        body = PhysicalBody(devices=devices)
        body.on_cleared({})
        self.assertEqual(adapter.last_command, SIGNALS["neutral"])

    def test_malformed_payload_is_ignored(self):
        devices, adapter = with_mock()
        body = PhysicalBody(devices=devices)
        self.assertIsNone(body.on_behavior("not a payload"))
        self.assertEqual(adapter.commands, [])

    def test_bus_wiring_moves_the_body(self):
        devices, adapter = with_mock()
        bus = EventBus()
        body = PhysicalBody(devices=devices)
        self.assertEqual(len(body.attach(bus)), 3)
        bus.emit(
            bus_module.BEHAVIOR,
            **actions_module.interaction_action("click").to_dict(),
        )
        self.assertEqual(adapter.last_command["cmd"], "led")
        bus.emit(bus_module.VISION_DETECTED, label="cat", confidence=0.9)
        self.assertEqual(adapter.last_command["cmd"], "move")

    def test_attach_without_bus_is_safe(self):
        body = PhysicalBody(devices=None)
        self.assertEqual(body.attach(None), [])


class DescribeTests(unittest.TestCase):
    def test_describe_is_for_the_dev_panel(self):
        devices, _ = with_mock()
        body = PhysicalBody(devices=devices)
        body.express("thinking")
        info = body.describe()
        self.assertEqual(info["device"], "mock-1")
        self.assertTrue(info["enabled"])
        self.assertEqual(info["sent"], 1)
        self.assertIn("thinking", info["emotions"])
        self.assertEqual(info["last_signal"]["emotion"], "thinking")


class BoundaryTests(unittest.TestCase):
    def test_body_only_talks_to_the_manager(self):
        text = (ROOT / "behavior" / "body.py").read_text(encoding="utf-8")
        self.assertIn("self.devices.execute", text)
        for word in ("transport", "esp32", "serial", "provider.chat", "ModelProvider"):
            self.assertNotIn(word, text)

    def test_agent_core_does_not_know_the_body(self):
        text = (ROOT / "agent" / "core.py").read_text(encoding="utf-8")
        for word in ("PhysicalBody", "behavior.body"):
            self.assertNotIn(word, text)

    def test_desktop_wires_the_body(self):
        main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("PhysicalBody(devices=agent.devices, logger=logger)", main)
        self.assertIn("body.attach(bus)", main)
        desktop = (ROOT / "ui" / "desktop.py").read_text(encoding="utf-8")
        self.assertIn("body=body", desktop)
        self.assertIn("实体身体", desktop)


if __name__ == "__main__":
    unittest.main()
