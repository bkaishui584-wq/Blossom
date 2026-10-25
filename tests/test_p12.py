"""P12 验收：Companion / Task 分工 —— 角色路由、陪伴只拿安全工具、分工路由。"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.context import ContextManager
from agent.core import DEFAULT_SYSTEM_PROMPT, TASK_CONFIRM_PROMPT, AgentCore
from agent.router import Intent, Mode, classify
from database.db import Database
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatResult, ModelProvider, ModelRole, ModelSlot, ToolCall, Usage
from model.router import ModelRouter
from security.audit import AuditLog
from tools.permission import PermissionManager, SAFE_LEVELS
from tools.registry import Tool, ToolExecutor, ToolRegistry


class ScriptProvider(ModelProvider):
    """按脚本回话的假 Provider，记录每次调用。"""

    name = "script"

    def __init__(self, script=None, reply="好的。", available=True):
        self.script = list(script or [])
        self.reply = reply
        self.available = available
        self.calls = []

    def is_available(self):
        return self.available

    def chat(self, messages, tools=None, temperature=0.7):
        self.calls.append({"messages": list(messages), "tools": tools})
        if self.script:
            return self.script.pop(0)
        return ChatResult(ok=True, content=self.reply, model=self.name)

    def stream(self, messages, tools=None, temperature=0.7):
        raise NotImplementedError


def say(content):
    return ChatResult(ok=True, content=content, model="script")


def tool_call(name, **arguments):
    return ChatResult(
        ok=True,
        tool_calls=[ToolCall(id=name, name=name, arguments=arguments)],
        usage=Usage(prompt_tokens=3, completion_tokens=1),
        model="script",
    )


def fake_tool(name, permission="READ", keywords=("天气",), recorder=None):
    return Tool(
        name=name,
        description="测试工具",
        keywords=keywords,
        permission=permission,
        risk="测试",
        handler=lambda **kwargs: (
            recorder.append(kwargs) if recorder is not None else None
        )
        or "ok",
    )


def build(tmp, companion, task=None, tools=(), confirm_callback=None):
    database = Database(Path(tmp) / "t.sqlite3")
    database.migrate()
    audit = AuditLog(database)
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    executor = ToolExecutor(
        registry, PermissionManager(confirm_callback=confirm_callback, audit=audit), audit=audit
    )
    memory = MemoryManager(MemoryRepository(database))
    agent = AgentCore(
        companion,
        memory,
        ContextManager(token_budget=800),
        executor=executor,
        task_provider=task,
    )
    return agent, database


class ModelRouterTests(unittest.TestCase):
    def test_picks_provider_by_role(self):
        companion, task = ScriptProvider(), ScriptProvider()
        router = ModelRouter(companion=companion, task=task)
        self.assertIs(router.provider(ModelRole.COMPANION), companion)
        self.assertIs(router.provider(ModelRole.TASK), task)

    def test_missing_task_does_not_affect_companion(self):
        router = ModelRouter(companion=ScriptProvider())
        self.assertTrue(router.available(ModelRole.COMPANION))
        self.assertFalse(router.available(ModelRole.TASK))
        self.assertEqual(router.task.status(), "unconfigured")

    def test_unknown_role_is_handled_quietly(self):
        router = ModelRouter(companion=ScriptProvider())
        self.assertIsNone(router.slot("stt"))
        self.assertIsNone(router.provider("stt"))
        self.assertFalse(router.available("stt"))

    def test_accepts_prebuilt_slots(self):
        slot = ModelSlot(ModelRole.TASK.value, ScriptProvider(), configured=True)
        router = ModelRouter(task=slot)
        self.assertIs(router.task, slot)
        self.assertTrue(router.available(ModelRole.TASK))

    def test_status_reports_both_roles(self):
        router = ModelRouter(companion=ScriptProvider(), task=ScriptProvider())
        self.assertEqual(router.status(), {"companion": "connected", "task": "connected"})
        self.assertTrue(router.any_available())

    def test_unavailable_provider_is_not_connected(self):
        router = ModelRouter(companion=ScriptProvider(available=False))
        self.assertFalse(router.any_available())
        # 配了但连不上是「不可用」，跟「没配」不是一回事
        self.assertEqual(router.status()["companion"], "unavailable")


class AgentRoleWiringTests(unittest.TestCase):
    def test_agent_uses_router_for_both_slots(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _ = build(tmp, ScriptProvider(), ScriptProvider())
            self.assertIs(agent.companion_slot, agent.models.companion)
            self.assertIs(agent.task_slot, agent.models.task)
            self.assertIs(agent.companion_provider, agent.models.companion.provider)

    def test_model_status_comes_from_router(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _ = build(tmp, ScriptProvider(), ScriptProvider())
            status = agent.model_status()
            self.assertEqual(status["companion"]["status"], "connected")
            self.assertEqual(status["task"]["status"], "connected")
            self.assertEqual(status["mode"], Mode.COMPANION.value)

    def test_companion_prompt_says_how_to_enter_task_mode(self):
        self.assertIn("任务模式", DEFAULT_SYSTEM_PROMPT)


class CompanionToolBoundaryTests(unittest.TestCase):
    def test_high_risk_tool_is_never_offered_to_companion(self):
        with tempfile.TemporaryDirectory() as tmp:
            hits = []
            provider = ScriptProvider()
            agent, _ = build(
                tmp, provider, tools=[fake_tool("edit_file", "HIGH", ("文件",), hits)]
            )
            reply = agent.respond("s", "看看这个文件")
            self.assertTrue(reply.ok)
            self.assertEqual(reply.debug["tools_offered"], [])
            self.assertIsNone(provider.calls[0]["tools"])

    def test_medium_risk_tool_is_not_offered_either(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = ScriptProvider()
            agent, _ = build(tmp, provider, tools=[fake_tool("send_msg", "MEDIUM", ("天气",))])
            agent.respond("s", "今天天气怎么样")
            self.assertIsNone(provider.calls[0]["tools"])

    def test_safe_tool_is_still_offered(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = ScriptProvider()
            agent, _ = build(tmp, provider, tools=[fake_tool("get_weather", "READ", ("天气",))])
            reply = agent.respond("s", "今天天气怎么样")
            self.assertEqual(reply.debug["tools_offered"], ["get_weather"])
            self.assertEqual(
                [schema["function"]["name"] for schema in provider.calls[0]["tools"]],
                ["get_weather"],
            )

    def test_low_risk_tool_is_still_offered(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = ScriptProvider()
            agent, _ = build(
                tmp, provider, tools=[fake_tool("add_reminder", "LOW", ("提醒",))]
            )
            reply = agent.respond("s", "加个提醒")
            self.assertEqual(reply.debug["tools_offered"], ["add_reminder"])
            self.assertIn("LOW", SAFE_LEVELS)

    def test_hallucinated_tool_name_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            hits = []
            provider = ScriptProvider(
                script=[tool_call("edit_file"), say("这个我做不了，需要走任务模式。")]
            )
            agent, _ = build(
                tmp, provider, tools=[fake_tool("edit_file", "HIGH", ("文件",), hits)]
            )
            reply = agent.respond("s", "讲个笑话")
            self.assertEqual(hits, [])
            self.assertFalse(reply.debug["tool_results"][0]["allowed"])
            self.assertEqual(reply.content, "这个我做不了，需要走任务模式。")
            tool_message = provider.calls[1]["messages"][-1]["content"]
            self.assertIn("没有提供给当前模型", tool_message)

    def test_task_path_can_still_use_the_same_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _ = build(tmp, ScriptProvider(), tools=[fake_tool("edit_file", "HIGH", ("文件",))])
            self.assertEqual(agent._select_tools("看看这个文件"), ["edit_file"])
            self.assertEqual(agent._select_tools("看看这个文件", safe_only=True), [])

    def test_critical_tool_is_never_offered_to_companion(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _ = build(tmp, ScriptProvider(), tools=[fake_tool("run_shell", "CRITICAL", ("命令",))])
            # 关键字能匹配上，但陪伴模型这条路上一个 schema 都不发
            self.assertEqual(agent._select_tools("执行个命令看看"), ["run_shell"])
            self.assertEqual(agent._select_tools("执行个命令看看", safe_only=True), [])

    def test_command_request_goes_through_task_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            companion = ScriptProvider()
            agent, _ = build(tmp, companion, ScriptProvider(), tools=[fake_tool("run_shell", "CRITICAL", ("命令",))])
            reply = agent.respond("s", "执行个命令看看")
            self.assertEqual(reply.content, TASK_CONFIRM_PROMPT)
            self.assertEqual(agent.mode_name, Mode.TASK_PENDING.value)
            self.assertEqual(companion.calls, [])


class SplitRoutingTests(unittest.TestCase):
    def test_local_answers_stay_out_of_the_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = ScriptProvider()
            agent, _ = build(tmp, provider)
            time_reply = agent.respond("s", "现在几点")
            self.assertTrue(time_reply.ok)
            self.assertEqual(provider.calls, [])
            self.assertEqual(time_reply.debug["capability"], "time")
            date_reply = agent.respond("s", "今天几号")
            self.assertEqual(date_reply.debug["capability"], "date")
            self.assertEqual(provider.calls, [])

    def test_opinion_question_goes_to_companion(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = ScriptProvider(reply="我觉得你最近挺稳的。")
            agent, _ = build(tmp, provider, ScriptProvider(available=False))
            reply = agent.respond("s", "你觉得我最近学习怎么样")
            self.assertEqual(reply.content, "我觉得你最近挺稳的。")
            self.assertEqual(agent.mode_name, Mode.COMPANION.value)
            self.assertEqual(agent.last_intent, Intent.CHAT.value)

    def test_file_change_request_goes_to_task_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _ = build(tmp, ScriptProvider(), ScriptProvider())
            reply = agent.respond("s", "帮我修改 DeskPet")
            self.assertEqual(reply.content, TASK_CONFIRM_PROMPT)
            self.assertEqual(agent.mode_name, Mode.TASK_PENDING.value)
            self.assertFalse(agent.companion_provider.calls)

    def test_capability_question_is_not_a_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _ = build(tmp, ScriptProvider(reply="可以聊，动手要你点头。"), ScriptProvider())
            reply = agent.respond("s", "你能帮我改代码吗？")
            self.assertEqual(agent.last_intent, Intent.CHAT.value)
            self.assertNotEqual(agent.mode_name, Mode.TASK_PENDING.value)
            self.assertTrue(reply.ok)

    def test_weather_question_is_a_safe_tool_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = ScriptProvider()
            agent, _ = build(tmp, provider, tools=[fake_tool("get_weather", "READ", ("天气",))])
            reply = agent.respond("s", "帮我查今天天气")
            self.assertEqual(agent.last_intent, Intent.SAFE_TOOL.value)
            self.assertEqual(reply.debug["tools_offered"], ["get_weather"])
            self.assertEqual(agent.mode_name, Mode.COMPANION.value)

    def test_pending_task_can_be_cancelled_back_to_companion(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _ = build(tmp, ScriptProvider(), ScriptProvider())
            agent.respond("s", "帮我修改 DeskPet")
            cancelled = agent.respond("s", "算了")
            self.assertTrue(cancelled.ok)
            self.assertEqual(agent.mode_name, Mode.COMPANION.value)
            self.assertEqual(agent.task_state, "cancelled")

    def test_low_risk_request_without_task_words_stays_in_chat(self):
        self.assertEqual(classify("你觉得我最近学习怎么样"), Intent.CHAT)
        self.assertEqual(classify("帮我修改 DeskPet"), Intent.TASK)
        self.assertEqual(classify("今天几点"), Intent.CHAT)


class NoModelSplitTests(unittest.TestCase):
    def test_no_model_still_answers_locally(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _ = build(tmp, ScriptProvider(available=False))
            reply = agent.respond("s", "现在几点")
            self.assertTrue(reply.ok)
            self.assertEqual(agent.mode_name, Mode.LOCAL.value)
            self.assertEqual(reply.debug["capability"], "time")

    def test_no_model_says_so_instead_of_faking(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _ = build(tmp, ScriptProvider(available=False))
            reply = agent.respond("s", "你觉得我最近学习怎么样")
            self.assertFalse(reply.ok)
            self.assertIn("聊天模型", reply.error)

    def test_task_request_without_task_model_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _ = build(tmp, ScriptProvider(available=False))
            reply = agent.respond("s", "帮我修改 DeskPet")
            self.assertFalse(reply.ok)
            self.assertEqual(reply.debug["error_type"], "TASK_MODEL_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
