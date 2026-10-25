"""P9 验收：桌宠大脑 V1 —— 双模型、意图路由、模式切换、任务确认与无模型降级。"""

import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.context import ContextManager
from agent.core import (
    LOCAL_GREETING,
    NO_COMPANION_REPLY,
    NO_TASK_REPLY,
    TASK_CONFIRM_PROMPT,
    AgentCore,
)
from agent.router import Intent, classify, is_confirm, is_stop
from avatar.interface import AvatarProvider, DefaultAvatarProvider
from database.db import Database
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.adapter import build_companion_provider, build_task_provider
from model.base import (
    ChatDelta,
    ChatResult,
    ModelProvider,
    ModelRole,
    ModelSlot,
    ToolCall,
    Usage,
)
from security.audit import AuditLog
from security.config import Config
from tools.builtin import ask_user_tool
from tools.permission import PermissionManager
from tools.registry import ASK_USER_TOOL, Tool, ToolExecutor, ToolRegistry


@contextmanager
def env(**values):
    """临时设置环境变量，结束后恢复。"""
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update({key: value for key, value in values.items() if value is not None})
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


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
        yield ChatDelta(done=True)


def tool_call(name, **arguments):
    return ChatResult(
        ok=True,
        tool_calls=[ToolCall(id=name, name=name, arguments=arguments)],
        usage=Usage(prompt_tokens=3, completion_tokens=1),
        model="script",
    )


def say(content):
    return ChatResult(ok=True, content=content, model="script")


def fake_tool(name="get_weather", permission="READ", recorder=None, keywords=("天气",)):
    return Tool(
        name=name,
        description="测试工具",
        keywords=keywords,
        permission=permission,
        handler=lambda **kwargs: (
            recorder.append(kwargs) if recorder is not None else None
        )
        or "ok",
    )


def build(tmp, companion, task=None, tools=(), confirm_callback=None, task_confirm=True, memory=None):
    database = Database(Path(tmp) / "t.sqlite3")
    database.migrate()
    audit = AuditLog(database)
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    executor = ToolExecutor(
        registry, PermissionManager(confirm_callback=confirm_callback, audit=audit), audit=audit
    )
    memory = memory or MemoryManager(MemoryRepository(database))
    agent = AgentCore(
        companion,
        memory,
        ContextManager(token_budget=800),
        executor=executor,
        task_provider=task,
        task_confirm=task_confirm,
    )
    return agent, memory, database


class ModelSlotTests(unittest.TestCase):
    def test_unconfigured_is_not_a_failure(self):
        slot = ModelSlot(ModelRole.TASK.value)
        self.assertEqual(slot.status(), "unconfigured")
        self.assertEqual(slot.label, "未配置")
        self.assertFalse(slot.available())

    def test_connected_and_unavailable(self):
        ready = ModelSlot(ModelRole.COMPANION.value, ScriptProvider(), configured=True)
        self.assertEqual(ready.status(), "connected")
        self.assertEqual(ready.label, "已连接")
        broken = ModelSlot(
            ModelRole.COMPANION.value, ScriptProvider(available=False), configured=True
        )
        self.assertEqual(broken.status(), "unavailable")

    def test_broken_probe_does_not_crash(self):
        class Boom:
            def is_available(self):
                raise RuntimeError("probe failed")

        slot = ModelSlot(ModelRole.TASK.value, Boom(), configured=True)
        self.assertEqual(slot.status(), "unavailable")


class DualModelConfigTests(unittest.TestCase):
    def test_legacy_config_becomes_companion(self):
        with env(
            AGENT_LLM_BASE_URL="http://legacy.local/v1",
            AGENT_LLM_MODEL="legacy-model",
            AGENT_LLM_API_KEY="legacy-key-123456",
            DESKPET_COMPANION_MODEL=None,
            DESKPET_TASK_MODEL=None,
        ):
            config = Config(load_env=False)
        self.assertEqual(config.companion_model, "legacy-model")
        self.assertEqual(config.companion_base_url, "http://legacy.local/v1")
        self.assertTrue(config.has_companion())
        self.assertFalse(config.has_task())
        self.assertIsNone(build_task_provider(config))
        self.assertIsNotNone(build_companion_provider(config))

    def test_new_companion_config_wins(self):
        with env(
            AGENT_LLM_MODEL="legacy-model",
            DESKPET_COMPANION_MODEL="new-model",
            DESKPET_COMPANION_BASE_URL="http://new.local/v1",
            DESKPET_COMPANION_API_KEY=None,
        ):
            config = Config(load_env=False)
        self.assertEqual(config.companion_model, "new-model")
        self.assertEqual(config.companion_base_url, "http://new.local/v1")

    def test_task_model_is_independent(self):
        with env(
            AGENT_LLM_MODEL="companion-only",
            DESKPET_TASK_MODEL="task-only",
            DESKPET_TASK_BASE_URL="http://task.local/v1",
        ):
            config = Config(load_env=False)
        self.assertTrue(config.has_companion())
        self.assertTrue(config.has_task())
        companion = build_companion_provider(config)
        task = build_task_provider(config)
        self.assertEqual(companion.model, "companion-only")
        self.assertEqual(companion.role, "companion")
        self.assertEqual(task.model, "task-only")
        self.assertEqual(task.role, "task")

    def test_keys_are_never_exposed(self):
        with env(
            DESKPET_COMPANION_API_KEY="companion-secret-9876543210",
            DESKPET_TASK_API_KEY="task-secret-9876543210",
        ):
            config = Config(load_env=False)
        view = config.redacted()
        self.assertEqual(view["companion_api_key"], "set")
        self.assertEqual(view["task_api_key"], "set")
        rendered = str(view)
        self.assertNotIn("companion-secret-9876543210", rendered)
        self.assertNotIn("task-secret-9876543210", rendered)
        self.assertIn("task-secret-9876543210", config.secret_values())

    def test_confirm_defaults_on_and_can_be_disabled(self):
        with env(DESKPET_TASK_CONFIRM=None):
            self.assertTrue(Config(load_env=False).task_confirm)
        with env(DESKPET_TASK_CONFIRM="0"):
            self.assertFalse(Config(load_env=False).task_confirm)


class RouterTests(unittest.TestCase):
    def test_scenarios_from_spec(self):
        cases = (
            ("你好。", Intent.CHAT),
            ("你觉得我最近学习怎么样？", Intent.CHAT),
            ("记住我喜欢晚上学习。", Intent.MEMORY),
            ("今天台北天气怎么样？", Intent.SAFE_TOOL),
            ("帮我修改 agent/core.py。", Intent.TASK),
            # V2 验收 Case 3：让我写一段程序是创作请求，模型在对话里直接给，不进任务模式。
            ("帮我写一个 Python 程序。", Intent.CHAT),
            # V2 验收 Case 4：改用户的既有项目才是任务。
            ("帮我修改 deskpet 的 AgentCore。", Intent.TASK),
            ("帮我写代码改一下这个函数。", Intent.TASK),
            ("帮我写一段快排。", Intent.CHAT),
            ("把这个项目运行起来并修复报错。", Intent.TASK),
            ("帮我看看这个代码为什么错。", Intent.CHAT),
            ("你能帮我修改代码吗？", Intent.CHAT),
            ("告诉我 AgentCore 应该怎么修改。", Intent.CHAT),
            ("按照刚才说的方案直接帮我改。", Intent.TASK),
        )
        for text, expected in cases:
            self.assertEqual(classify(text), expected, text)

    def test_stop_and_confirm_words(self):
        self.assertTrue(is_stop("停。"))
        self.assertTrue(is_stop("算了"))
        self.assertTrue(is_stop("取消"))
        self.assertFalse(is_confirm("停"))
        self.assertTrue(is_confirm("开始"))


class NoModelTests(unittest.TestCase):
    def test_agent_starts_without_any_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _, _ = build(tmp, None, None)
            self.assertEqual(agent.mode_name, "local")
            self.assertEqual(agent.companion_slot.status(), "unconfigured")
            self.assertEqual(agent.task_slot.status(), "unconfigured")

    def test_greeting_is_honest_local_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _, _ = build(tmp, None, None)
            reply = agent.respond("s", "你好")
            self.assertTrue(reply.ok)
            self.assertEqual(reply.content, LOCAL_GREETING)
            self.assertEqual(reply.debug["mode"], "local")
            self.assertEqual(reply.debug["error_type"], "COMPANION_MODEL_UNAVAILABLE")

    def test_unknown_chat_is_reported_not_faked(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _, _ = build(tmp, None, None)
            reply = agent.respond("s", "讲个冷笑话")
            self.assertFalse(reply.ok)
            self.assertEqual(reply.error, NO_COMPANION_REPLY)
            self.assertFalse(reply.content)

    def test_local_time_is_real_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _, _ = build(tmp, None, None)
            reply = agent.respond("s", "现在几点")
            self.assertTrue(reply.ok)
            self.assertRegex(reply.content, r"\d{1,2}:\d{2}")

    def test_memory_can_be_saved_without_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, memory, _ = build(tmp, None, None)
            reply = agent.respond("s", "记住我喜欢晚上学习")
            self.assertTrue(reply.ok)
            self.assertTrue(memory.search("晚上学习"))

    def test_weather_question_is_not_answered_with_local_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _, _ = build(tmp, None, None)
            reply = agent.respond("s", "今天天气怎么样")
            self.assertFalse(reply.ok)
            self.assertEqual(reply.error, NO_COMPANION_REPLY)

    def test_explicit_date_question_uses_local_clock(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _, _ = build(tmp, None, None)
            reply = agent.respond("s", "今天几号")
            self.assertTrue(reply.ok)
            self.assertRegex(reply.content, r"\d{4}-\d{2}-\d{2}")

    def test_task_request_without_task_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _, _ = build(tmp, None, None)
            reply = agent.respond("s", "帮我修改 agent/core.py")
            self.assertFalse(reply.ok)
            self.assertEqual(reply.error, NO_TASK_REPLY)
            self.assertEqual(reply.debug["error_type"], "TASK_MODEL_UNAVAILABLE")

    def test_unavailable_companion_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _, _ = build(tmp, ScriptProvider(available=False))
            reply = agent.respond("s", "讲个冷笑话")
            self.assertFalse(reply.ok)
            self.assertEqual(reply.debug["error_type"], "COMPANION_MODEL_UNAVAILABLE")


class TaskFlowTests(unittest.TestCase):
    def test_chat_never_calls_task_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = ScriptProvider(reply="不该被调用")
            companion = ScriptProvider(reply="陪你聊")
            agent, _, _ = build(tmp, companion, task)
            reply = agent.respond("s", "你好呀")
            self.assertTrue(reply.ok)
            self.assertEqual(reply.content, "陪你聊")
            self.assertEqual(task.calls, [])

    def test_task_waits_for_confirmation_before_calling_task_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = []
            task = ScriptProvider(script=[tool_call("get_weather", city="北京")], reply="办好了。")
            agent, _, _ = build(tmp, ScriptProvider(reply="在的"), task, tools=[fake_tool(recorder=recorder)])

            pending = agent.respond("s", "帮我修改 agent/core.py")
            self.assertEqual(pending.content, TASK_CONFIRM_PROMPT)
            self.assertEqual(agent.mode_name, "task_pending")
            self.assertEqual(agent.task_state, "pending_confirmation")
            self.assertEqual(task.calls, [])

            done = agent.respond("s", "开始")
            self.assertTrue(done.ok)
            self.assertEqual(agent.mode_name, "companion")
            self.assertEqual(agent.task_state, "completed")
            self.assertEqual(recorder, [{"city": "北京"}])
            self.assertGreaterEqual(len(task.calls), 2)
            self.assertEqual(agent.last_task_result.steps[0]["tool"], "get_weather")
            self.assertEqual(done.debug["task_result"]["state"], "completed")
            self.assertEqual(done.debug["model"], "script")
            self.assertIn("total_tokens", done.debug)

    def test_confirmed_task_can_use_avatar_and_emotion(self):
        avatar = DefaultAvatarProvider()
        with tempfile.TemporaryDirectory() as tmp:
            task = ScriptProvider(reply="改好了。")
            agent, _, _ = build(tmp, ScriptProvider(reply="改好了。"), task, task_confirm=False)
            agent.avatar = avatar
            reply = agent.respond("s", "帮我修改 agent/core.py")
            self.assertTrue(reply.ok)
            self.assertEqual(avatar.state.get("emotion"), "happy")
            self.assertEqual(agent.mode_name, "companion")

    def test_task_can_skip_confirmation_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = ScriptProvider(reply="直接做了。")
            agent, _, _ = build(tmp, ScriptProvider(reply="x"), task, task_confirm=False)
            reply = agent.respond("s", "帮我修改 agent/core.py")
            self.assertTrue(reply.ok)
            self.assertEqual(len(task.calls), 1)
            self.assertEqual(agent.task_state, "completed")

    def test_missing_task_model_does_not_break_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            companion = ScriptProvider(reply="我在的。")
            agent, _, _ = build(tmp, companion, None)
            task_reply = agent.respond("s", "帮我修改 agent/core.py")
            self.assertFalse(task_reply.ok)
            self.assertEqual(task_reply.error, NO_TASK_REPLY)
            self.assertEqual(agent.mode_name, "companion")

            chat = agent.respond("s", "今天过得还不错")
            self.assertTrue(chat.ok)
            self.assertEqual(chat.content, "我在的。")

    def test_cancel_at_pending_returns_to_companion(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = ScriptProvider(reply="x")
            agent, _, _ = build(tmp, ScriptProvider(reply="好"), task)
            agent.respond("s", "帮我修改 agent/core.py")
            self.assertEqual(agent.mode_name, "task_pending")
            reply = agent.respond("s", "算了")
            self.assertTrue(reply.ok)
            self.assertEqual(agent.mode_name, "companion")
            self.assertEqual(agent.task_state, "cancelled")
            self.assertEqual(task.calls, [])

    def test_cancel_during_task_stops_remaining_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            executed = []
            holder = {}

            def cancelling(**kwargs):
                holder["agent"].cancel_task()
                executed.append("left")
                return "done"

            left = Tool(name="left", description="左", keywords=("all",), handler=cancelling)
            right = Tool(
                name="right",
                description="右",
                keywords=("all",),
                handler=lambda **kwargs: executed.append("right") or "done",
            )
            script = [
                ChatResult(
                    ok=True,
                    tool_calls=[
                        ToolCall(id="1", name="left", arguments={}),
                        ToolCall(id="2", name="right", arguments={}),
                    ],
                    model="script",
                ),
                say("不该跑到这里"),
            ]
            agent, _, _ = build(
                tmp, ScriptProvider(reply="好"), ScriptProvider(script=script), tools=[left, right]
            )
            holder["agent"] = agent
            agent.respond("s", "帮我修改 agent/core.py")
            reply = agent.respond("s", "开始")
            self.assertEqual(executed, ["left"])
            self.assertEqual(agent.task_state, "cancelled")
            self.assertIn("取消", reply.content)

    def test_high_risk_tool_inside_task_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            hits = []
            task = ScriptProvider(script=[tool_call("danger"), say("没做成。")])
            agent, _, _ = build(
                tmp,
                ScriptProvider(reply="x"),
                task,
                tools=[fake_tool(name="danger", permission="HIGH", recorder=hits, keywords=("命令",))],
                task_confirm=False,
            )
            reply = agent.respond("s", "帮我执行命令")
            self.assertEqual(hits, [])
            self.assertFalse(reply.debug["tool_results"][0]["allowed"])

    def test_task_failure_is_reported_not_faked(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 两次都超时：可恢复的错会按预算重试一次，两次都失败才收手，不许伪装成功。
            task = ScriptProvider(
                script=[ChatResult(ok=False, error="任务模型超时", model="script")] * 2
            )
            agent, _, _ = build(tmp, ScriptProvider(reply="x"), task, task_confirm=False)
            reply = agent.respond("s", "帮我修改 agent/core.py")
            self.assertFalse(reply.ok)
            self.assertIn("任务模型超时", reply.error)
            # 超时不算"执行失败"：没在时限内回来和做错了是两回事，状态要分开。
            self.assertEqual(agent.task_state, "timeout")
            self.assertEqual(agent.mode_name, "companion")
            self.assertEqual(len(task.calls), 2, "可恢复的错要按预算重试一次")

    def test_a_plain_task_failure_still_reads_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = ScriptProvider(script=[ChatResult(ok=False, error="模型就是不答", model="script")])
            agent, _, _ = build(tmp, ScriptProvider(reply="x"), task, task_confirm=False)
            reply = agent.respond("s", "帮我修改 agent/core.py")
            self.assertFalse(reply.ok)
            self.assertEqual(agent.task_state, "failed")


class TaskContextTests(unittest.TestCase):
    """任务模式只取相关记忆，不带闲聊历史，也不带整库。"""

    @staticmethod
    def prompt_text(call):
        return "\n".join(str(message.get("content", "")) for message in call["messages"])

    def test_task_receives_only_relevant_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = ScriptProvider(reply="改好了。")
            agent, memory, _ = build(
                tmp, ScriptProvider(reply="x"), task, task_confirm=False
            )
            memory.remember("项目代码在 deskpet/agent 目录，用 Python 3.13")
            memory.remember("用户养了一只叫团子的猫")
            agent.respond("s", "帮我修改 agent/core.py")

            prompt = self.prompt_text(task.calls[0])
            self.assertIn("deskpet/agent", prompt)
            self.assertNotIn("团子", prompt)
            self.assertGreaterEqual(agent.last_debug["memory_count"], 1)

    def test_task_does_not_receive_chat_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = ScriptProvider(reply="办好了。")
            agent, _, _ = build(tmp, ScriptProvider(reply="嗯嗯"), task, task_confirm=False)
            agent.respond("s", "我们昨天聊了团子这只猫")
            agent.respond("s", "帮我修改 agent/core.py")

            prompt = self.prompt_text(task.calls[0])
            self.assertNotIn("我们昨天聊了团子这只猫", prompt)
            self.assertIn("帮我修改 agent/core.py", prompt)

    def test_task_without_memory_still_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = ScriptProvider(reply="办好了。")
            agent, _, _ = build(tmp, ScriptProvider(reply="x"), task, task_confirm=False)
            reply = agent.respond("s", "帮我修改 agent/core.py")
            self.assertTrue(reply.ok)
            self.assertEqual(agent.task_state, "completed")
            self.assertEqual(agent.last_debug["memory_count"], 0)

    def test_broken_memory_does_not_break_task(self):
        class BrokenMemory:
            last_recall = {}

            def recall(self, *args, **kwargs):
                raise RuntimeError("db down")

            def record(self, *args, **kwargs):
                raise RuntimeError("db down")

            def record_usage(self, *args, **kwargs):
                raise RuntimeError("db down")

            def remember(self, *args, **kwargs):
                raise RuntimeError("db down")

        with tempfile.TemporaryDirectory() as tmp:
            task = ScriptProvider(reply="办好了。")
            agent, _, _ = build(
                tmp,
                ScriptProvider(reply="x"),
                task,
                task_confirm=False,
                memory=BrokenMemory(),
            )
            reply = agent.respond("s", "帮我修改 agent/core.py")
            self.assertTrue(reply.ok)
            self.assertEqual(agent.task_state, "completed")


class WaitingUserTests(unittest.TestCase):
    """任务缺信息时先问用户，拿到回答再继续；期间用户随时可以停。"""

    def _agent(self, tmp, task, companion=None):
        return build(
            tmp,
            companion or ScriptProvider(reply="嗯"),
            task,
            tools=[ask_user_tool()],
            task_confirm=False,
        )

    def test_ask_user_is_offered_to_task_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = ScriptProvider(reply="好")
            agent, _, _ = self._agent(tmp, task)
            agent.respond("s", "帮我创建一个提醒")
            offered = [item["function"]["name"] for item in (task.calls[0]["tools"] or [])]
            self.assertIn(ASK_USER_TOOL, offered)

    def test_already_done_tools_are_not_run_again_after_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            hits = []
            task = ScriptProvider(
                script=[
                    ChatResult(
                        ok=True,
                        tool_calls=[
                            ToolCall(id="1", name="add_thing", arguments={"note": "买牛奶"}),
                            ToolCall(id="2", name=ASK_USER_TOOL, arguments={"question": "定在几点？"}),
                        ],
                        model="script",
                    ),
                    say("提醒已经建好了。"),
                ]
            )
            tool = fake_tool(name="add_thing", keywords=("提醒",), recorder=hits)
            agent, _, _ = build(
                tmp,
                ScriptProvider(reply="嗯"),
                task,
                tools=[tool, ask_user_tool()],
                task_confirm=False,
            )

            agent.respond("s", "帮我创建一个提醒")
            self.assertEqual(hits, [{"note": "买牛奶"}])
            self.assertEqual(agent.task_state, "waiting_user")

            resumed = agent.respond("s", "晚上九点")
            self.assertTrue(resumed.ok)
            self.assertEqual(agent.task_state, "completed")
            self.assertEqual(hits, [{"note": "买牛奶"}])

    def test_task_pauses_to_ask_user_then_resumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = ScriptProvider(
                script=[
                    tool_call(ASK_USER_TOOL, question="提醒定在几点？"),
                    say("提醒已经建好了。"),
                ]
            )
            agent, _, _ = self._agent(tmp, task)

            paused = agent.respond("s", "帮我创建一个提醒")
            self.assertTrue(paused.ok)
            self.assertEqual(paused.content, "提醒定在几点？")
            self.assertEqual(agent.task_state, "waiting_user")
            self.assertEqual(agent.mode_name, "task")
            self.assertEqual(len(task.calls), 1)
            self.assertIn("total_tokens", paused.debug)

            done = agent.respond("s", "晚上十点")
            self.assertTrue(done.ok)
            self.assertEqual(agent.task_state, "completed")
            self.assertEqual(agent.mode_name, "companion")
            self.assertEqual(len(task.calls), 2)
            self.assertEqual(task.calls[1]["messages"][-1]["content"], "晚上十点")

    def test_tool_history_carries_into_later_task_rounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            hits = []
            task = ScriptProvider(
                script=[tool_call("step_one"), tool_call("step_one"), say("两轮都做完了。")]
            )
            agent, _, _ = build(
                tmp,
                ScriptProvider(reply="嗯"),
                task,
                tools=[fake_tool(name="step_one", recorder=hits)],
                task_confirm=False,
            )
            reply = agent.respond("s", "帮我做一件事")
            self.assertTrue(reply.ok)
            self.assertEqual(len(hits), 2)
            tool_messages = [m for m in task.calls[2]["messages"] if m.get("role") == "tool"]
            self.assertEqual(len(tool_messages), 2)

    def test_stop_while_waiting_cancels_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = ScriptProvider(
                script=[tool_call(ASK_USER_TOOL, question="定在几点？"), say("不该出现")]
            )
            agent, _, _ = self._agent(tmp, task)
            agent.respond("s", "帮我创建一个提醒")
            self.assertEqual(agent.task_state, "waiting_user")

            reply = agent.respond("s", "算了")
            self.assertTrue(reply.ok)
            self.assertEqual(agent.task_state, "cancelled")
            self.assertEqual(agent.mode_name, "companion")
            self.assertEqual(agent._pending_question, "")
            self.assertEqual(len(task.calls), 1)

    def test_cancel_task_clears_pending_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = ScriptProvider(script=[tool_call(ASK_USER_TOOL, question="定在几点？")])
            agent, _, _ = self._agent(tmp, task)
            agent.respond("s", "帮我创建一个提醒")
            self.assertTrue(agent._pending_question)
            agent.cancel_task()
            self.assertEqual(agent._pending_question, "")
            self.assertEqual(agent.task_state, "cancelled")


class AvatarInterfaceTests(unittest.TestCase):
    def test_avatar_provider_keeps_its_three_methods(self):
        self.assertTrue(issubclass(DefaultAvatarProvider, AvatarProvider))
        for name in ("set_expression", "play_animation", "set_state"):
            self.assertTrue(hasattr(DefaultAvatarProvider, name))

    def test_emotion_flows_through_program_not_model(self):
        avatar = DefaultAvatarProvider()
        with tempfile.TemporaryDirectory() as tmp:
            agent, _, _ = build(tmp, ScriptProvider(reply="嗯"))
            agent.avatar = avatar
            agent.respond("s", "你好")
            self.assertEqual(avatar.expression, "smile")
            self.assertEqual(avatar.state.get("emotion"), "happy")


class AppWiringTests(unittest.TestCase):
    def test_app_assembles_without_any_model(self):
        from app.main import build_agent

        with tempfile.TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / "t.sqlite3")
            database.migrate()
            config = Config(load_env=False)
            agent, memory = build_agent(config, database, None, None)
            self.assertEqual(agent.companion_slot.status(), "unconfigured")
            self.assertEqual(agent.task_slot.status(), "unconfigured")
            self.assertEqual(agent.mode_name, "local")
            self.assertIsNotNone(memory)


if __name__ == "__main__":
    unittest.main()
