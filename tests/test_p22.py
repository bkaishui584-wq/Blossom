"""P22 验收：用户活动感知与桌宠自己的时间感 —— 全是程序判断，不经过模型。"""

import os
import sys
import tkinter as tk
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from behavior import actions
from behavior.activity import (
    ActivityWatcher,
    NullUserActivityProvider,
    UserActivityProvider,
    WindowsUserActivityProvider,
)
from behavior.manager import BehaviorManager
from events import bus as bus_module
from events.bus import EventBus
from persona.state import EmotionState
from security.config import Config
from ui.desktop import DesktopPet


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FixedRandom:
    """待机永远取下限，方便比较不同时间段的节奏。"""

    def choice(self, seq):
        return seq[0]

    def randint(self, low, high):
        return low


class FakeProvider(UserActivityProvider):
    name = "fake"

    def __init__(self, seconds=0.0, available=True):
        self.seconds = seconds
        self.available = available

    def is_available(self):
        return self.available

    def idle_seconds(self):
        return self.seconds


class BrokenProvider(UserActivityProvider):
    name = "broken"

    def is_available(self):
        raise RuntimeError("no api")

    def idle_seconds(self):
        raise RuntimeError("boom")


class StubBehavior:
    """只记录行为层被怎么调用了。"""

    def __init__(self):
        self.away = False
        self.calls = []

    def user_away(self, now=None):
        self.away = True
        self.calls.append("away")
        return self.away

    def user_back(self, now=None):
        self.away = False
        self.calls.append("back")
        return self.away


class StubAgent:
    def __init__(self):
        self.avatar = None
        self.emotion = EmotionState()
        self.persona = None
        self.mode_name = "local"


class ProviderTests(unittest.TestCase):
    def test_null_provider_always_reports_active(self):
        provider = NullUserActivityProvider()
        self.assertFalse(provider.is_available())
        self.assertEqual(provider.idle_seconds(), 0.0)
        self.assertIn("没有接入用户活动感知", provider.describe()["note"])

    def test_windows_provider_reads_a_non_negative_number(self):
        provider = WindowsUserActivityProvider()
        self.assertEqual(provider.is_available(), os.name == "nt")
        seconds = provider.idle_seconds()
        self.assertIsInstance(seconds, float)
        self.assertGreaterEqual(seconds, 0.0)

    def test_windows_provider_is_quiet_on_other_platforms(self):
        if os.name == "nt":
            self.skipTest("只在非 Windows 上验证降级")
        self.assertFalse(WindowsUserActivityProvider().is_available())


class WatcherTests(unittest.TestCase):
    def test_no_provider_never_reports_away(self):
        watcher = ActivityWatcher()
        self.assertFalse(watcher.available())
        self.assertEqual(watcher.check(), "")
        self.assertFalse(watcher.away)
        self.assertEqual(watcher.away_count, 0)

    def test_away_after_the_threshold(self):
        watcher = ActivityWatcher(provider=FakeProvider(2401))
        self.assertEqual(watcher.check(), bus_module.USER_IDLE)
        self.assertTrue(watcher.away)
        self.assertEqual(watcher.away_count, 1)

    def test_away_is_reported_once(self):
        watcher = ActivityWatcher(provider=FakeProvider(9999))
        watcher.check()
        self.assertEqual(watcher.check(), "")
        self.assertEqual(watcher.away_count, 1)

    def test_return_after_the_user_acts_again(self):
        provider = FakeProvider(9999)
        watcher = ActivityWatcher(provider=provider)
        watcher.check()
        provider.seconds = 0.0
        self.assertEqual(watcher.check(), bus_module.USER_RETURNED)
        self.assertFalse(watcher.away)
        self.assertEqual(watcher.return_count, 1)

    def test_return_is_reported_once(self):
        provider = FakeProvider(9999)
        watcher = ActivityWatcher(provider=provider)
        watcher.check()
        provider.seconds = 1.0
        watcher.check()
        self.assertEqual(watcher.check(), "")
        self.assertEqual(watcher.return_count, 1)

    def test_a_full_away_and_back_cycle(self):
        provider = FakeProvider(0.0)
        watcher = ActivityWatcher(provider=provider)
        self.assertEqual(watcher.check(), "")
        provider.seconds = 3000.0
        self.assertEqual(watcher.check(), bus_module.USER_IDLE)
        provider.seconds = 2.0
        self.assertEqual(watcher.check(), bus_module.USER_RETURNED)
        self.assertEqual((watcher.away_count, watcher.return_count), (1, 1))

    def test_broken_provider_is_contained(self):
        watcher = ActivityWatcher(provider=BrokenProvider())
        self.assertFalse(watcher.available())
        self.assertEqual(watcher.idle_seconds(), 0.0)
        self.assertEqual(watcher.check(), "")
        self.assertFalse(watcher.away)

    def test_negative_and_silly_values_are_clamped(self):
        watcher = ActivityWatcher(provider=FakeProvider(-5))
        self.assertEqual(watcher.idle_seconds(), 0.0)
        self.assertEqual(watcher.check(), "")

    def test_events_are_published_on_the_bus(self):
        bus = EventBus()
        seen = []
        bus.subscribe(bus_module.USER_IDLE, lambda event: seen.append(event.name))
        bus.subscribe(bus_module.USER_RETURNED, lambda event: seen.append(event.name))
        provider = FakeProvider(5000)
        watcher = ActivityWatcher(provider=provider, bus=bus)
        watcher.check()
        provider.seconds = 1.0
        watcher.check()
        self.assertEqual(
            seen, [bus_module.USER_IDLE, bus_module.USER_RETURNED]
        )

    def test_callbacks_receive_the_idle_seconds(self):
        seen = []
        provider = FakeProvider(5000)
        watcher = ActivityWatcher(
            provider=provider, on_idle=seen.append, on_return=seen.append
        )
        watcher.check()
        provider.seconds = 1.0
        watcher.check()
        self.assertEqual(seen, [5000.0, 1.0])

    def test_broken_callback_is_contained(self):
        def explode(seconds):
            raise RuntimeError("boom")

        watcher = ActivityWatcher(provider=FakeProvider(5000), on_idle=explode)
        self.assertEqual(watcher.check(), bus_module.USER_IDLE)

    def test_describe_reports_state(self):
        provider = FakeProvider(5000)
        watcher = ActivityWatcher(provider=provider)
        watcher.check()
        info = watcher.describe()
        self.assertEqual(info["provider"], "fake")
        self.assertTrue(info["away"])
        self.assertEqual(info["away_count"], 1)
        self.assertEqual(info["idle_after"], 2400.0)


class DaypartTests(unittest.TestCase):
    def test_hours_map_to_the_right_part_of_day(self):
        for hour, expected in (
            (3, actions.SLEEP),
            (6, actions.MORNING),
            (12, actions.DAY),
            (19, actions.EVENING),
            (23, actions.NIGHT),
        ):
            self.assertEqual(actions.daypart_of(hour), expected)

    def test_silly_hours_fall_back_to_daytime(self):
        self.assertEqual(actions.daypart_of("不是时间"), actions.DAY)
        self.assertEqual(actions.daypart_of(25), actions.SLEEP)

    def test_every_daypart_has_a_label_and_a_factor(self):
        for part in actions.DAYPARTS:
            self.assertTrue(actions.DAYPART_LABELS.get(part))
            self.assertGreaterEqual(actions.DAYPART_IDLE_FACTOR[part], 1.0)

    def test_later_is_quieter(self):
        factors = actions.DAYPART_IDLE_FACTOR
        self.assertGreater(factors[actions.EVENING], factors[actions.DAY])
        self.assertGreater(factors[actions.NIGHT], factors[actions.EVENING])


class BehaviorTimeTests(unittest.TestCase):
    def _manager(self, hour):
        return BehaviorManager(
            emotion=EmotionState(),
            hour_of=lambda ts: hour,
            random_source=FixedRandom(),
        )

    def test_manager_reports_its_daypart(self):
        self.assertEqual(self._manager(19).daypart(), actions.EVENING)
        self.assertEqual(self._manager(3).daypart(), actions.SLEEP)

    def test_night_moves_less_than_daytime(self):
        day = self._manager(12)
        night = self._manager(22)
        self.assertGreater(night._next_gap(), day._next_gap())

    def test_away_stops_idle_motions(self):
        manager = self._manager(12)
        self.assertIsNotNone(manager.idle())
        manager.user_away()
        manager.last_idle_at = None
        self.assertIsNone(manager.idle())

    def test_back_restores_the_rhythm(self):
        manager = self._manager(12)
        manager.user_away()
        manager.user_back()
        manager.last_idle_at = None
        self.assertIsNotNone(manager.idle())

    def test_away_is_reported_in_describe(self):
        manager = self._manager(12)
        manager.user_away()
        self.assertTrue(manager.describe()["user_away"])
        self.assertEqual(manager.describe()["daypart"], actions.DAY)


class DesktopActivityTests(unittest.TestCase):
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

    def _pet(self, activity=None, behavior=None):
        config = Config(load_env=False)
        config.dev_mode = True
        pet = DesktopPet(
            StubAgent(),
            config,
            root=self.root,
            behavior=behavior,
            activity=activity,
        )
        if activity is not None:
            activity.on_idle = pet.user_away
            activity.on_return = pet.user_back
        return pet

    def test_leaving_and_returning_reach_the_behavior_layer(self):
        behavior = StubBehavior()
        activity = ActivityWatcher(provider=FakeProvider(0.0))
        pet = self._pet(activity=activity, behavior=behavior)
        pet.user_away()
        self.assertTrue(behavior.away)
        pet.user_back()
        self.assertFalse(behavior.away)
        self.assertEqual(behavior.calls, ["away", "back"])

    def test_the_whole_chain_from_a_tick(self):
        behavior = StubBehavior()
        provider = FakeProvider(9999)
        activity = ActivityWatcher(provider=provider)
        pet = self._pet(activity=activity, behavior=behavior)
        pet._activity_tick()
        self.assertTrue(behavior.away)

    def test_activity_shows_up_in_the_developer_panel(self):
        pet = self._pet(activity=ActivityWatcher(provider=FakeProvider(5000)))
        pet._set_dev({})
        self.assertIn("活动感知", pet.dev.get("1.0", "end"))

    def test_broken_activity_source_does_not_break_the_pet(self):
        pet = self._pet(activity=ActivityWatcher(provider=BrokenProvider()))
        pet._activity_tick()
        self.assertFalse(pet.activity.available())

    def test_no_activity_source_keeps_the_pet_quiet(self):
        behavior = StubBehavior()
        pet = self._pet(behavior=behavior)
        pet._activity_tick()
        self.assertEqual(behavior.calls, [])


class AgentDecouplingTests(unittest.TestCase):
    def test_agent_core_knows_nothing_about_activity_sensing(self):
        text = (ROOT / "agent" / "core.py").read_text(encoding="utf-8").lower()
        for word in ("daypart", "idle_seconds", "user_away", "user.idle", "getlastinputinfo"):
            self.assertNotIn(word, text)

    def test_activity_module_imports_nothing_exotic(self):
        text = (ROOT / "behavior" / "activity.py").read_text(encoding="utf-8").lower()
        for needle in ("pywin32", "win32api", "psutil", "keyboard", "mouse", "pynput"):
            self.assertNotIn(needle, text)

    def test_swapping_the_activity_source_needs_no_behavior_change(self):
        for provider in (FakeProvider(9999), NullUserActivityProvider(), BrokenProvider()):
            watcher = ActivityWatcher(provider=provider)
            watcher.check()
            watcher.describe()


if __name__ == "__main__":
    unittest.main()
