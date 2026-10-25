"""阶段 V5-C 验收：ESP32 真串口传输，真机默认仍然连不上。

这一版补上串口实现，但把三件事钉死：
    没配端口就不连，回落到内存回环；
    真串口设备不是模拟设备，allow_real 没打开时 DeviceManager 直接拒收；
    读不到数据就是读不到，返回空列表并把原因写进 health，绝不编传感器读数。
波特率沿用系统设置，这一层不改串口参数，也不假装改过。
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from devices.esp32 import (  # noqa: E402
    Esp32Adapter,
    LoopbackEsp32Transport,
    SerialEsp32Transport,
    build_transport,
    validate_command,
)
from devices.manifest import build_adapter, load_manifest, parse_manifest  # noqa: E402
from devices.manager import DeviceManager  # noqa: E402


class FakeStream:
    """串口替身：不动硬件，只记下发出去的字和要喂回来的字节。"""

    def __init__(self, non_blocking=True, chunks=None, fail_write=False):
        self.non_blocking = non_blocking
        self.written = []
        self.chunks = list(chunks or [])
        self.closed = False
        self.fail_write = fail_write

    def write(self, data):
        if self.fail_write:
            raise OSError("串口掉了")
        self.written.append(data)
        return len(data)

    def flush(self):
        return None

    def read(self, size):
        return self.chunks.pop(0) if self.chunks else b""

    def close(self):
        self.closed = True


def transport_with(stream, port="COM7", **kwargs):
    return SerialEsp32Transport(port=port, opener=lambda name: stream, **kwargs)


class PortConfigurationTests(unittest.TestCase):
    def test_serial_without_port_refuses_to_open(self):
        result = SerialEsp32Transport().open()
        self.assertFalse(result["ok"])
        self.assertIn("串口名", result["error"])

    def test_build_transport_without_port_stays_simulated(self):
        transport = build_transport({})
        self.assertIsInstance(transport, LoopbackEsp32Transport)
        self.assertTrue(transport.is_simulated)

    def test_build_transport_with_port_is_real(self):
        transport = build_transport({"port": "COM7", "baudrate": 9600})
        self.assertIsInstance(transport, SerialEsp32Transport)
        self.assertFalse(transport.is_simulated)
        self.assertEqual(transport.baudrate, 9600)

    def test_bad_baudrate_falls_back_instead_of_crashing(self):
        self.assertEqual(build_transport({"port": "COM7", "baudrate": "快"}).baudrate, 115200)

    def test_open_failure_is_reported_with_the_port_name(self):
        def boom(name):
            raise OSError("拒绝访问")

        transport = SerialEsp32Transport(port="COM9", opener=boom)
        result = transport.open()
        self.assertFalse(result["ok"])
        self.assertIn("COM9", result["error"])
        self.assertEqual(transport.health()["errors"], 1)


class SerialIoUnitTests(unittest.TestCase):
    def test_write_sends_one_line_with_newline(self):
        stream = FakeStream()
        transport = transport_with(stream)
        self.assertTrue(transport.open()["ok"])
        result = transport.write('{"cmd": "led", "on": true}')
        self.assertTrue(result["ok"])
        self.assertEqual(stream.written, [b'{"cmd": "led", "on": true}\n'])
        self.assertEqual(result["count"], 1)

    def test_write_before_open_is_refused(self):
        transport = transport_with(FakeStream())
        self.assertFalse(transport.write("x")["ok"])

    def test_write_failure_is_reported_and_counted(self):
        transport = transport_with(FakeStream(fail_write=True))
        transport.open()
        result = transport.write("x")
        self.assertFalse(result["ok"])
        self.assertIn("写串口失败", result["error"])
        self.assertEqual(transport.health()["errors"], 1)

    def test_read_returns_complete_lines_only(self):
        stream = FakeStream(chunks=[b'{"distance": 12}\n{"dist'])
        transport = transport_with(stream)
        transport.open()
        self.assertEqual(transport.read(), ['{"distance": 12}'])
        self.assertEqual(transport.read(), [])

    def test_read_joins_split_lines(self):
        stream = FakeStream(chunks=[b"hal", b"f\n"])
        transport = transport_with(stream)
        transport.open()
        transport.read()
        self.assertIn("half", transport.read())

    def test_blocking_stream_is_not_read_and_says_why(self):
        stream = FakeStream(non_blocking=False, chunks=[b"data\n"])
        transport = transport_with(stream)
        transport.open()
        self.assertEqual(transport.read(), [])
        self.assertIn("非阻塞", transport.health()["note"])

    def test_no_data_is_not_an_error(self):
        transport = transport_with(FakeStream())
        transport.open()
        self.assertEqual(transport.read(), [])
        self.assertEqual(transport.health()["errors"], 0)

    def test_close_is_idempotent(self):
        stream = FakeStream()
        transport = transport_with(stream)
        transport.open()
        self.assertTrue(transport.close()["ok"])
        self.assertTrue(stream.closed)
        self.assertTrue(transport.close()["ok"])

    def test_health_reports_the_truth(self):
        transport = transport_with(FakeStream(chunks=[b"ok\n"]))
        transport.open()
        transport.write("x")
        transport.read()
        health = transport.health()
        self.assertEqual(health["port"], "COM7")
        self.assertTrue(health["open"])
        self.assertEqual(health["writes"], 1)
        self.assertEqual(health["lines_read"], 1)
        self.assertEqual(health["errors"], 0)


class AdapterTests(unittest.TestCase):
    def test_adapter_keeps_loopback_as_mock(self):
        self.assertTrue(Esp32Adapter().is_mock)

    def test_serial_adapter_is_not_mock(self):
        adapter = Esp32Adapter(transport=transport_with(FakeStream()))
        self.assertFalse(adapter.is_mock)

    def test_state_carries_transport_health(self):
        adapter = Esp32Adapter(transport=transport_with(FakeStream()))
        adapter.connect()
        state = adapter.get_state()
        self.assertEqual(state["health"]["port"], "COM7")
        self.assertTrue(state["health"]["open"])

    def test_loopback_state_has_no_health_block(self):
        adapter = Esp32Adapter()
        self.assertIsNone(adapter.get_state()["health"])

    def test_commands_are_still_validated_before_going_out(self):
        stream = FakeStream()
        adapter = Esp32Adapter(transport=transport_with(stream))
        adapter.connect()
        bad = adapter.execute({"cmd": "servo", "angle": 400})
        self.assertFalse(bad["ok"])
        self.assertEqual(stream.written, [])
        good = adapter.execute({"cmd": "servo", "angle": 90})
        self.assertTrue(good["ok"])
        self.assertEqual(len(stream.written), 1)

    def test_disconnected_adapter_refuses_commands(self):
        adapter = Esp32Adapter(transport=transport_with(FakeStream()))
        self.assertFalse(adapter.execute({"cmd": "led", "on": True})["ok"])

    def test_validate_command_still_rejects_unknown_capability(self):
        line, error = validate_command({"cmd": "launch"})
        self.assertIsNone(line)
        self.assertIn("不支持", error)


class DeviceManagerGatingTests(unittest.TestCase):
    def test_real_serial_device_is_refused_without_allow_real(self):
        manager = DeviceManager()
        outcome = manager.register(Esp32Adapter(transport=transport_with(FakeStream())))
        self.assertFalse(outcome["ok"])
        self.assertIn("禁止接入真实设备", outcome["error"])
        self.assertEqual(manager.names(), [])

    def test_real_serial_device_registers_once_allowed(self):
        manager = DeviceManager(allow_real=True)
        outcome = manager.register(Esp32Adapter(transport=transport_with(FakeStream())))
        self.assertTrue(outcome["ok"])

    def test_loopback_device_registers_without_allow_real(self):
        manager = DeviceManager()
        self.assertTrue(manager.register(Esp32Adapter())["ok"])

    def test_manager_connect_and_state_pass_through(self):
        manager = DeviceManager(allow_real=True)
        manager.register(Esp32Adapter(name="arm", transport=transport_with(FakeStream())))
        self.assertTrue(manager.connect("arm")["ok"])
        state = manager.get_state("arm")["device"]
        self.assertTrue(state["connected"])
        self.assertEqual(state["health"]["port"], "COM7")


class ManifestWiringTests(unittest.TestCase):
    def test_manifest_without_port_builds_a_loopback_device(self):
        specs, errors = parse_manifest(
            {"devices": [{"name": "桌宠手臂", "kind": "esp32"}]}
        )
        self.assertEqual(errors, [])
        adapter = build_adapter(specs[0])
        self.assertTrue(adapter.is_mock)

    def test_manifest_with_port_builds_a_serial_device(self):
        specs, _ = parse_manifest(
            {
                "devices": [
                    {
                        "name": "桌宠手臂",
                        "kind": "esp32",
                        "options": {"port": "COM4", "baudrate": 115200},
                    }
                ]
            }
        )
        adapter = build_adapter(specs[0])
        self.assertFalse(adapter.is_mock)
        self.assertEqual(adapter.transport.port, "COM4")

    def test_manifest_file_is_read_from_the_devices_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "devices.json"
            target.write_text(
                '{"devices": [{"name": "手臂", "kind": "esp32", "options": {"port": "COM2"}}]}',
                encoding="utf-8",
            )
            specs, errors = load_manifest(target)
        self.assertEqual(errors, [])
        self.assertEqual(specs[0].options["port"], "COM2")

    def test_manifest_devices_are_not_auto_connected_by_default(self):
        specs, _ = parse_manifest({"devices": [{"name": "手臂", "kind": "esp32"}]})
        self.assertFalse(specs[0].auto_connect)


if __name__ == "__main__":
    unittest.main()