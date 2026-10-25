"""P25 验收：主动陪伴调度。规则表、事件触发、免打扰门禁、日历临近提醒。"""

import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from events import bus as bus_module
from events.bus import EventBus
from scheduler import rules as rules_module
from scheduler.manager import CompanionRule, Scheduler

RULE_ORDER = [
    "work_long",
    "morning",
    "evening",
    "calendar_soon",
    "welcome_back",
    "battery_low",
]


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


class FakeCalendar:
    def __init__(self, events=None, ok=True, boom=False):
        self.events = list(events or [])
        self.ok = ok
        self.boom = boom
        self.calls = 0

    def list_events(self, limit=10):
        self.calls += 1
        if self.boom:
            raise RuntimeError("db gone")
        count = len(self.events)
        return {"ok": self.ok, "data": {"count": count, "events": list(self.events)}}


def at(hour, minute=0):
    """今天指定时刻的本地时间戳。"""
    parts = list(time.localtime())
    parts[3:6] = [hour, minute, 0]
    return time.mktime(time.struct_time(parts))


def build(clock=None, preferences=None, calendar=None, proactive=True, work_seconds=3600):
    """装好规则的调度器，返回 (总线, 调度器, 收到的主动开口事件)。"""
    bus = EventBus()
    fired = []
    bus.subscribe(bus_module.COMPANION, lambda event: fired.append(event))
    clock = clock or FakeClock(at(12, 0))
    scheduler = Scheduler(bus, preferences=preferences, clock=clock)
    config = SimpleNamespace(proactive=proactive, work_reminder_seconds=work_seconds)
    installed = rules_module.install(scheduler, config, calendar=calendar)
    assert installed is scheduler
    return bus, scheduler, fired


def names(fired):
    return [event.payload["rule"] for event in fired]


class InstallTests(unittest.TestCase):
    def test_installed_rule_names(self):
        _, scheduler, _ = build(calendar=FakeCalendar())
        self.assertEqual([rule.name for rule in scheduler.rules], RULE_ORDER)

    def test_calendar_rule_only_when_service_present(self):
        _, scheduler, _ = build()
        self.assertEqual(
            [rule.name for rule in scheduler.rules],
            ["work_long", "morning", "evening", "welcome_back", "battery_low"],
        )

    def test_proactive_off_installs_nothing(self):
        bus, scheduler, fired = build(proactive=False)
        self.assertEqual(scheduler.rules, [])
        self.assertEqual(scheduler.tick(), [])
        bus.emit(bus_module.USER_RETURNED)
        self.assertEqual(fired, [])


class TriggerRuleTests(unittest.TestCase):
    def test_trigger_rule_is_not_polled_by_tick(self):
        bus, scheduler, fired = build(clock=FakeClock(at(12, 0)))
        self.assertEqual(scheduler.tick(), [])
        self.assertEqual(fired, [])
        bus.emit(bus_module.USER_RETURNED)
        self.assertEqual(names(fired), ["welcome_back"])

    def test_welcome_back_cools_down(self):
        clock = FakeClock(at(12, 0))
        bus, scheduler, fired = build(clock=clock)
        bus.emit(bus_module.USER_RETURNED)
        self.assertEqual(len(fired), 1)
        bus.emit(bus_module.USER_RETURNED)
        self.assertEqual(len(fired), 1)
        self.assertFalse(scheduler.last_outcome["welcome_back"].allowed)
        self.assertTrue(scheduler.last_outcome["welcome_back"].reason)
        clock.advance(3600)
        bus.emit(bus_module.USER_RETURNED)
        self.assertEqual(len(fired), 2)

    def test_welcome_back_respects_user_preference(self):
        bus, scheduler, fired = build(
            clock=FakeClock(at(12, 0)),
            preferences=FakePreferences({"proactive_enabled": "0"}),
        )
        bus.emit(bus_module.USER_RETURNED)
        self.assertEqual(fired, [])
        self.assertFalse(scheduler.last_outcome["welcome_back"].allowed)

    def test_welcome_back_respects_quiet_hours(self):
        bus, scheduler, fired = build(clock=FakeClock(at(23, 30)))
        bus.emit(bus_module.USER_RETURNED)
        self.assertEqual(fired, [])
        self.assertTrue(scheduler.last_outcome["welcome_back"].reason)

    def test_idle_and_return_toggle_away_flag(self):
        bus, scheduler, _ = build(clock=FakeClock(at(12, 0)))
        self.assertFalse(scheduler.user_away)
        bus.emit(bus_module.USER_IDLE)
        self.assertTrue(scheduler.user_away)
        bus.emit(bus_module.USER_RETURNED)
        self.assertFalse(scheduler.user_away)


class TimedRuleTests(unittest.TestCase):
    def test_morning_fires_in_morning_window(self):
        _, scheduler, fired = build(clock=FakeClock(at(8, 0)))
        scheduler.tick()
        self.assertEqual(names(fired), ["morning"])

    def test_morning_keeps_quiet_inside_quiet_hours(self):
        _, scheduler, fired = build(clock=FakeClock(at(5, 30)))
        scheduler.tick()
        self.assertEqual(fired, [])

    def test_evening_fires_in_evening_window(self):
        _, scheduler, fired = build(clock=FakeClock(at(22, 0)))
        scheduler.tick()
        self.assertEqual(names(fired), ["evening"])

    def test_morning_fires_again_next_day(self):
        clock = FakeClock(at(8, 0))
        _, scheduler, fired = build(clock=clock)
        scheduler.tick()
        self.assertEqual(len(fired), 1)
        clock.advance(24 * 3600)
        scheduler.tick()
        self.assertEqual(len(fired), 2)

    def test_morning_stays_quiet_when_user_away(self):
        _, scheduler, fired = build(clock=FakeClock(at(8, 0)))
        scheduler.user_away = True
        scheduler.tick()
        self.assertEqual(fired, [])

    def test_user_preference_can_disable_quiet_hours(self):
        _, scheduler, fired = build(
            clock=FakeClock(at(6, 0)),
            preferences=FakePreferences({"quiet_hours": "0-0"}),
        )
        scheduler.tick()
        self.assertEqual(names(fired), ["morning"])

    def test_work_long_fires_after_configured_silence(self):
        clock = FakeClock(at(14, 0))
        bus, scheduler, fired = build(clock=clock, work_seconds=600)
        bus.emit(bus_module.USER_MESSAGE, text="在吗")
        scheduler.tick()
        self.assertEqual(fired, [])
        clock.advance(600)
        scheduler.tick()
        self.assertEqual(names(fired), ["work_long"])


class CalendarRuleTests(unittest.TestCase):
    @staticmethod
    def event(title, minutes, now):
        when = time.localtime(now + minutes * 60)
        return {
            "id": 1,
            "title": title,
            "when_at": time.strftime("%Y-%m-%d %H:%M", when),
            "note": "",
        }

    def test_upcoming_event_fires_with_title(self):
        now = at(15, 0)
        calendar = FakeCalendar([self.event("开会", 5, now)])
        _, scheduler, fired = build(clock=FakeClock(now), calendar=calendar)
        scheduler.tick()
        self.assertEqual(names(fired), ["calendar_soon"])
        self.assertEqual(fired[0].payload["event_title"], "开会")

    def test_far_event_stays_quiet(self):
        now = at(15, 0)
        calendar = FakeCalendar([self.event("开会", 40, now)])
        _, scheduler, fired = build(clock=FakeClock(now), calendar=calendar)
        scheduler.tick()
        self.assertEqual(fired, [])

    def test_past_event_stays_quiet(self):
        now = at(15, 0)
        calendar = FakeCalendar([self.event("开会", -30, now)])
        _, scheduler, fired = build(clock=FakeClock(now), calendar=calendar)
        scheduler.tick()
        self.assertEqual(fired, [])

    def test_unreadable_calendar_stays_quiet(self):
        now = at(15, 0)
        event = self.event("开会", 5, now)
        for calendar in (FakeCalendar([event], boom=True), FakeCalendar([event], ok=False)):
            _, scheduler, fired = build(clock=FakeClock(now), calendar=calendar)
            scheduler.tick()
            self.assertEqual(fired, [])

    def test_event_without_title_stays_quiet(self):
        now = at(15, 0)
        for title in ("", None, "   "):
            calendar = FakeCalendar([self.event(title, 5, now)])
            _, scheduler, fired = build(clock=FakeClock(now), calendar=calendar)
            scheduler.tick()
            self.assertEqual(fired, [])


class RobustnessTests(unittest.TestCase):
    def make(self, rule):
        bus = EventBus()
        fired = []
        bus.subscribe(bus_module.COMPANION, lambda event: fired.append(event))
        scheduler = Scheduler(bus, rules=[rule], clock=FakeClock(at(12, 0)))
        return bus, scheduler, fired

    def test_broken_condition_keeps_pet_quiet(self):
        def boom(context):
            raise RuntimeError("condition exploded")

        bus, scheduler, fired = self.make(
            CompanionRule(name="boom", situation="炸", condition=boom)
        )
        self.assertEqual(scheduler.tick(), [])
        self.assertEqual(fired, [])
        self.assertFalse(scheduler.last_outcome["boom"].allowed)

    def test_broken_condition_on_event_trigger_does_not_crash(self):
        bus, scheduler, fired = self.make(CompanionRule(name="seed", situation="种子"))
        scheduler.watch(
            bus_module.USER_RETURNED, situation="问候", condition=lambda ctx: 1 / 0
        )
        bus.emit(bus_module.USER_RETURNED)
        self.assertEqual(fired, [])

    def test_broken_preferences_do_not_block(self):
        def boom(self, key):
            raise RuntimeError("db gone")

        bus = EventBus()
        fired = []
        bus.subscribe(bus_module.COMPANION, lambda event: fired.append(event))
        preferences = type("Broken", (), {"get_preference": boom})()
        scheduler = Scheduler(bus, preferences=preferences, clock=FakeClock(at(12, 0)))
        rules_module.install(scheduler, SimpleNamespace(proactive=True, work_reminder_seconds=60))
        bus.emit(bus_module.USER_RETURNED)
        self.assertEqual(len(fired), 1)


class DecouplingTests(unittest.TestCase):
    def test_agent_core_has_no_rule_knowledge(self):
        source = (ROOT / "agent" / "core.py").read_text(encoding="utf-8")
        for word in ("calendar_soon", "work_long", "welcome_back", "morning_rule"):
            self.assertNotIn(word, source)

    def test_rules_are_plain_swappable_objects(self):
        """规则只是普通对象，换一套规则不用改 Agent。"""

        def always(context):
            return True

        bus = EventBus()
        fired = []
        bus.subscribe(bus_module.COMPANION, lambda event: fired.append(event))
        custom = SimpleNamespace(
            name="custom",
            situation="自定义",
            interval_seconds=0,
            event=bus_module.COMPANION,
            priority="normal",
            cooldown_seconds=0,
            max_per_day=99,
            quiet_hours=(0, 0),
            condition=always,
            payload={},
            trigger="",
        )
        scheduler = Scheduler(bus, rules=[custom], clock=FakeClock(at(12, 0)))
        scheduler.tick()
        self.assertEqual(names(fired), ["custom"])

    def test_app_wires_calendar_into_scheduler(self):
        source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("calendar=calendar_service", source)


if __name__ == "__main__":
    unittest.main()
