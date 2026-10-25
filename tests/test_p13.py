"""P13 验收：Local Tools —— 只读工具直连、结果说人话、没模型也能查。"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import safe_tools
from agent.context import ContextManager
from agent.core import AgentCore
from agent.router import Intent, Mode
from database.db import Database
from events import bus as bus_module
from events.bus import EventBus
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatResult, ModelProvider
from security.audit import AuditLog
from tools.builtin import (
    calendar_line,
    devices_line,
    news_line,
    weather_line,
    weather_tool,
)
from tools.permission import PermissionManager
from tools.registry import Tool, ToolExecutor, ToolRegistry


class ScriptProvider(ModelProvider):
    name = "script"

    def __init__(self, reply="查到了。", available=True, tools=None):
        self.reply = reply
        self.available = available
        self.calls = []

    def is_available(self):
        return self.available

    def chat(self, messages, tools=None, temperature=0.7):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        return ChatResult(ok=True, content=self.reply, model=self.name)

    def stream(self, messages, tools=None, temperature=0.7):
        raise NotImplementedError


class FakeWeather:
    """假天气服务：不联网，按脚本返回。"""

    def __init__(self, result=None):
        self.result = result or {
            "ok": True,
            "data": {"city": "北京", "temperature": 18, "wind": 5, "weather": "多云"},
        }
        self.calls = []

    def current(self, city=""):
        self.calls.append(city)
        return self.result


def simple_tool(name, permission="READ", keywords=("天气",), summarize=None, required=(), handler=None):
    return Tool(
        name=name,
        description=f"{name} 测试工具",
        keywords=keywords,
        permission=permission,
        risk="测试",
        summarize=summarize,
        schema={
            "type": "object",
            "properties": {"q": {"type": "string"}} if required else {},
            "required": list(required),
        },
        handler=handler or (lambda **kwargs: {"ok": True}),
    )


def build(tmp, companion=None, tools=(), bus=None):
    database = Database(Path(tmp) / "t.sqlite3")
    database.migrate()
    log = AuditLog(database)
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    executor = ToolExecutor(registry, PermissionManager(audit=log), audit=log)
    memory = MemoryManager(MemoryRepository(database))
    agent = AgentCore(
        companion,
        memory,
        ContextManager(token_budget=800),
        executor=executor,
        bus=bus,
    )
    return agent, log, database


def texts(provider):
    return "\n".join(message.get("content") or "" for message in provider.calls[0]["messages"])


class CandidateTests(unittest.TestCase):
    def test_picks_single_no_arg_read_tool(self):
        registry = ToolRegistry()
        registry.register(simple_tool("get_weather", summarize=weather_line))
        tool = safe_tools.candidate(registry, "今天天气怎么样")
        self.assertIsNotNone(tool)
        self.assertEqual(tool.name, "get_weather")

    def test_more_than_one_candidate_is_left_to_the_model(self):
        registry = ToolRegistry()
        registry.register(simple_tool("get_weather", summarize=weather_line))
        registry.register(simple_tool("weather_alerts", summarize=weather_line))
        self.assertIsNone(safe_tools.candidate(registry, "今天天气怎么样"))

    def test_tool_that_needs_arguments_is_not_run_directly(self):
        registry = ToolRegistry()
        registry.register(
            simple_tool("get_device_state", keywords=("设备",), summarize=devices_line, required=("q",))
        )
        self.assertIsNone(safe_tools.candidate(registry, "设备"))

    def test_tool_without_summarize_is_not_run_directly(self):
        registry = ToolRegistry()
        registry.register(simple_tool("raw_reader", summarize=None))
        self.assertIsNone(safe_tools.candidate(registry, "天气"))

    def test_high_risk_tool_never_runs_directly(self):
        registry = ToolRegistry()
        registry.register(simple_tool("edit_file", permission="HIGH", summarize=lambda data: "改了"))
        registry.register(simple_tool("danger", permission="CRITICAL", summarize=lambda data: "跑了"))
        self.assertIsNone(safe_tools.candidate(registry, "天气"))

    def test_empty_input_has_no_candidate(self):
        registry = ToolRegistry()
        registry.register(simple_tool("get_weather", summarize=weather_line))
        self.assertIsNone(safe_tools.candidate(registry, ""))
        self.assertIsNone(safe_tools.candidate(None, "天气"))


class NoteTests(unittest.TestCase):
    def test_json_result_is_turned_into_a_sentence(self):
        tool = simple_tool("get_weather", summarize=weather_line)
        outcome = safe_tools_execute(tool, {"city": "北京", "temperature": 18, "weather": "多云"})
        self.assertEqual(safe_tools.note(tool, outcome), "北京现在 18°C，多云。")

    def test_failure_is_reported_not_faked(self):
        tool = simple_tool("get_weather", summarize=weather_line)
        outcome = safe_tools_execute(tool, error="天气服务暂时不可用：超时")
        self.assertIn("天气服务暂时不可用", safe_tools.note(tool, outcome))

    def test_broken_summarize_falls_through_quietly(self):
        def boom(data):
            raise ValueError("bad data")

        tool = simple_tool("get_weather", summarize=boom)
        outcome = safe_tools_execute(tool, {"ok": True})
        self.assertIsNone(safe_tools.note(tool, outcome))


def safe_tools_execute(tool, result=None, error=""):
    """假执行层：直接给出结果，不经过真实的权限与审计。

    执行层给 ToolOutcome.result 时存的是 JSON 文本，这里保持一致。
    """
    import json

    from tools.registry import ToolOutcome

    if error:
        return ToolOutcome(name=tool.name, ok=False, allowed=False, error=error)
    return ToolOutcome(name=tool.name, ok=True, result=json.dumps(result, ensure_ascii=False))


class LineTests(unittest.TestCase):
    def test_weather_line(self):
        self.assertEqual(
            weather_line({"city": "北京", "temperature": 18, "weather": "多云"}),
            "北京现在 18°C，多云。",
        )
        self.assertIn("读不到", weather_line({"city": "北京"}))

    def test_news_line(self):
        line = news_line({"topic": "头条", "items": [{"title": "甲"}, {"title": "乙"}]})
        self.assertEqual(line, "头条：甲；乙。")
        self.assertIn("没有拿到", news_line({"topic": "头条", "items": []}))

    def test_calendar_line(self):
        line = calendar_line({"events": [{"title": "开会", "when_at": "2026-10-08 09:00"}]})
        self.assertEqual(line, "最近的安排：2026-10-08 09:00 开会。")
        self.assertIn("没有安排", calendar_line({"events": []}))

    def test_devices_line(self):
        line = devices_line({"devices": [{"device": "mock-1", "connected": True}]})
        self.assertEqual(line, "已接入：mock-1（已连接）。")
        self.assertIn("没有接入", devices_line({"devices": []}))


class DirectAnswerTests(unittest.TestCase):
    def test_weather_is_answered_without_any_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = FakeWeather()
            agent, _, _ = build(tmp, None, [weather_tool(service)])
            reply = agent.respond("s", "今天天气怎么样")
            self.assertTrue(reply.ok)
            self.assertEqual(reply.content, "北京现在 18°C，多云。")
            self.assertEqual(reply.debug["safe_tool"], "get_weather")
            self.assertEqual(reply.debug["tools_offered"], [])
            self.assertEqual(reply.usage.total_tokens, 0)
            self.assertEqual(agent.mode_name, Mode.LOCAL.value)

    def test_service_failure_is_told_truthfully(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = FakeWeather(result={"ok": False, "error": "天气服务暂时不可用：超时"})
            agent, _, _ = build(tmp, None, [weather_tool(service)])
            reply = agent.respond("s", "今天天气怎么样")
            self.assertTrue(reply.ok)
            self.assertIn("天气服务暂时不可用", reply.content)
            self.assertNotIn("18°C", reply.content)

    def test_direct_run_is_audited(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, audit, _ = build(tmp, None, [weather_tool(FakeWeather())])
            agent.respond("s", "今天天气怎么样")
            kinds = {row["kind"] for row in audit.recent()}
            self.assertIn("tool_ok", kinds)

    def test_tool_completed_event_really_fires(self):
        with tempfile.TemporaryDirectory() as tmp:
            bus = EventBus()
            seen = []
            bus.subscribe(bus_module.TOOL_COMPLETED, seen.append)
            agent, _, _ = build(tmp, None, [weather_tool(FakeWeather())], bus=bus)
            agent.respond("s", "今天天气怎么样")
            self.assertEqual(len(seen), 1)
            self.assertEqual(seen[0].payload["name"], "get_weather")
            self.assertTrue(seen[0].payload["ok"])

    def test_emit_allows_a_name_key_in_the_payload(self):
        bus = EventBus()
        seen = []
        bus.subscribe("x", seen.append)
        bus.emit("x", name="tool", ok=True)
        self.assertEqual(seen[0].name, "x")
        self.assertEqual(seen[0].payload["name"], "tool")

    def test_tool_needing_arguments_is_not_auto_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            hits = []
            tool = simple_tool(
                "get_device_state",
                keywords=("设备",),
                summarize=devices_line,
                required=("q",),
                handler=lambda **kwargs: hits.append(kwargs) or {"ok": True},
            )
            agent, _, _ = build(tmp, ScriptProvider(reply="你想看哪台设备？"), [tool])
            reply = agent.respond("s", "看看设备状态")
            self.assertEqual(hits, [])
            self.assertEqual(reply.content, "你想看哪台设备？")

    def test_no_network_service_still_needs_a_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 需要联网又没有 summarize 的工具不会被直连
            tool = simple_tool("get_news", keywords=("新闻",), summarize=None)
            agent, _, _ = build(tmp, None, [tool])
            reply = agent.respond("s", "有什么新闻")
            self.assertFalse(reply.ok)
            self.assertIn("聊天模型", reply.error)


class ModelPathTests(unittest.TestCase):
    def test_companion_phrases_the_result_without_the_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = ScriptProvider(reply="北京现在 18 度，多云。")
            agent, _, _ = build(tmp, provider, [weather_tool(FakeWeather())])
            reply = agent.respond("s", "今天天气怎么样")
            self.assertEqual(reply.content, "北京现在 18 度，多云。")
            self.assertEqual(len(provider.calls), 1)
            self.assertIsNone(provider.calls[0]["tools"])
            self.assertIn("本轮已查到的信息", texts(provider))
            self.assertIn("北京现在 18°C，多云。", texts(provider))
            self.assertEqual(reply.debug["safe_tool"], "get_weather")
            self.assertEqual(reply.debug["tools_offered"], [])


class RegressionTests(unittest.TestCase):
    def test_countdown_still_goes_to_the_scheduler_capability(self):
        with tempfile.TemporaryDirectory() as tmp:
            hits = []
            agent, _, _ = build(
                tmp,
                ScriptProvider(),
                [
                    simple_tool(
                        "add_reminder",
                        permission="LOW",
                        keywords=("提醒",),
                        summarize=lambda data: "提醒建好了。",
                        handler=lambda **kwargs: hits.append(kwargs) or {"ok": True},
                    )
                ],
            )
            reply = agent.respond("s", "20 分钟后提醒我休息")
            self.assertTrue(reply.ok)
            self.assertEqual(reply.debug["capability"], "timer")
            self.assertEqual(hits, [])

    def test_local_capability_still_wins_over_safe_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _, _ = build(tmp, None, [weather_tool(FakeWeather())])
            reply = agent.respond("s", "现在几点")
            self.assertEqual(reply.debug["capability"], "time")


if __name__ == "__main__":
    unittest.main()
