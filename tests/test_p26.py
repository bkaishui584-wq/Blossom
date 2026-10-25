"""P26 验收：环境感知层。电量与内存读数、低电量事件、门禁、界面与开发者面板。"""

import inspect
import os
import sys
import time
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from behavior.environment import (
    EnvironmentProvider,
    EnvironmentWatcher,
    NullEnvironmentProvider,
    WindowsEnvironmentProvider,
)
from events import bus as bus_module
from events.bus import EventBus
from persona.state import EmotionState
from scheduler import rules as rules_module
from scheduler.manager import Scheduler
from security.config import Config
from ui.desktop import DesktopPet, launch


class StubAgent:
    def __init__(self):
        self.avatar = None
        self.emotion = EmotionState()
        self.persona = None
        self.mode_name = "local"


class FakeClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


class FakePreferences:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get_preference(self, key):
        return self.values.get(key)


class ScriptedProvider(EnvironmentProvider):
    """按脚本给读数的假来源。"""

    name = "scripted"

    def __init__(self, reading=None, available=True, boom=False):
        self.reading = dict(reading or {})
        self.available = available
        self.boom = boom

    def is_available(self):
        return self.available

    def snapshot(self):
        if self.boom:
            raise RuntimeError("read exploded")
        return dict(self.reading)


def at(hour, minute=0):
    """今天指定时刻的本地时间戳。"""
    parts = list(time.localtime())
    parts[3:6] = [hour, minute, 0]
    return time.mktime(time.struct_time(parts))


def battery_watcher(provider, low_battery_percent=20, bus=None):
    bus = bus or EventBus()
    seen = []
    for name in (bus_module.BATTERY_LOW, bus_module.BATTERY_OK):
        bus.subscribe(name, lambda event: seen.append(event))
    watcher = EnvironmentWatcher(
        provider=provider, bus=bus, low_battery_percent=low_battery_percent
    )
    return watcher, seen


def install_rules(clock, preferences=None):
    bus = EventBus()
    fired = []
    bus.subscribe(bus_module.COMPANION, lambda event: fired.append(event))
    scheduler = Scheduler(bus, preferences=preferences, clock=clock)
    config = SimpleNamespace(proactive=True, work_reminder_seconds=3600)
    rules_module.install(scheduler, config)
    return bus, scheduler, fired


class ProviderTests(unittest.TestCase):
    def test_null_provider_is_unavailable(self):
        provider = NullEnvironmentProvider()
        self.assertFalse(provider.is_available())
        self.assertEqual(provider.snapshot(), {})
        self.assertEqual(provider.describe()["provider"], "null")

    def test_windows_provider_reads_low_cost_facts(self):
        if os.name != "nt":
            self.skipTest("只在 Windows 上读数")
        provider = WindowsEnvironmentProvider()
        self.assertTrue(provider.is_available())
        reading = provider.snapshot()
        self.assertEqual(
            sorted(reading), ["battery_percent", "memory_percent", "on_power"]
        )
        percent = reading["battery_percent"]
        self.assertTrue(percent is None or 0 <= percent <= 100)
        memory = reading["memory_percent"]
        self.assertTrue(memory is None or 0 <= memory <= 100)

    def test_windows_provider_reports_unavailable_off_windows(self):
        provider = WindowsEnvironmentProvider()
        if os.name == "nt":
            self.skipTest("只在非 Windows 上判断")
        self.assertFalse(provider.is_available())
        self.assertEqual(provider.snapshot()["battery_percent"], None)


class WatcherTests(unittest.TestCase):
    def test_no_change_no_event(self):
        watcher, seen = battery_watcher(
            ScriptedProvider({"battery_percent": 80, "on_power": False})
        )
        self.assertEqual(watcher.update(), "")
        self.assertEqual(watcher.update(), "")
        self.assertEqual(seen, [])

    def test_low_battery_fires_once(self):
        provider = ScriptedProvider({"battery_percent": 80, "on_power": False})
        watcher, seen = battery_watcher(provider)
        watcher.update()
        provider.reading = {"battery_percent": 15, "on_power": False}
        self.assertEqual(watcher.update(), bus_module.BATTERY_LOW)
        provider.reading = {"battery_percent": 14, "on_power": False}
        self.assertEqual(watcher.update(), "")
        self.assertEqual([event.name for event in seen], [bus_module.BATTERY_LOW])
        self.assertEqual(seen[0].payload["battery_percent"], 15)
        self.assertEqual(watcher.low_count, 1)

    def test_charging_reports_recovery(self):
        provider = ScriptedProvider({"battery_percent": 12, "on_power": False})
        watcher, seen = battery_watcher(provider)
        self.assertEqual(watcher.update(), bus_module.BATTERY_LOW)
        provider.reading = {"battery_percent": 12, "on_power": True}
        self.assertEqual(watcher.update(), bus_module.BATTERY_OK)
        self.assertEqual(
            [event.name for event in seen],
            [bus_module.BATTERY_LOW, bus_module.BATTERY_OK],
        )
        self.assertFalse(watcher.low_battery)

    def test_charging_never_warns(self):
        watcher, seen = battery_watcher(
            ScriptedProvider({"battery_percent": 8, "on_power": True})
        )
        self.assertEqual(watcher.update(), "")
        self.assertEqual(seen, [])

    def test_unknown_battery_keeps_previous_state(self):
        provider = ScriptedProvider({"battery_percent": 10, "on_power": False})
        watcher, seen = battery_watcher(provider)
        watcher.update()
        provider.reading = {"battery_percent": None, "on_power": None}
        self.assertEqual(watcher.update(), "")
        self.assertTrue(watcher.low_battery)
        provider.reading = {"battery_percent": 9, "on_power": False}
        self.assertEqual(watcher.update(), "")
        self.assertEqual([event.name for event in seen], [bus_module.BATTERY_LOW])

    def test_desktop_without_battery_stays_silent(self):
        watcher, seen = battery_watcher(
            ScriptedProvider({"battery_percent": None, "memory_percent": 40})
        )
        self.assertEqual(watcher.update(), "")
        self.assertEqual(seen, [])

    def test_threshold_is_configurable(self):
        watcher, seen = battery_watcher(
            ScriptedProvider({"battery_percent": 45, "on_power": False}),
            low_battery_percent=50,
        )
        self.assertEqual(watcher.update(), bus_module.BATTERY_LOW)

    def test_broken_provider_does_not_crash(self):
        watcher, seen = battery_watcher(ScriptedProvider(boom=True))
        self.assertEqual(watcher.update(), "")
        self.assertEqual(seen, [])
        self.assertEqual(watcher.describe()["provider"], "scripted")

    def test_unavailable_provider_is_reported(self):
        watcher = EnvironmentWatcher(provider=ScriptedProvider(available=False))
        self.assertFalse(watcher.available())
        self.assertEqual(watcher.update(), "")

    def test_describe_carries_the_reading(self):
        watcher, _ = battery_watcher(
            ScriptedProvider(
                {"battery_percent": 55, "on_power": True, "memory_percent": 77}
            )
        )
        watcher.update()
        info = watcher.describe()
        self.assertEqual(info["memory_percent"], 77)
        self.assertTrue(info["on_power"])
        self.assertEqual(info["low_battery_percent"], 20)
        self.assertEqual(info["low_count"], 0)


class RuleTests(unittest.TestCase):
    def test_low_battery_reminder_carries_program_written_text(self):
        bus, _, fired = install_rules(FakeClock(at(12, 0)))
        EnvironmentWatcher(
            provider=ScriptedProvider({"battery_percent": 15, "on_power": False}),
            bus=bus,
        ).update()
        self.assertEqual([event.payload["rule"] for event in fired], ["battery_low"])
        self.assertTrue(fired[0].payload["local_text"])
        self.assertEqual(fired[0].payload["priority"], "high")
        self.assertEqual(fired[0].payload["emotion"], "tired")

    def test_low_battery_rule_is_not_polled_by_tick(self):
        _, scheduler, fired = install_rules(FakeClock(at(12, 0)))
        scheduler.tick()
        self.assertNotIn("battery_low", [event.payload["rule"] for event in fired])

    def test_low_battery_stays_quiet_at_night(self):
        bus, _, fired = install_rules(FakeClock(at(23, 30)))
        EnvironmentWatcher(
            provider=ScriptedProvider({"battery_percent": 10, "on_power": False}),
            bus=bus,
        ).update()
        self.assertEqual(fired, [])

    def test_low_battery_respects_user_preference(self):
        bus, _, fired = install_rules(
            FakeClock(at(12, 0)), preferences=FakePreferences({"proactive_enabled": "0"})
        )
        EnvironmentWatcher(
            provider=ScriptedProvider({"battery_percent": 10, "on_power": False}),
            bus=bus,
        ).update()
        self.assertEqual(fired, [])

    def test_low_battery_does_not_repeat_within_cooldown(self):
        provider = ScriptedProvider({"battery_percent": 10, "on_power": False})
        bus, _, fired = install_rules(FakeClock(at(12, 0)))
        watcher = EnvironmentWatcher(provider=provider, bus=bus)
        watcher.update()
        self.assertEqual(len(fired), 1)
        provider.reading = {"battery_percent": 90, "on_power": False}
        watcher.update()
        provider.reading = {"battery_percent": 10, "on_power": False}
        watcher.update()
        self.assertEqual(len(fired), 1)

    def test_low_battery_works_without_any_model(self):
        """低电量提醒是程序规则，不配置模型也能说出来。"""

        bus, scheduler, fired = install_rules(FakeClock(at(12, 0)))
        rule = [item for item in scheduler.rules if item.name == "battery_low"][0]
        self.assertTrue(rule.payload.get("local_text"))
        self.assertEqual(rule.trigger, bus_module.BATTERY_LOW)


class DesktopTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except Exception as exc:
            self.skipTest(f"tkinter 不可用：{exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy)

    def _destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _pet(self, environment=None, bus=None):
        config = Config(load_env=False)
        config.dev_mode = True
        return DesktopPet(
            StubAgent(), config, root=self.root, bus=bus, environment=environment
        )

    def test_tick_reads_environment(self):
        bus = EventBus()
        seen = []
        bus.subscribe(bus_module.BATTERY_LOW, seen.append)
        watcher = EnvironmentWatcher(
            provider=ScriptedProvider({"battery_percent": 15, "on_power": False}),
            bus=bus,
        )
        pet = self._pet(environment=watcher, bus=bus)
        pet._environment_tick()
        self.assertEqual(len(seen), 1)

    def test_broken_environment_does_not_break_the_pet(self):
        pet = self._pet(
            environment=EnvironmentWatcher(provider=ScriptedProvider(boom=True))
        )
        pet._environment_tick()
        self.assertEqual(pet.environment.reading, {})

    def test_no_environment_source_is_fine(self):
        pet = self._pet()
        pet._environment_tick()
        self.assertIsNone(pet.environment)

    def test_environment_shows_up_in_the_developer_panel(self):
        watcher = EnvironmentWatcher(
            provider=ScriptedProvider(
                {"battery_percent": 99, "on_power": True, "memory_percent": 50}
            )
        )
        pet = self._pet(environment=watcher)
        pet._set_dev({})
        self.assertIn("环境感知", pet.dev.get("1.0", "end"))

    def test_check_interval_is_low_frequency(self):
        self.assertGreaterEqual(DesktopPet.ENVIRONMENT_MS, 30000)

    def test_launch_accepts_environment(self):
        self.assertIn("environment", inspect.signature(launch).parameters)


class DecouplingTests(unittest.TestCase):
    def test_agent_core_has_no_environment_knowledge(self):
        text = (ROOT / "agent" / "core.py").read_text(encoding="utf-8").lower()
        for word in ("battery", "environment", "memory_percent", "on_power"):
            self.assertNotIn(word, text)

    def test_environment_layer_does_not_touch_agent_or_model(self):
        text = (ROOT / "behavior" / "environment.py").read_text(encoding="utf-8")
        for word in ("import agent", "from agent", "ModelProvider", "socket"):
            self.assertNotIn(word, text)

    def test_no_third_party_library(self):
        text = (ROOT / "behavior" / "environment.py").read_text(encoding="utf-8")
        for word in ("psutil", "requests", "urllib", "pip"):
            self.assertNotIn(word, text)

    def test_main_wires_environment_into_the_pet(self):
        text = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("EnvironmentWatcher(", text)
        self.assertIn("environment=environment,", text)


if __name__ == "__main__":
    unittest.main()
