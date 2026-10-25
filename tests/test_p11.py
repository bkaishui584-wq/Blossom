"""V2 P0 验收：Capability Router —— 本地优先，能不用模型就不调用模型。"""

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.capabilities import CapabilityRouter, LocalCapability
from agent.context import ContextManager
from agent.core import NO_COMPANION_REPLY, AgentCore
from agent.router import Intent
from database.db import Database
from events import bus as bus_module
from events.bus import EventBus
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatDelta, ChatResult, ModelProvider, Usage
from persona.interface import DefaultPersonaProvider
from security.config import Config


FIXED = time.struct_time((2026, 10, 7, 21, 30, 0, 2, 280, 0))  # 周三 21:30


class CountingProvider(ModelProvider):
    """记录调用次数的假模型：本地能答的问题一次都不该被调用。"""

    name = "counting"

    def __init__(self):
        self.calls = 0

    def is_available(self):
        return True

    def chat(self, messages, tools=None, temperature=0.7):
        self.calls += 1
        return ChatResult(ok=True, content="模型回复", usage=Usage(3, 4), model="counting")

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


class FakeScheduler:
    """只记录一次性定时请求，不真的等。"""

    def __init__(self):
        self.requests = []

    def schedule_once(self, seconds, **payload):
        self.requests.append((seconds, payload))


def make_router(memory=None, scheduler=None, status=None, config=None):
    return CapabilityRouter(
        memory=memory,
        config=config,
        scheduler=scheduler,
        status=status or (lambda: {"mode": "local", "emotion": "neutral"}),
        clock=lambda: FIXED,
    )


_DEFAULT_MODEL = object()


def build(tmp, provider=_DEFAULT_MODEL):
    provider = CountingProvider() if provider is _DEFAULT_MODEL else provider
    database = Database(Path(tmp) / "t.sqlite3")
    database.migrate()
    memory = MemoryManager(MemoryRepository(database))
    agent = AgentCore(
        provider,
        memory,
        ContextManager(token_budget=600),
        persona=DefaultPersonaProvider(),
    )
    return agent, memory


class TimeAndDateTests(unittest.TestCase):
    def test_time_is_local_and_not_from_a_model(self):
        answer = make_router().resolve("现在几点")
        self.assertIsNotNone(answer)
        self.assertEqual(answer.capability, "time")
        self.assertEqual(answer.text, "现在是 21:30。")

    def test_date_is_local(self):
        answer = make_router().resolve("今天几号")
        self.assertEqual(answer.capability, "date")
        self.assertIn("2026-10-07", answer.text)
        self.assertIn("周三", answer.text)

    def test_weather_question_is_not_claimed_locally(self):
        self.assertIsNone(make_router().resolve("今天天气怎么样"))

    def test_greeting_is_not_a_local_capability(self):
        # 打招呼属于人格层，不属于本地能力层。
        self.assertIsNone(make_router().resolve("你好"))


class CalculatorTests(unittest.TestCase):
    def test_basic_arithmetic(self):
        answer = make_router().resolve("算一下 12*8+3")
        self.assertEqual(answer.capability, "calculator")
        self.assertIn("99", answer.text)

    def test_chinese_operators(self):
        answer = make_router().resolve("帮我算一下 3 加 5")
        self.assertIn("8", answer.text)

    def test_division_is_exact_or_rounded(self):
        answer = make_router().resolve("10 除以 4 等于多少")
        self.assertIn("2.5", answer.text)

    def test_divide_by_zero_is_not_answered(self):
        self.assertIsNone(make_router().resolve("算一下 5/0"))

    def test_code_is_never_executed(self):
        # 不允许 eval 任意内容：非数字表达式一律不回答。
        for text in ("算一下 __import__('os').system('echo hi')", "算一下 2**99999", "计算 open('x')"):
            self.assertIsNone(make_router().resolve(text), text)


class ConvertTests(unittest.TestCase):
    def test_km_to_mile(self):
        answer = make_router().resolve("100 公里是多少英里")
        self.assertEqual(answer.capability, "convert")
        self.assertIn("62.1371", answer.text)

    def test_jin_to_kg(self):
        self.assertIn("90", make_router().resolve("180 斤是多少公斤").text)

    def test_celsius_to_fahrenheit(self):
        self.assertIn("100", make_router().resolve("37.8 摄氏度是多少华氏度").text)

    def test_unknown_pair_is_not_guessed(self):
        self.assertIsNone(make_router().resolve("100 斤是多少公里"))


class TimerTests(unittest.TestCase):
    def test_timer_schedules_one_shot_without_a_model(self):
        scheduler = FakeScheduler()
        answer = make_router(scheduler=scheduler).resolve("10分钟后提醒我喝水")
        self.assertEqual(answer.capability, "timer")
        self.assertIn("10 分钟", answer.text)
        self.assertIn("喝水", answer.text)
        seconds, payload = scheduler.requests[0]
        self.assertEqual(seconds, 600)
        self.assertIn("喝水", payload["local_text"])

    def test_countdown_without_duration_is_not_claimed(self):
        self.assertIsNone(make_router(scheduler=FakeScheduler()).resolve("帮我创建一个提醒"))

    def test_timer_without_scheduler_says_so(self):
        answer = make_router().resolve("倒计时5分钟")
        self.assertIn("调度器", answer.text)


class StatusAndSystemTests(unittest.TestCase):
    def test_status_reports_real_mode(self):
        router = make_router(status=lambda: {"mode": "companion", "emotion": "happy"})
        answer = router.resolve("你现在怎么样")
        self.assertIn("陪伴", answer.text)
        self.assertIn("开心", answer.text)
        self.assertNotIn("模式模式", answer.text)

    def test_battery_and_memory_never_crash(self):
        router = make_router(status=lambda: {})
        for text in ("电脑电量还有多少", "系统内存够用吗"):
            answer = router.resolve(text)
            self.assertIsNotNone(answer, text)
            self.assertTrue(answer.text.strip(), text)

    def test_boundary_is_honest_without_models(self):
        answer = make_router(status=lambda: {"companion_ready": False, "task_ready": False}).resolve(
            "你能做什么"
        )
        self.assertEqual(answer.capability, "boundary")
        self.assertIn("不用模型", answer.text)
        self.assertIn("做不到", answer.text)


class MemoryAndSettingsTests(unittest.TestCase):
    def test_memory_readback(self):
        with tempfile.TemporaryDirectory() as tmp:
            _agent, memory = build(tmp)
            memory.remember("喜欢晚上学习")
            answer = make_router(memory=memory).resolve("你记住了什么")
            self.assertEqual(answer.capability, "memory")
            self.assertIn("晚上学习", answer.text)

    def test_empty_memory_is_honest(self):
        with tempfile.TemporaryDirectory() as tmp:
            _agent, memory = build(tmp)
            self.assertIn("还没记住", make_router(memory=memory).resolve("你记住了什么").text)

    def test_settings_readback(self):
        with tempfile.TemporaryDirectory() as tmp:
            _agent, memory = build(tmp)
            memory.set_preference("quiet_hours", "22-8")
            memory.set_preference("proactive_enabled", "0")
            answer = make_router(memory=memory).resolve("免打扰时段是什么")
            self.assertEqual(answer.capability, "settings")
            self.assertIn("22-8", answer.text)
            self.assertIn("关着", answer.text)

    def test_remember_memory_is_not_hijacked(self):
        with tempfile.TemporaryDirectory() as tmp:
            _agent, memory = build(tmp)
            self.assertIsNone(make_router(memory=memory).resolve("记住我喜欢晚上学习"))


class RouterOrderTests(unittest.TestCase):
    def test_first_matching_capability_wins(self):
        router = CapabilityRouter(
            skills=[
                LocalCapability("first", "第一个", lambda text, ctx: "A", ("测试",)),
                LocalCapability("second", "第二个", lambda text, ctx: "B", ("测试",)),
            ]
        )
        self.assertEqual(router.resolve("测试一下").capability, "first")

    def test_failing_capability_falls_through(self):
        def boom(text, ctx):
            raise RuntimeError("坏了")

        router = CapabilityRouter(
            skills=[
                LocalCapability("boom", "会炸", boom, ("测试",)),
                LocalCapability("ok", "兜底", lambda text, ctx: "兜底回答", ("测试",)),
            ]
        )
        self.assertEqual(router.resolve("测试一下").capability, "ok")

    def test_no_capability_returns_none(self):
        self.assertIsNone(make_router().resolve("讲个冷笑话"))


class AgentLocalFirstTests(unittest.TestCase):
    def test_local_question_never_calls_the_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = CountingProvider()
            agent, _ = build(tmp, provider)
            reply = agent.respond("s", "现在几点")
            self.assertTrue(reply.ok)
            self.assertRegex(reply.content, r"\d{1,2}:\d{2}")
            self.assertEqual(provider.calls, 0)
            self.assertEqual(reply.debug["mode"], "local")
            self.assertEqual(reply.debug["capability"], "time")
            self.assertTrue(reply.debug["local"])
            self.assertEqual(reply.usage.total_tokens, 0)

    def test_open_chat_still_uses_the_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = CountingProvider()
            agent, _ = build(tmp, provider)
            reply = agent.respond("s", "讲个冷笑话")
            self.assertTrue(reply.ok)
            self.assertEqual(reply.content, "模型回复")
            self.assertEqual(provider.calls, 1)
            self.assertEqual(reply.debug["mode"], "companion")

    def test_local_answers_also_work_without_any_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _ = build(tmp, None)
            reply = agent.respond("s", "100 公里是多少英里")
            self.assertTrue(reply.ok)
            self.assertIn("62.1371", reply.content)
            unknown = agent.respond("s", "讲个冷笑话")
            self.assertFalse(unknown.ok)
            self.assertEqual(unknown.error, NO_COMPANION_REPLY)

    def test_capability_boundary_answer_without_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _ = build(tmp, None)
            reply = agent.respond("s", "你能做什么")
            self.assertTrue(reply.ok)
            self.assertIn("不用模型", reply.content)
            self.assertEqual(reply.debug["capability"], "boundary")

    def test_local_status_view_is_read_only_and_shows_capability(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _ = build(tmp, None)
            agent.respond("s", "现在几点")
            status = agent.local_status()
            self.assertEqual(status["capability"], "time")
            self.assertFalse(status["companion_ready"])
            self.assertIn("mode", status)

    def test_timer_goes_through_the_scheduler_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _ = build(tmp)
            scheduler = FakeScheduler()
            agent.attach_scheduler(scheduler)
            reply = agent.respond("s", "5分钟后提醒我起来走走")
            self.assertTrue(reply.ok)
            self.assertEqual(scheduler.requests[0][0], 300)
            self.assertEqual(reply.debug["capability"], "timer")


class ProactiveWithoutModelTests(unittest.TestCase):
    def test_local_reminder_text_is_used_when_no_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _ = build(tmp, None)
            event = bus_module.Event(
                name=bus_module.COMPANION,
                payload={
                    "situation": "提醒用户：喝水",
                    "local_text": "时间到了：喝水",
                    "capability": "timer",
                },
            )
            reply = agent.respond_to_event(event)
            self.assertTrue(reply.ok)
            self.assertEqual(reply.content, "时间到了：喝水")
            self.assertEqual(reply.debug["capability"], "timer")

    def test_proactive_without_text_still_reports_missing_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _ = build(tmp, None)
            event = bus_module.Event(name=bus_module.COMPANION, payload={"situation": "该休息了"})
            reply = agent.respond_to_event(event)
            self.assertFalse(reply.ok)
            self.assertEqual(reply.error, NO_COMPANION_REPLY)


class SchedulerOneShotTests(unittest.TestCase):
    def test_one_shot_event_fires(self):
        bus = EventBus()
        seen = []
        bus.subscribe(bus_module.COMPANION, seen.append)
        from scheduler.manager import Scheduler

        scheduler = Scheduler(bus)
        scheduler.schedule_once(0.05, situation="提醒", local_text="时间到了")
        deadline = time.time() + 2
        while not seen and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(seen)
        self.assertEqual(seen[0].payload["local_text"], "时间到了")

    def test_stop_cancels_pending_timers(self):
        bus = EventBus()
        seen = []
        bus.subscribe(bus_module.COMPANION, seen.append)
        from scheduler.manager import Scheduler

        scheduler = Scheduler(bus)
        scheduler.schedule_once(0.2, situation="提醒")
        scheduler.stop()
        time.sleep(0.4)
        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
