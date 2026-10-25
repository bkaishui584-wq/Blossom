"""P6 设备接入层落地：用户自己的设备清单会被发现、校验、注册，真实传输端口仍留空。"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from devices.esp32 import Esp32Adapter, Esp32Transport
from devices.interface import MockDevice
from devices.manager import DEFAULT_STALE_SECONDS, DeviceManager
from devices.manifest import (
    KINDS,
    MANIFEST_NAME,
    MAX_DEVICES,
    DeviceSpec,
    build_adapter,
    find_manifest,
    install,
    load_into,
    load_manifest,
    parse_manifest,
)
from security.config import Config


class FakeLogger:
    def __init__(self):
        self.warnings = []
        self.infos = []

    def warning(self, message, *args):
        self.warnings.append(message % args if args else message)

    def info(self, message, *args):
        self.infos.append(message % args if args else message)


class FakeRealTransport(Esp32Transport):
    """假装是一个真实传输（不是模拟），用来验证真实硬件仍然被拒。"""

    name = "real-fake"
    is_simulated = False

    def open(self):
        return {"ok": True}

    def close(self):
        return {"ok": True}

    def write(self, line):
        return {"ok": True, "sent": line}

    def read(self):
        return []


class TempDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write_manifest(self, data, name=MANIFEST_NAME):
        path = self.root / name
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        path.write_text(text, encoding="utf-8")
        return path


class FindTests(TempDir):
    def test_empty_root_is_not_an_error(self):
        self.assertEqual(find_manifest(""), "")
        self.assertEqual(find_manifest(None), "")

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(find_manifest(self.root), "")

    def test_file_is_found(self):
        path = self.write_manifest({"devices": []})
        self.assertEqual(find_manifest(self.root), str(path))


class ParseTests(unittest.TestCase):
    def test_bare_list_is_accepted(self):
        specs, errors = parse_manifest([{"name": "a"}])
        self.assertEqual(errors, [])
        self.assertEqual([spec.name for spec in specs], ["a"])

    def test_dict_wrapper_is_accepted(self):
        specs, errors = parse_manifest({"devices": [{"name": "a", "kind": "esp32"}]})
        self.assertEqual(errors, [])
        self.assertEqual(specs[0].kind, "esp32")

    def test_missing_devices_list(self):
        specs, errors = parse_manifest({"other": 1})
        self.assertEqual(specs, [])
        self.assertEqual(len(errors), 1)

    def test_devices_must_be_a_list(self):
        specs, errors = parse_manifest({"devices": "nope"})
        self.assertEqual(specs, [])
        self.assertEqual(len(errors), 1)

    def test_default_kind_is_mock(self):
        specs, _ = parse_manifest([{"name": "a"}])
        self.assertEqual(specs[0].kind, "mock")
        self.assertFalse(specs[0].auto_connect)
        self.assertEqual(specs[0].capabilities, ())

    def test_broken_entries_are_skipped_one_by_one(self):
        specs, errors = parse_manifest(
            [
                "not an object",
                {"name": "  "},
                {"name": "a", "kind": "spaceship"},
                {"name": "ok"},
                {"name": "ok"},
            ]
        )
        self.assertEqual([spec.name for spec in specs], ["ok"])
        self.assertEqual(len(errors), 4)

    def test_fields_are_cleaned(self):
        specs, _ = parse_manifest(
            [{"name": " 小舵机 ", "kind": " ESP32 ", "capabilities": ["servo", ""], "options": 5}]
        )
        spec = specs[0]
        self.assertEqual(spec.name, "小舵机")
        self.assertEqual(spec.kind, "esp32")
        self.assertEqual(spec.capabilities, ("servo",))
        self.assertEqual(spec.options, {})

    def test_auto_connect_is_read(self):
        specs, _ = parse_manifest([{"name": "a", "auto_connect": True}])
        self.assertTrue(specs[0].auto_connect)

    def test_too_many_devices_are_capped(self):
        raw = [{"name": "d%d" % i} for i in range(MAX_DEVICES + 5)]
        specs, errors = parse_manifest(raw)
        self.assertEqual(len(specs), MAX_DEVICES)
        self.assertTrue(errors)

    def test_spec_to_dict_round_trips(self):
        spec = DeviceSpec(name="a", kind="mock", capabilities=("servo",), auto_connect=True)
        view = spec.to_dict()
        self.assertEqual(view["capabilities"], ["servo"])
        self.assertTrue(view["auto_connect"])


class LoadTests(TempDir):
    def test_missing_path_is_not_an_error(self):
        self.assertEqual(load_manifest(""), ([], []))
        self.assertEqual(load_manifest(self.root / "nope.json"), ([], []))

    def test_broken_json_reports_and_does_not_crash(self):
        path = self.write_manifest("{ this is not json")
        specs, errors = load_manifest(path)
        self.assertEqual(specs, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("JSON", errors[0])

    def test_good_file_is_loaded(self):
        path = self.write_manifest({"devices": [{"name": "a", "kind": "esp32"}]})
        specs, errors = load_manifest(path)
        self.assertEqual(errors, [])
        self.assertEqual(specs[0].kind, "esp32")


class BuildTests(unittest.TestCase):
    def test_mock_device_is_built(self):
        adapter = build_adapter(DeviceSpec(name="m", kind="mock", capabilities=("servo",)))
        self.assertIsInstance(adapter, MockDevice)
        self.assertEqual(adapter.name, "m")
        self.assertEqual(adapter.capabilities, ("servo",))

    def test_esp32_device_is_built(self):
        adapter = build_adapter(DeviceSpec(name="e", kind="esp32"))
        self.assertIsInstance(adapter, Esp32Adapter)
        self.assertEqual(adapter.name, "e")

    def test_unknown_kind_refuses(self):
        with self.assertRaises(ValueError):
            build_adapter(DeviceSpec(name="x", kind="spaceship"))

    # 老的 build_adapter 只造它原来那两种。wifi 是 V11 新加的，由
    # devices/provider.py:build_provider 造，覆盖在 test_p99。
    LEGACY_KINDS = ("mock", "esp32")

    def test_the_legacy_builder_only_makes_simulated_devices(self):
        # 没写在清单里的东西这条老路造不出来；造出来的也都是模拟的。
        for kind in self.LEGACY_KINDS:
            adapter = build_adapter(DeviceSpec(name="d", kind=kind))
            self.assertTrue(adapter.is_mock)

    def test_every_kind_builds_something_simulated_without_config(self):
        # 不填端口、不填地址，任何一类都不许变成真设备。
        from devices.provider import build_provider

        for kind in KINDS:
            adapter = build_provider(DeviceSpec(name="d", kind=kind))
            self.assertTrue(adapter.is_mock, kind)


class InstallTests(TempDir):
    def manager(self, **kwargs):
        return DeviceManager(**kwargs)

    def test_devices_are_registered(self):
        manager = self.manager()
        results = install(
            manager,
            [DeviceSpec(name="m1", kind="mock"), DeviceSpec(name="e1", kind="esp32")],
        )
        self.assertTrue(all(item["ok"] for item in results))
        self.assertEqual(manager.names(), ["e1", "m1"])

    def test_bad_kind_is_reported_not_raised(self):
        manager = self.manager()
        results = install(manager, [DeviceSpec(name="x", kind="spaceship")])
        self.assertFalse(results[0]["ok"])
        self.assertTrue(results[0]["error"])
        self.assertEqual(manager.names(), [])

    def test_real_device_is_still_refused(self):
        # 这一层不能绕过设备管理器的安全判断。
        manager = self.manager(allow_real=False)
        outcome = manager.register(Esp32Adapter(name="real", transport=FakeRealTransport()))
        self.assertFalse(outcome["ok"])
        self.assertIn("禁止接入真实设备", outcome["error"])

    def test_real_device_needs_an_explicit_switch(self):
        manager = self.manager(allow_real=True)
        outcome = manager.register(Esp32Adapter(name="real", transport=FakeRealTransport()))
        self.assertTrue(outcome["ok"])
        self.assertEqual(manager.names(), ["real"])


class LoadIntoTests(TempDir):
    def test_no_manifest_changes_nothing(self):
        manager = DeviceManager()
        report = load_into(manager, self.root)
        self.assertEqual(report["path"], "")
        self.assertEqual(report["specs"], [])
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["results"], [])
        self.assertEqual(report["connected"], {})
        self.assertEqual(manager.names(), [])

    def test_manifest_registers_and_connects(self):
        self.write_manifest(
            {
                "devices": [
                    {"name": "m1", "kind": "mock", "auto_connect": True},
                    {"name": "e1", "kind": "esp32"},
                    {"name": "bad", "kind": "spaceship"},
                ]
            }
        )
        manager = DeviceManager()
        report = load_into(manager, self.root)
        self.assertEqual(len(report["specs"]), 2)
        self.assertEqual(len(report["errors"]), 1)
        self.assertEqual(manager.names(), ["e1", "m1"])
        self.assertTrue(report["connected"]["m1"]["ok"])
        self.assertEqual(manager.connected_names(), ["m1"])

    def test_broken_manifest_is_reported_not_fatal(self):
        self.write_manifest("[")
        logger = FakeLogger()
        manager = DeviceManager(logger=logger)
        report = load_into(manager, self.root, logger=logger)
        self.assertEqual(manager.names(), [])
        self.assertTrue(report["errors"])

    def test_refused_device_is_not_connected(self):
        self.write_manifest({"devices": [{"name": "x", "kind": "spaceship", "auto_connect": True}]})
        manager = DeviceManager()
        report = load_into(manager, self.root)
        self.assertEqual(report["connected"], {})


class HealthTests(unittest.TestCase):
    def build(self, **kwargs):
        now = [1000.0]
        manager = DeviceManager(clock=lambda: now[0], **kwargs)
        return manager, now

    def test_never_seen_counts_as_stale(self):
        manager, _ = self.build()
        manager.register(MockDevice(name="m1"))
        self.assertTrue(manager.is_stale("m1"))
        self.assertIsNone(manager.last_seen("m1"))
        self.assertEqual(manager.stale_names(), ["m1"])

    def test_a_reply_makes_it_fresh_until_it_goes_quiet(self):
        manager, now = self.build(stale_after=100)
        manager.register(MockDevice(name="m1"))
        manager.connect("m1")
        self.assertFalse(manager.is_stale("m1"))
        now[0] += 101
        self.assertTrue(manager.is_stale("m1"))

    def test_zero_means_never_stale(self):
        manager, now = self.build(stale_after=0)
        manager.register(MockDevice(name="m1"))
        manager.connect("m1")
        now[0] += 99999
        self.assertFalse(manager.is_stale("m1"))

    def test_refusal_still_counts_as_a_reply(self):
        manager, _ = self.build(stale_after=100)
        manager.register(MockDevice(name="m1"))
        result = manager.execute("m1", {"cmd": "servo", "angle": 10})
        self.assertFalse(result["ok"])
        self.assertFalse(manager.is_stale("m1"))

    def test_state_read_counts_as_a_reply(self):
        manager, _ = self.build(stale_after=100)
        manager.register(MockDevice(name="m1"))
        manager.get_state("m1")
        self.assertFalse(manager.is_stale("m1"))

    def test_connect_all_takes_a_list(self):
        manager, _ = self.build()
        manager.register(MockDevice(name="m1"))
        manager.register(MockDevice(name="m2"))
        outcome = manager.connect_all(["m2"])
        self.assertEqual(list(outcome), ["m2"])
        self.assertEqual(manager.connected_names(), ["m2"])
        self.assertEqual(manager.connect_all([]), {})

    def test_disconnect_all_clears_everything(self):
        manager, _ = self.build()
        manager.register(MockDevice(name="m1"))
        manager.connect("m1")
        manager.disconnect_all()
        self.assertEqual(manager.connected_names(), [])

    def test_describe_is_a_readonly_view(self):
        manager, _ = self.build(stale_after=42)
        manager.register(MockDevice(name="m1"))
        manager.connect("m1")
        view = manager.describe()
        for key in ("count", "names", "connected", "stale", "stale_after", "allow_real"):
            self.assertIn(key, view)
        self.assertEqual(view["count"], 1)
        self.assertEqual(view["names"], ["m1"])
        self.assertEqual(view["connected"], ["m1"])
        self.assertEqual(view["stale"], [])
        self.assertEqual(view["stale_after"], 42)
        self.assertFalse(view["allow_real"])

    def test_default_window_is_sane(self):
        self.assertGreater(DEFAULT_STALE_SECONDS, 0)

    def test_missing_device_is_reported(self):
        manager = DeviceManager()
        self.assertEqual(manager.connect_all(["ghost"])["ghost"]["ok"], False)
        self.assertFalse(manager.get_state("ghost")["ok"])


class ConfigTests(unittest.TestCase):
    def test_devices_folder_is_inside_the_data_folder(self):
        config = Config(load_env=False)
        self.assertEqual(config.devices_dir.name, "devices")
        self.assertEqual(config.devices_dir.parent, config.data_dir)
        self.assertTrue(config.devices_dir.is_dir())

    def test_env_can_move_it(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        target = holder.name
        with mock.patch.dict(
            os.environ, {"DESKPET_DEVICES_DIR": target}, clear=False
        ):
            config = Config(load_env=False)
        self.assertEqual(str(config.devices_dir), str(Path(target)))

    def test_redacted_shows_the_folder_not_the_content(self):
        view = Config(load_env=False).redacted()
        self.assertIn("devices_dir", view)
        self.assertNotIn("sk-", json.dumps(view))


class BoundaryTests(unittest.TestCase):
    def test_agent_core_does_not_know_about_the_manifest(self):
        text = (ROOT / "agent" / "core.py").read_text(encoding="utf-8").lower()
        for word in ("manifest", "devices.json", "autoconnect", "auto_connect"):
            self.assertNotIn(word, text)

    def test_manifest_module_only_needs_the_standard_library(self):
        text = (ROOT / "devices" / "manifest.py").read_text(encoding="utf-8")
        imported = {
            line.strip()
            for line in text.splitlines()
            if line.startswith("import ") or line.startswith("from ")
        }
        self.assertEqual(
            imported,
            {
                "import json",
                "from dataclasses import dataclass, field",
                "from pathlib import Path",
                "from devices.esp32 import Esp32Adapter, build_transport",
                "from devices.interface import MockDevice",
            },
        )

    # V11 之后真串口和 Wi-Fi 都是要做的，所以"设备目录里不许出现串口/网络"这条已经过期。
    # 现在守的是另一件事：开端口这件事只允许出现在这三个文件里，别处不许自己开。
    TRANSPORT_FILES = ("ports.py", "wifi.py", "esp32.py")

    def test_only_the_transport_files_open_ports_or_sockets(self):
        for path in (ROOT / "devices").glob("*.py"):
            if path.name in self.TRANSPORT_FILES:
                continue
            text = path.read_text(encoding="utf-8")
            for needle in ("import serial", "import socket"):
                self.assertNotIn(needle, text, "%s 出现在 %s" % (needle, path.name))

    def test_only_the_ports_module_talks_to_the_serial_library(self):
        for path in (ROOT / "devices").glob("*.py"):
            if path.name == "ports.py":
                continue
            self.assertNotIn(
                "import serial", path.read_text(encoding="utf-8"), path.name
            )

    def test_the_device_layer_never_shells_out(self):
        for path in (ROOT / "devices").glob("*.py"):
            text = path.read_text(encoding="utf-8")
            for needle in ("import subprocess", "os.system", "popen"):
                self.assertNotIn(needle, text, "%s 出现在 %s" % (needle, path.name))

    def test_manifest_folder_is_not_the_source_package(self):
        config = Config(load_env=False)
        self.assertNotEqual(config.devices_dir, ROOT / "devices")

    def test_memory_data_is_untouched(self):
        from database.db import Database

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = Config(load_env=False)
        config.db_path = os.path.join(tmp.name, "deskpet.sqlite3")
        db = Database(config.db_path)
        db.migrate()
        manager = DeviceManager()
        load_into(manager, config.devices_dir)
        with db.connect() as conn:
            self.assertTrue(conn.execute("select 1").fetchone())


if __name__ == "__main__":
    unittest.main()
