"""P4 验收：事件总线、调度门禁、主动陪伴只在事件触发时调用模型。"""

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.context import ContextManager
from agent.core import AgentCore
from database.db import Database
from events import bus as bus_module
from events.bus import Event, EventBus
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatDelta, ChatResult, ModelProvider, Usage
from scheduler.manager import CompanionRule, Scheduler


class StubProvider(ModelProvider):
    name = "stub"

    def __init__(self, reply="该休息一下了。", fail=False):
        self.reply = reply
        self.fail = fail
        self.calls = []
        self.tools = []

    def is_available(self):
        return True

    def chat(self, messages, tools=None, temperature=0.7):
        self.calls.append(messages)
        self.tools.append(tools)
        if self.fail:
            return ChatResult(ok=False, error="模型不可用", model="stub")
        return ChatResult(
            ok=True,
            content=self.reply,
            usage=Usage(prompt_tokens=8, completion_tokens=4),
            model="stub",
        )

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


class FakeClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


def noon_timestamp():
    """当天 12:00 的本地时间戳，避免测试落进免打扰时段。"""
    parts = list(time.localtime())
    parts[3:6] = [12, 0, 0]
    return time.mktime(time.struct_time(parts))


def midnight_timestamp():
    parts = list(time.localtime())
    parts[3:6] = [23, 30, 0]
    return time.mktime(time.struct_time(parts))


class EventBusTests(unittest.TestCase):
    def test_emit_reaches_subscriber(self):
        bus = EventBus()
        seen = []
        bus.subscribe(bus_module.USER_MESSAGE, lambda event: seen.append(event.payload))
        event = bus.emit(bus_module.USER_MESSAGE, text="你好")
        self.assertEqual(seen, [{"text": "你好"}])
        self.assertEqual(event.name, bus_module.USER_MESSAGE)

    def test_wildcard_subscriber_sees_everything(self):
        bus = EventBus()
        names = []
        bus.subscribe(bus_module.WILDCARD, lambda event: names.append(event.name))
        bus.emit("a")
        bus.emit("b")
        self.assertEqual(names, ["a", "b"])

    def test_broken_handler_does_not_block_others(self):
        bus = EventBus()
        seen = []

        def boom(event):
            raise RuntimeError("订阅者出错")

        bus.subscribe("x", boom)
        bus.subscribe("x", lambda event: seen.append("ok"))
        results = bus.publish(Event(name="x"))
        self.assertEqual(seen, ["ok"])
        self.assertIn("error", results[0])

    def test_unsubscribe_stops_delivery(self):
        bus = EventBus()
        seen = []
        stop = bus.subscribe("x", lambda event: seen.append(1))
        stop()
        bus.emit("x")
        self.assertEqual(seen, [])

    def test_history_is_recorded(self):
        bus = EventBus()
        bus.emit("x", value=1)
        self.assertEqual(bus.recent()[0]["payload"]["value"], 1)


class SchedulerTests(unittest.TestCase):
    def make_scheduler(self, rules, clock, preferences=None, **kwargs):
        bus = EventBus()
        fired = []
        bus.subscribe(bus_module.COMPANION, lambda event: fired.append(event.payload))
        scheduler = Scheduler(
            bus, rules=rules, preferences=preferences, clock=clock, **kwargs
        )
        return bus, scheduler, fired

    def test_interval_controls_recheck_and_first_check_is_immediate(self):
        clock = FakeClock(noon_timestamp())
        rule = CompanionRule(
            name="r", situation="休息", interval_seconds=600, cooldown_seconds=0
        )
        _, scheduler, fired = self.make_scheduler([rule], clock)
        self.assertEqual(len(scheduler.tick()), 1)
        clock.advance(300)
        self.assertEqual(len(scheduler.tick()), 0)
        clock.advance(400)
        self.assertEqual(len(scheduler.tick()), 1)
        self.assertEqual(len(fired), 2)

    def test_quiet_hours_block_at_night(self):
        clock = FakeClock(midnight_timestamp())
        rule = CompanionRule(name="r", situation="休息", quiet_hours=(23, 7))
        _, scheduler, _ = self.make_scheduler([rule], clock)
        self.assertEqual(scheduler.tick(), [])
        self.assertEqual(scheduler.last_outcome["r"].reason, "处于免打扰时段")

    def test_cooldown_blocks_second_reminder(self):
        clock = FakeClock(noon_timestamp())
        rule = CompanionRule(
            name="r", situation="休息", interval_seconds=60, cooldown_seconds=3600
        )
        _, scheduler, _ = self.make_scheduler([rule], clock)
        self.assertEqual(len(scheduler.tick()), 1)
        clock.advance(120)
        self.assertEqual(scheduler.tick(), [])
        self.assertEqual(scheduler.last_outcome["r"].reason, "仍在冷却时间")

    def test_daily_frequency_limit(self):
        clock = FakeClock(noon_timestamp())
        rule = CompanionRule(
            name="r",
            situation="休息",
            interval_seconds=10,
            cooldown_seconds=0,
            max_per_day=2,
        )
        _, scheduler, _ = self.make_scheduler([rule], clock)
        for _ in range(4):
            scheduler.tick()
            clock.advance(20)
        self.assertEqual(scheduler.last_outcome["r"].reason, "已达到今日上限")

    def test_condition_must_pass(self):
        clock = FakeClock(noon_timestamp())
        rule = CompanionRule(
            name="r",
            situation="休息",
            condition=lambda ctx: (ctx["silent_for"] or 0) >= 3600,
        )
        _, scheduler, _ = self.make_scheduler([rule], clock)
        self.assertEqual(scheduler.tick(), [])
        self.assertEqual(scheduler.last_outcome["r"].reason, "触发条件未满足")

    def test_condition_exception_keeps_quiet(self):
        clock = FakeClock(noon_timestamp())

        def broken(ctx):
            raise RuntimeError("条件出错")

        rule = CompanionRule(name="r", situation="休息", condition=broken)
        _, scheduler, _ = self.make_scheduler([rule], clock)
        self.assertEqual(scheduler.tick(), [])

    def test_work_long_rule_fires_after_user_silence(self):
        clock = FakeClock(noon_timestamp())
        rule = CompanionRule(
            name="work_long",
            situation="提醒休息",
            interval_seconds=300,
            cooldown_seconds=3600,
            condition=lambda ctx: (ctx["silent_for"] or 0) >= 3600,
        )
        bus, scheduler, fired = self.make_scheduler([rule], clock)
        bus.emit(bus_module.USER_MESSAGE, text="开始工作")
        clock.advance(600)
        scheduler.tick()
        self.assertEqual(fired, [])
        clock.advance(4000)
        scheduler.tick()
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["rule"], "work_long")

    def test_user_preference_turns_companion_off(self):
        class Preferences:
            def __init__(self, values):
                self.values = values

            def get_preference(self, key):
                return self.values.get(key)

        clock = FakeClock(noon_timestamp())
        rule = CompanionRule(name="r", situation="休息")
        _, scheduler, _ = self.make_scheduler(
            [rule], clock, preferences=Preferences({"proactive_enabled": "0"})
        )
        self.assertEqual(scheduler.tick(), [])
        self.assertEqual(scheduler.last_outcome["r"].reason, "用户已关闭主动提醒")

    def test_quiet_hours_preference_overrides_rule(self):
        class Preferences:
            def get_preference(self, key):
                return "12-13" if key == "quiet_hours" else None

        clock = FakeClock(noon_timestamp())
        rule = CompanionRule(name="r", situation="休息", quiet_hours=(0, 0))
        _, scheduler, _ = self.make_scheduler([rule], clock, preferences=Preferences())
        self.assertEqual(scheduler.tick(), [])
        self.assertEqual(scheduler.last_outcome["r"].reason, "处于免打扰时段")

    def test_high_priority_rule_fires_first(self):
        clock = FakeClock(noon_timestamp())
        low = CompanionRule(name="low", situation="闲聊", priority="low")
        high = CompanionRule(name="high", situation="喝水", priority="high")
        _, scheduler, fired = self.make_scheduler([low, high], clock)
        scheduler.tick()
        self.assertEqual([payload["rule"] for payload in fired], ["high", "low"])

    def test_tick_never_calls_the_model(self):
        clock = FakeClock(noon_timestamp())
        provider = StubProvider()
        rule = CompanionRule(name="r", situation="休息")
        _, scheduler, _ = self.make_scheduler([rule], clock)
        scheduler.tick()
        self.assertEqual(provider.calls, [])

    def test_start_and_stop_thread(self):
        clock = FakeClock(noon_timestamp())
        rule = CompanionRule(name="r", situation="休息")
        _, scheduler, _ = self.make_scheduler([rule], clock, tick_seconds=0.01)
        self.assertTrue(scheduler.start())
        self.assertTrue(scheduler.running)
        self.assertFalse(scheduler.start())
        scheduler.stop()
        self.assertFalse(scheduler.running)


class ProactiveAgentTests(unittest.TestCase):
    def make_agent(self, tmp, provider, bus=None):
        database = Database(Path(tmp) / "t.sqlite3")
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        return AgentCore(provider, memory, ContextManager(token_budget=600), bus=bus)

    def test_event_triggers_one_short_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = StubProvider(reply="起来走走。")
            agent = self.make_agent(tmp, provider)
            event = Event(
                name=bus_module.COMPANION,
                payload={"rule": "work_long", "situation": "提醒休息", "priority": "normal"},
            )
            reply = agent.respond_to_event(event, session_id="cli")
            self.assertTrue(reply.ok)
            self.assertEqual(reply.content, "起来走走。")
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(reply.debug["rule"], "work_long")
            self.assertTrue(reply.debug["spoke"])

    def test_empty_reply_means_no_disturbance(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.make_agent(tmp, StubProvider(reply="   "))
            reply = agent.respond_to_event(Event(name=bus_module.COMPANION, payload={}))
            self.assertTrue(reply.ok)
            self.assertEqual(reply.content, "")
            self.assertFalse(reply.debug["spoke"])

    def test_proactive_call_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.make_agent(tmp, StubProvider(fail=True))
            reply = agent.respond_to_event(Event(name=bus_module.COMPANION, payload={}))
            self.assertFalse(reply.ok)
            self.assertIn("模型不可用", reply.error)

    def test_proactive_call_does_not_load_tool_schemas(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = StubProvider()
            agent = self.make_agent(tmp, provider)
            agent.respond_to_event(Event(name=bus_module.COMPANION, payload={}))
            self.assertIsNone(provider.tools[0])

    def test_user_message_event_is_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            bus = EventBus()
            seen = []
            bus.subscribe(bus_module.USER_MESSAGE, lambda event: seen.append(event.payload))
            agent = self.make_agent(tmp, StubProvider(reply="在"), bus=bus)
            agent.respond("cli", "在吗")
            self.assertEqual(seen[0]["text"], "在吗")

    def test_end_to_end_scheduler_to_agent_to_speech(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock(noon_timestamp())
            provider = StubProvider(reply="该休息了。")
            agent = self.make_agent(tmp, provider)
            spoken = []
            bus = EventBus()
            bus.subscribe(
                bus_module.COMPANION,
                lambda event: spoken.append(agent.respond_to_event(event, session_id="cli")),
            )
            rule = CompanionRule(
                name="work_long",
                situation="提醒休息",
                interval_seconds=300,
                condition=lambda ctx: (ctx["silent_for"] or 0) >= 3600,
            )
            scheduler = Scheduler(bus, rules=[rule], clock=clock)
            self.assertEqual(scheduler.tick(), [])
            bus.emit(bus_module.USER_MESSAGE, text="我开工了")
            clock.advance(4000)
            emitted = scheduler.tick()
            self.assertEqual(len(emitted), 1)
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(spoken[0].content, "该休息了。")


if __name__ == "__main__":
    unittest.main()
