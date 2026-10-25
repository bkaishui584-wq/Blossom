"""P2 验收：ESP-IDF 固件工程、串口枚举与打开、心跳与重连。

固件这份做静态核对：动作表、错误码、常量要和电脑端对得上。
串口不打开真实设备，只验证枚举形状、打开时的标记和失败路径。
"""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from devices import ports, protocol  # noqa: E402
from devices.actions import DeviceCommandRegistry  # noqa: E402
from devices.esp32 import SerialEsp32Transport  # noqa: E402
from devices.manager import DeviceManager  # noqa: E402
from devices.permissions import DevicePermissionService  # noqa: E402
from devices.provider import LoopbackProvider  # noqa: E402
from devices.stack import build_stack  # noqa: E402
from devices.tasks import DeviceTaskManager  # noqa: E402
from tools.permission import PermissionManager  # noqa: E402

FW = ROOT / "firmware" / "esp32"


class FirmwareSourceTests(unittest.TestCase):
    """固件源码和电脑端必须说的是同一套约定。"""

    @classmethod
    def setUpClass(cls):
        cls.commands = (FW / "main" / "commands.c").read_text(encoding="utf-8")
        cls.config = (FW / "main" / "device_config.h").read_text(encoding="utf-8")
        cls.app = (FW / "main" / "app_main.c").read_text(encoding="utf-8")
        cls.drivers = (FW / "main" / "drivers.c").read_text(encoding="utf-8")
        cls.sdkconfig = (FW / "sdkconfig.defaults").read_text(encoding="utf-8")
        cls.cmake = (FW / "CMakeLists.txt").read_text(encoding="utf-8")
        cls.main_cmake = (FW / "main" / "CMakeLists.txt").read_text(encoding="utf-8")

    def test_the_project_is_an_esp_idf_project(self):
        self.assertIn("idf", self.cmake.lower())
        self.assertIn("project(", self.cmake)
        self.assertIn("idf_component_register", self.main_cmake)
        for source in ("app_main.c", "commands.c", "drivers.c", "json_wire.c"):
            self.assertIn(source, self.main_cmake)

    def test_every_registered_action_is_in_the_firmware_table(self):
        table = re.findall(r'\{"([a-z_.]+)",\s*CAP_[A-Z]+,\s*do_', self.commands)
        self.assertEqual(sorted(table), sorted(DeviceCommandRegistry().names()))

    def test_the_firmware_rejects_by_default(self):
        self.assertIn("find_command(command)", self.commands)
        self.assertIn("entry == NULL", self.commands)
        self.assertIn("EC_UNKNOWN_COMMAND", self.commands)

    def test_error_codes_match_the_computer_side(self):
        defined = dict(re.findall(r'#define\s+(EC_[A-Z_]+)\s+"([a-z_]*)"', self.commands))
        values = {value for value in defined.values() if value}
        for code in protocol.ERROR_CODES:
            self.assertIn(code, values, code)
        self.assertEqual(defined.get("EC_NONE"), "")

    def test_the_line_limit_matches_the_computer_side(self):
        limit = re.search(r"#define\s+LINE_MAX\s+(\d+)", self.config)
        self.assertIsNotNone(limit)
        self.assertEqual(int(limit.group(1)), protocol.MAX_LINE_BYTES)

    def test_the_protocol_version_matches(self):
        version = re.search(r"#define\s+PROTOCOL_VERSION\s+(\d+)", self.config)
        self.assertIsNotNone(version)
        self.assertEqual(int(version.group(1)), protocol.PROTOCOL_VERSION)
        for status in ("completed", "rejected"):
            self.assertIn('"%s"' % status, self.commands, status)

    def test_the_command_layer_never_touches_pins(self):
        self.assertNotIn("gpio", self.commands.lower())
        self.assertIn("gpio_set_level", self.drivers)
        for needle in ("system(", "popen"):
            self.assertNotIn(needle, self.commands)

    def test_unknown_message_types_are_refused(self):
        self.assertIn("EC_BAD_TYPE", self.commands)
        self.assertIn('strcmp(kind, "command")', self.commands)

    def test_repeated_message_ids_are_deduped(self):
        self.assertIn("seen_lookup", self.commands)
        self.assertIn("seen_store", self.commands)

    def test_the_console_is_not_mixed_into_the_protocol_line(self):
        self.assertIn("CONFIG_ESP_CONSOLE_UART_NONE=y", self.sdkconfig)

    def test_the_firmware_validates_params_on_its_own(self):
        for needle in ("check_channel", "超出机械范围", "angle_deg 要是整数"):
            self.assertIn(needle, self.commands)

    def test_there_is_a_watchdog_and_a_safety_idle(self):
        self.assertIn("esp_task_wdt_add", self.app)
        self.assertIn("SAFETY_IDLE_MS", self.app)
        self.assertIn("drivers_safety_idle", self.app)

    def test_the_boot_announcement_carries_identity(self):
        self.assertIn("announce_boot", self.app)
        self.assertIn("FIRMWARE_VERSION", self.app)
        self.assertIn("commands_capabilities_json", self.app)

    def test_unverified_peripherals_default_to_simulated(self):
        for name in ("LED_KIND", "SERVO_KIND", "DISTANCE_KIND", "AUDIO_KIND"):
            match = re.search(r"#define\s+%s\s+(\d+)" % name, self.config)
            self.assertIsNotNone(match, name)
            self.assertEqual(int(match.group(1)), 0, "%s 默认必须是模拟驱动" % name)


class _FakeHandle:
    """假串口对象，形状照着 pyserial 来。"""

    def __init__(self, baud_set=True, non_blocking=True):
        self.written = []
        self.closed = False
        self.timeout = 0 if non_blocking else None
        self.baud_set = baud_set
        self.backend = "pyserial" if baud_set else "系统自带"
        self.non_blocking = non_blocking

    def write(self, payload):
        self.written.append(payload)
        return len(payload)

    def flush(self):
        return None

    def read(self, size):
        return b""

    def close(self):
        self.closed = True


class PortTests(unittest.TestCase):
    def test_listing_has_a_usable_shape(self):
        rows = ports.list_ports()
        self.assertIsInstance(rows, list)
        seen = set()
        for row in rows:
            self.assertTrue(row.get("port"), row)
            self.assertIn("description", row)
            self.assertIn("source", row)
            self.assertNotIn(row["port"].upper(), seen, "同一个口不该列两次")
            seen.add(row["port"].upper())
        self.assertIn(ports.backend_name(), ("pyserial", "系统自带"))

    def test_port_names_is_just_the_names(self):
        names = ports.port_names()
        self.assertEqual(names, [row["port"] for row in ports.list_ports()])

    def test_opening_without_a_port_name_is_refused(self):
        with self.assertRaises(ValueError):
            ports.open_port("")

    def test_opening_says_whether_the_baud_rate_was_applied(self):
        handle = _FakeHandle()
        transport = SerialEsp32Transport(
            port="COM9", baudrate=115200, opener=lambda port, baud: handle
        )
        result = transport.open()
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["baud_set"])
        self.assertTrue(result["non_blocking"])
        self.assertIn("115200", result["note"])
        transport.close()
        self.assertTrue(handle.closed)

    def test_a_fallback_opener_reports_that_baud_was_not_set(self):
        handle = _FakeHandle(baud_set=False)
        transport = SerialEsp32Transport(
            port="COM9", baudrate=115200, opener=lambda port, baud: handle
        )
        result = transport.open()
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["baud_set"])
        self.assertIn("沿用系统设置", result["note"])

    def test_an_old_single_argument_opener_still_works(self):
        handle = _FakeHandle()
        transport = SerialEsp32Transport(port="COM9", opener=lambda port: handle)
        self.assertTrue(transport.open()["ok"])


def sim_provider(name="sim-1"):
    provider = LoopbackProvider(
        name=name,
        capabilities=("status", "led", "servo", "sensor", "audio", "stop"),
        limits={
            "led_channels": {"min": 0, "max": 1},
            "servo_channels": {"min": 0, "max": 1},
            "servo_angle": {"min": 10, "max": 170},
            "sensor_channels": {"min": 0, "max": 0},
        },
    )
    provider.connect()
    return provider


class HeartbeatTests(unittest.TestCase):
    def test_a_living_device_answers_the_heartbeat(self):
        provider = sim_provider()
        beat = provider.heartbeat()
        self.assertTrue(beat["ok"], beat)
        self.assertEqual(provider.heartbeats, 1)
        self.assertTrue(provider.heartbeat_state()["ok"])

    def test_a_silent_device_is_reported_as_not_answering(self):
        provider = sim_provider()
        provider.simulator.silent_actions = {"device.get_status"}
        beat = provider.heartbeat(timeout=0.2)
        self.assertFalse(beat["ok"])
        self.assertEqual(beat["error_code"], protocol.ERROR_TIMEOUT)
        self.assertEqual(provider.heartbeat_failures, 1)

    def test_an_unplugged_device_is_not_pretended_to_be_alive(self):
        provider = sim_provider()
        provider.connected = False
        beat = provider.heartbeat()
        self.assertFalse(beat["ok"])
        self.assertEqual(beat["error"], "设备未连接")
        self.assertEqual(beat["error_code"], protocol.ERROR_NOT_CONNECTED)

    def test_heartbeat_state_shows_up_in_the_device_state(self):
        provider = sim_provider()
        provider.heartbeat()
        state = provider.get_state()
        self.assertTrue(state["heartbeat"]["ok"])
        self.assertEqual(state["heartbeat"]["beats"], 1)


class _BrokenProvider(LoopbackProvider):
    """打不开的传输，用来测重连失败。"""

    def _open_transport(self):
        return {"ok": False, "error": "端口被别的程序占了"}


class ReconnectTests(unittest.TestCase):
    def test_reconnect_comes_back_when_the_port_returns(self):
        provider = sim_provider()
        report = provider.reconnect(attempts=2, delay=0)
        self.assertTrue(report["ok"], report)
        self.assertEqual(provider.reconnects, 1)
        self.assertTrue(provider.connected)

    def test_reconnect_gives_up_after_a_bounded_number_of_tries(self):
        provider = _BrokenProvider(name="sim-9")
        report = provider.reconnect(attempts=2, delay=0)
        self.assertFalse(report["ok"])
        self.assertEqual(len(report["attempts"]), 2, "只试两次，不能无限重试")
        self.assertEqual(provider.reconnect_failures, 1)
        self.assertIn("端口", report["error"])


def make_tasks(**kwargs):
    manager = DeviceManager(allow_real=False)
    registry = DeviceCommandRegistry()
    permissions = DevicePermissionService(
        permissions=PermissionManager(confirm_callback=lambda *a: True)
    )
    return DeviceTaskManager(
        devices=manager, registry=registry, permissions=permissions, **kwargs
    ), manager


class TaskHeartbeatTests(unittest.TestCase):
    def test_only_connected_devices_are_asked(self):
        tasks, manager = make_tasks()
        manager.register(sim_provider("sim-1"))
        manager.register(LoopbackProvider(name="sim-2", capabilities=("status",)))
        rows = tasks.heartbeat_devices()
        self.assertEqual([row["device"] for row in rows], ["sim-1"])
        self.assertTrue(rows[0]["result"]["ok"])

    def test_the_interval_is_respected(self):
        tasks, manager = make_tasks(heartbeat_seconds=60.0)
        manager.register(sim_provider("sim-1"))
        self.assertEqual(tasks._maybe_heartbeat(), [], "还没到间隔，不该打扰设备")
        tasks._next_heartbeat = 0
        self.assertEqual(len(tasks._maybe_heartbeat()), 1)

    def test_heartbeat_is_off_when_the_interval_is_zero(self):
        tasks, manager = make_tasks(heartbeat_seconds=0.0)
        manager.register(sim_provider("sim-1"))
        self.assertEqual(tasks._maybe_heartbeat(), [])

    def test_the_stack_wires_device_events_to_the_bridge(self):
        stack = build_stack()
        self.assertTrue(callable(stack.event_handler))
        provider = stack.manager.get("sim-1")
        self.assertTrue(callable(provider.on_event))
        frame = protocol.make_event(provider.device_id, protocol.EVENT_BUTTON, {"index": 0})
        provider.transport.inbox.append(protocol.encode(frame))
        provider.poll()
        self.assertTrue(stack.bridge.recent(limit=5, kind="device.alert"))


if __name__ == "__main__":
    unittest.main()
