"""P3 验收：工具注册、选择性 schema、权限等级、确认、禁止项、审计与执行。"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.context import ContextManager
from agent.core import TASK_CONFIRM_PROMPT, AgentCore
from database.db import Database
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatDelta, ChatResult, ModelProvider, ToolCall, Usage
from security.audit import AuditLog
from tools.permission import PermissionManager
from tools.registry import Tool, ToolExecutor, ToolRegistry


def time_tool(hits, permission="READ", schema=None):
    def handler(**kwargs):
        hits.append(kwargs)
        return "2026-10-06 09:00"

    return Tool(
        name="get_time",
        description="查询当前时间",
        schema=schema or {"type": "object", "properties": {}, "required": []},
        permission=permission,
        risk="只读",
        handler=handler,
        keywords=("时钟",),
    )


class RecordingProvider(ModelProvider):
    """第一轮请求工具，第二轮给出自然语言。"""

    name = "recording"

    def __init__(self, tool_name="get_time", arguments=None):
        self.tool_name = tool_name
        self.arguments = arguments or {}
        self.calls = []

    def is_available(self):
        return True

    def chat(self, messages, tools=None, temperature=0.7):
        self.calls.append({"messages": messages, "tools": tools})
        if len(self.calls) == 1 and self.tool_name:
            return ChatResult(
                ok=True,
                content="",
                tool_calls=[
                    ToolCall(id="call_1", name=self.tool_name, arguments=self.arguments)
                ],
                usage=Usage(prompt_tokens=10, completion_tokens=2),
                model="recording",
            )
        return ChatResult(
            ok=True,
            content="现在是九点。",
            usage=Usage(prompt_tokens=20, completion_tokens=6),
            model="recording",
        )

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.audit = None
        self.permissions = PermissionManager()

    def test_low_risk_is_auto_allowed(self):
        self.assertTrue(self.permissions.check("t", "READ").allowed)
        self.assertTrue(self.permissions.check("t", "LOW").allowed)

    def test_medium_needs_confirmation_and_fails_closed(self):
        decision = self.permissions.check("t", "MEDIUM")
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.needs_confirmation)

    def test_confirmation_callback_can_approve_or_reject(self):
        allowed = PermissionManager(confirm_callback=lambda *a: True)
        denied = PermissionManager(confirm_callback=lambda *a: False)
        self.assertTrue(allowed.check("t", "HIGH").allowed)
        self.assertFalse(denied.check("t", "HIGH").allowed)

    def test_critical_is_denied_even_with_confirmation(self):
        manager = PermissionManager(confirm_callback=lambda *a: True)
        decision = manager.check("rm", "CRITICAL")
        self.assertFalse(decision.allowed)
        self.assertFalse(decision.needs_confirmation)
        self.assertIn("禁止等级", decision.reason)

    def test_unknown_level_is_treated_as_dangerous(self):
        self.assertFalse(self.permissions.check("t", "SUPER_USER").allowed)

    def test_decisions_are_audited(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / "t.sqlite3")
            database.migrate()
            manager = PermissionManager(audit=AuditLog(database))
            manager.check("t", "CRITICAL")
            rows = AuditLog(database).recent()
            self.assertEqual(rows[0]["kind"], "permission")
            self.assertEqual(rows[0]["decision"], "deny")


class RegistryTests(unittest.TestCase):
    def test_schemas_are_only_loaded_for_requested_tools(self):
        registry = ToolRegistry()
        registry.register(time_tool([]))
        registry.register(Tool(name="other", description="别的工具", keywords=("别的",)))
        schemas = registry.schemas_for(["get_time"])
        self.assertEqual([s["function"]["name"] for s in schemas], ["get_time"])

    def test_index_has_no_full_schema(self):
        registry = ToolRegistry()
        registry.register(time_tool([]))
        self.assertIn("get_time", registry.index())
        self.assertNotIn("parameters", registry.index())

    def test_relevant_picks_matching_tool_only(self):
        registry = ToolRegistry()
        registry.register(time_tool([]))
        registry.register(Tool(name="weather", description="查天气", keywords=("天气", "下雨")))
        self.assertEqual(registry.relevant("系统时钟"), ["get_time"])
        self.assertEqual(registry.relevant("今天天气如何"), ["weather"])
        self.assertEqual(registry.relevant("讲个笑话"), [])


class ExecutorTests(unittest.TestCase):
    def make_executor(self, tmp, **permission_kwargs):
        database = Database(Path(tmp) / "t.sqlite3")
        database.migrate()
        audit = AuditLog(database)
        registry = ToolRegistry()
        executor = ToolExecutor(
            registry, PermissionManager(audit=audit, **permission_kwargs), audit=audit
        )
        return registry, executor, database

    def test_low_risk_tool_runs_and_audits(self):
        with tempfile.TemporaryDirectory() as tmp:
            hits = []
            registry, executor, database = self.make_executor(tmp)
            registry.register(time_tool(hits))
            outcome = executor.execute("get_time", {})
            self.assertTrue(outcome.ok)
            self.assertEqual(hits, [{}])
            kinds = {row["kind"] for row in AuditLog(database).recent()}
            self.assertIn("tool_ok", kinds)

    def test_unknown_tool_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, executor, _ = self.make_executor(tmp)
            self.assertFalse(executor.execute("nope", {}).ok)

    def test_critical_tool_never_reaches_handler(self):
        with tempfile.TemporaryDirectory() as tmp:
            hits = []
            registry, executor, _ = self.make_executor(tmp, confirm_callback=lambda *a: True)
            registry.register(time_tool(hits, permission="CRITICAL"))
            outcome = executor.execute("get_time", {})
            self.assertFalse(outcome.ok)
            self.assertFalse(outcome.allowed)
            self.assertEqual(hits, [])

    def test_high_risk_without_confirmation_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            hits = []
            registry, executor, _ = self.make_executor(tmp)
            registry.register(time_tool(hits, permission="HIGH"))
            outcome = executor.execute("get_time", {})
            self.assertFalse(outcome.ok)
            self.assertTrue(outcome.needs_confirmation)
            self.assertEqual(hits, [])

    def test_handler_error_is_reported_not_faked(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry, executor, _ = self.make_executor(tmp)

            def boom(**kwargs):
                raise RuntimeError("接口超时")

            registry.register(
                Tool(name="get_time", description="查时间", permission="READ", handler=boom)
            )
            outcome = executor.execute("get_time", {})
            self.assertFalse(outcome.ok)
            self.assertIn("接口超时", outcome.error)

    def test_missing_required_argument_is_rejected_before_handler(self):
        with tempfile.TemporaryDirectory() as tmp:
            hits = []
            registry, executor, _ = self.make_executor(tmp)
            schema = {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            }
            registry.register(time_tool(hits, schema=schema))
            outcome = executor.execute("get_time", {})
            self.assertFalse(outcome.ok)
            self.assertIn("缺少必填参数", outcome.error)
            self.assertEqual(hits, [])

    def test_long_result_is_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry, executor, _ = self.make_executor(tmp)
            registry.register(
                Tool(
                    name="get_time",
                    description="查时间",
                    permission="READ",
                    handler=lambda **kwargs: "x" * 5000,
                )
            )
            outcome = executor.execute("get_time", {})
            self.assertTrue(outcome.ok)
            self.assertTrue(outcome.truncated)
            self.assertEqual(len(outcome.result), ToolExecutor.MAX_RESULT_CHARS)


class AgentToolFlowTests(unittest.TestCase):
    def make_agent(self, tmp, registry_tools, provider, task_provider=None, **permission_kwargs):
        database = Database(Path(tmp) / "t.sqlite3")
        database.migrate()
        audit = AuditLog(database)
        registry = ToolRegistry()
        for tool in registry_tools:
            registry.register(tool)
        executor = ToolExecutor(
            registry, PermissionManager(audit=audit, **permission_kwargs), audit=audit
        )
        memory = MemoryManager(MemoryRepository(database))
        return AgentCore(
            provider,
            memory,
            ContextManager(token_budget=800),
            executor=executor,
            task_provider=task_provider,
        )

    def test_agent_runs_tool_then_answers_with_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            hits = []
            provider = RecordingProvider("get_time")
            agent = self.make_agent(tmp, [time_tool(hits)], provider)
            reply = agent.respond("s", "帮我看下系统时钟")
            self.assertTrue(reply.ok)
            self.assertEqual(reply.content, "现在是九点。")
            self.assertEqual(hits, [{}])
            self.assertIn("get_time", reply.debug["tools_offered"])
            self.assertEqual(reply.debug["tool_results"][0]["ok"], True)
            self.assertEqual(reply.usage.total_tokens, 38)

    def test_only_relevant_schema_is_sent_to_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = RecordingProvider("get_time")
            agent = self.make_agent(
                tmp,
                [
                    time_tool([]),
                    Tool(name="weather", description="查天气", keywords=("天气",)),
                ],
                provider,
            )
            agent.respond("s", "帮我看下系统时钟")
            offered = provider.calls[0]["tools"]
            self.assertEqual([s["function"]["name"] for s in offered], ["get_time"])

    def test_no_schema_sent_when_no_tool_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = RecordingProvider("")
            agent = self.make_agent(tmp, [time_tool([])], provider)
            agent.respond("s", "讲个笑话")
            self.assertIsNone(provider.calls[0]["tools"])

    def test_high_risk_tool_is_not_executed_and_refusal_is_truthful(self):
        with tempfile.TemporaryDirectory() as tmp:
            hits = []
            provider = RecordingProvider("get_time")
            agent = self.make_agent(tmp, [time_tool(hits, permission="HIGH")], provider)
            reply = agent.respond("s", "帮我看下系统时钟")
            self.assertTrue(reply.ok)
            self.assertEqual(hits, [])
            # 陪伴模型连 schema 都拿不到高风险工具
            self.assertEqual(reply.debug["tools_offered"], [])
            self.assertIsNone(provider.calls[0]["tools"])
            self.assertFalse(reply.debug["tool_results"][0]["allowed"])
            self.assertFalse(reply.debug["tool_results"][0]["ok"])
            tool_message = provider.calls[1]["messages"][-1]["content"]
            self.assertIn("没有提供给当前模型", tool_message)

    def test_llm_cannot_call_critical_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            hits = []
            provider = RecordingProvider("danger")
            danger = Tool(
                name="danger",
                description="执行系统命令",
                permission="CRITICAL",
                risk="会修改系统",
                handler=lambda **kwargs: hits.append(kwargs) or "done",
                keywords=("命令",),
            )
            agent = self.make_agent(
                tmp,
                [danger],
                provider,
                task_provider=RecordingProvider("danger"),
                confirm_callback=lambda *a: True,
            )
            # 执行类请求先进任务确认，而不是直接动手
            pending = agent.respond("s", "帮我执行命令")
            self.assertEqual(agent.mode_name, "task_pending")
            self.assertEqual(pending.content, TASK_CONFIRM_PROMPT)
            self.assertEqual(hits, [])

            # 用户确认后进入任务模式，危险工具仍然被权限拦住
            reply = agent.respond("s", "开始")
            self.assertEqual(hits, [])
            self.assertFalse(reply.debug["tool_results"][0]["allowed"])


if __name__ == "__main__":
    unittest.main()
