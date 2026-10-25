"""P3-2：ESP32 适配层。当前只跑模拟传输，真硬件不允许注册。"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from devices.esp32 import (
    CAPABILITIES,
    DIRECTIONS,
    Esp32Adapter,
    Esp32Transport,
    LoopbackEsp32Transport,
    validate_command,
)
from devices.interface import MockDevice
from devices.manager import DeviceManager


class FakeAudit:
    def __init__(self):
        self.rows = []

    def record(self, **kwargs):
        self.rows.append(kwargs)
        return kwargs


class RealTransport(Esp32Transport):
    """假装是真硬件：名字像真的，也不是模拟传输。"""

    name = "usb"

    def open(self):
        return {"ok": True}

    def close(self):
        return {"ok": True}

    def write(self, line):
        return {"ok": True, "sent": line}

    def read(self):
        return []


class BrokenTransport(Esp32Transport):
    name = "broken"

    def open(self):
        raise RuntimeError("端口被占用了")

    def close(self):
        raise RuntimeError("已经断了")

    def write(self, line):
        raise RuntimeError("写不进去")

    def read(self):
        raise RuntimeError("读不出来")


def manager(**kwargs):
    audit = FakeAudit()
    return DeviceManager(audit=audit, **kwargs), audit


class ValidateTests(unittest.TestCase):
    def test_only_dicts_pass(self):
        for bad in (None, "", [], 5):
            line, error = validate_command(bad)
            self.assertIsNone(line)
            self.assertTrue(error)

    def test_unknown_command_is_rejected(self):
        line, error = validate_command({"cmd": "self_destruct"})
        self.assertIsNone(line)
        self.assertIn("self_destruct", error)

    def test_servo_needs_a_real_angle(self):
        self.assertTrue(validate_command({"cmd": "servo", "angle": 90})[1] == "")
        self.assertTrue(validate_command({"cmd": "servo", "angle": "45"})[1] == "")
        self.assertTrue(validate_command({"cmd": "servo", "angle": 900})[1])
        self.assertTrue(validate_command({"cmd": "servo", "angle": "右"})[1])
        self.assertTrue(validate_command({"cmd": "servo"})[1])

    def test_led_is_a_switch(self):
        line, error = validate_command({"cmd": "led", "on": True})
        self.assertEqual(error, "")
        self.assertEqual(json.loads(line), {"cmd": "led", "on": True})
        self.assertEqual(json.loads(validate_command({"cmd": "led"})[0])["on"], False)

    def test_move_needs_a_known_direction(self):
        self.assertTrue(validate_command({"cmd": "move", "direction": "sideways"})[1])
        line, error = validate_command({"cmd": "move", "direction": "forward", "speed": 30})
        self.assertEqual(error, "")
        self.assertEqual(json.loads(line), {"cmd": "move", "direction": "forward", "speed": 30})
        for direction in DIRECTIONS:
            self.assertEqual(validate_command({"cmd": "move", "direction": direction})[1], "")

    def test_move_speed_is_clamped(self):
        self.assertEqual(
            json.loads(validate_command({"cmd": "move", "direction": "back", "speed": 200})[0])["speed"],
            100,
        )
        self.assertEqual(
            json.loads(validate_command({"cmd": "move", "direction": "back", "speed": -5})[0])["speed"],
            0,
        )
        self.assertTrue(validate_command({"cmd": "move", "direction": "back", "speed": "快"})[1])

    def test_distance_has_no_extra_fields(self):
        self.assertEqual(json.loads(validate_command({"cmd": "distance"})[0]), {"cmd": "distance"})

    def test_shell_words_never_pass(self):
        for command in ({"cmd": "rm -rf /"}, {"cmd": "servo; shutdown"}, {"cmd": "exec"}):
            self.assertIsNotNone(validate_command(command)[1])

    def test_every_capability_is_known(self):
        self.assertEqual(sorted(CAPABILITIES), ["distance", "led", "move", "servo"])


class TransportTests(unittest.TestCase):
    def test_loopback_is_simulated(self):
        transport = LoopbackEsp32Transport()
        self.assertTrue(transport.is_simulated)
        self.assertEqual(transport.name, "loopback")
        self.assertFalse(transport.write('{"cmd":"led","on":true}')["ok"])
        transport.open()
        sent = transport.write('{"cmd":"led","on":true}')
        self.assertTrue(sent["ok"])
        self.assertEqual(transport.sent, ['{"cmd":"led","on":true}'])
        self.assertEqual(transport.read(), [])
        transport.push('{"distance_cm": 12}')
        self.assertEqual(transport.read(), ['{"distance_cm": 12}'])


class AdapterTests(unittest.TestCase):
    def test_registers_as_a_mock_device(self):
        devices, _ = manager()
        result = devices.register(Esp32Adapter())
        self.assertTrue(result["ok"])
        self.assertTrue(result["mock"])
        self.assertEqual(devices.names(), ["esp32-1"])

    def test_real_transport_is_refused_by_default(self):
        devices, audit = manager()
        result = devices.register(Esp32Adapter(transport=RealTransport()))
        self.assertFalse(result["ok"])
        self.assertIn("禁止接入真实设备", result["error"])
        self.assertEqual(devices.names(), [])
        self.assertEqual(audit.rows, [])

    def test_real_transport_needs_an_explicit_switch(self):
        devices, _ = manager(allow_real=True)
        self.assertTrue(devices.register(Esp32Adapter(transport=RealTransport()))["ok"])

    def test_commands_need_a_connection(self):
        adapter = Esp32Adapter()
        result = adapter.execute({"cmd": "led", "on": True})
        self.assertFalse(result["ok"])
        self.assertIn("未连接", result["error"])

    def test_connect_then_servo(self):
        adapter = Esp32Adapter()
        self.assertTrue(adapter.connect()["ok"])
        result = adapter.execute({"cmd": "servo", "angle": 90})
        self.assertTrue(result["ok"])
        self.assertTrue(result["simulated"])
        self.assertEqual(json.loads(result["sent"]), {"cmd": "servo", "angle": 90})
        self.assertEqual(result["command_count"], 1)
        state = adapter.get_state()
        self.assertEqual(state["kind"], "esp32")
        self.assertEqual(state["capabilities"], list(CAPABILITIES))
        self.assertEqual(state["last_command"], {"cmd": "servo", "angle": 90})
        self.assertTrue(state["connected"])

    def test_bad_command_never_reaches_the_device(self):
        adapter = Esp32Adapter()
        adapter.connect()
        result = adapter.execute({"cmd": "arm_the_missiles"})
        self.assertFalse(result["ok"])
        self.assertEqual(adapter.transport.sent, [])
        self.assertEqual(adapter.commands, [])

    def test_disconnect_blocks_commands(self):
        adapter = Esp32Adapter()
        adapter.connect()
        self.assertTrue(adapter.disconnect()["ok"])
        self.assertFalse(adapter.execute({"cmd": "led", "on": True})["ok"])
        self.assertFalse(adapter.get_state()["connected"])

    def test_distance_is_never_invented(self):
        adapter = Esp32Adapter()
        adapter.connect()
        unknown = adapter.execute({"cmd": "distance"})
        self.assertIsNone(unknown["distance_cm"])
        self.assertIn("没有回报", unknown["note"])
        adapter.transport.push('{"distance_cm": 42}')
        self.assertEqual(adapter.execute({"cmd": "distance"})["distance_cm"], 42)
        adapter.transport.push("not json at all")
        self.assertEqual(adapter.get_state()["last_reading"], {"distance_cm": 42})

    def test_transport_failures_are_contained(self):
        adapter = Esp32Adapter(transport=BrokenTransport())
        self.assertFalse(adapter.connect()["ok"])
        self.assertFalse(adapter.execute({"cmd": "led", "on": True})["ok"])
        self.assertFalse(adapter.disconnect()["ok"])
        self.assertEqual(adapter.get_state()["command_count"], 0)

    def test_manager_audits_every_command(self):
        devices, audit = manager()
        devices.register(Esp32Adapter())
        devices.connect("esp32-1")
        devices.execute("esp32-1", {"cmd": "led", "on": True})
        devices.execute("esp32-1", {"cmd": "nope"})
        kinds = [row["kind"] for row in audit.rows]
        self.assertIn("device_ok", kinds)
        self.assertIn("device_failed", kinds)
        self.assertTrue(all(row["level"] == "DEVICE" for row in audit.rows))

    def test_mock_device_and_esp32_live_side_by_side(self):
        devices, _ = manager()
        devices.register(MockDevice(name="mock-1", capabilities=("servo",)))
        devices.register(Esp32Adapter())
        self.assertEqual(devices.names(), ["esp32-1", "mock-1"])


class BoundaryTests(unittest.TestCase):
    def test_no_hardware_library_is_imported(self):
        lines = (ROOT / "devices" / "esp32.py").read_text(encoding="utf-8").splitlines()
        allowed = ("import json", "import os", "from abc", "import abc", "from devices")
        for line in lines:
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                self.assertTrue(stripped.startswith(allowed), stripped)

    def test_agent_core_stays_out_of_hardware(self):
        text = (ROOT / "agent" / "core.py").read_text(encoding="utf-8")
        for word in ("esp32", "servo", "Esp32Adapter", "serial"):
            self.assertNotIn(word, text)

    def test_app_registers_the_adapter(self):
        main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("from devices.esp32 import Esp32Adapter", main)
        self.assertIn("devices.register(Esp32Adapter(logger=logger))", main)

    def test_tool_tells_the_model_the_real_command_shape(self):
        text = (ROOT / "tools" / "builtin.py").read_text(encoding="utf-8")
        start = text.index("def send_device_command_tool")
        block = text[start : text.index("def register_default_tools")]
        self.assertIn('\\"cmd\\": \\"servo\\"', block)
        self.assertIn("舵机", block)
        self.assertIn('permission="LOW"', block)


if __name__ == "__main__":
    unittest.main()
