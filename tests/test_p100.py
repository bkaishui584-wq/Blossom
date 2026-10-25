"""阶段 B 验收：核心闭环与稳定性。

这一份盯四件事：
  1. Companion / Task 两个模型槽各配各的，缺一个不影响另一个，都不配也能起来。
  2. 模型报错要分得清是超时、限流、断网还是密钥不对；可恢复的按预算重试，超时不叫失败。
  3. 模型推理、工具失败、权限拒绝、真实完成四件事在数据上可区分。
  4. 人格改动即时生效；记忆能持久化、能冲突处理、能暂停学习、删掉就不再被检索到。

全程临时目录和临时数据库，不碰真实用户数据。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.context import ContextManager  # noqa: E402
from agent.core import AgentCore, NO_COMPANION_REPLY, NO_TASK_REPLY  # noqa: E402
from agent.task import (  # noqa: E402
    ERROR_AUTH,
    ERROR_NETWORK,
    ERROR_OTHER,
    ERROR_RATE_LIMIT,
    ERROR_TIMEOUT,
    TERMINAL_STATES,
    TaskState,
    classify_error,
    error_label,
    retryable,
    state_for_error,
)
from database.db import Database  # noqa: E402
from memory.manager import MemoryManager  # noqa: E402
from memory.repository import MemoryRepository  # noqa: E402
from model.base import ChatDelta, ChatResult, ModelProvider, ModelSlot, Usage  # noqa: E402
from persona.learner import HabitLearner  # noqa: E402
from persona.interface import DefaultPersonaProvider, build_persona_note  # noqa: E402
from security.config import Config  # noqa: E402
from tools.permission import PermissionLevel, PermissionManager  # noqa: E402
from tools.registry import Tool, ToolExecutor, ToolRegistry  # noqa: E402

# 路由对"目标含糊"的任务会先反问，所以这里给一个带明确对象的请求。
TASK_REQUEST = "帮我修改 README.md 文件里的说明"


class StubProvider(ModelProvider):
    """按脚本回答的假模型。errors 里有几次就先失败几次。"""

    name = "stub"

    def __init__(self, reply="在的。", errors=(), ok=True):
        self.reply = reply
        self.errors = list(errors)
        self.ok = ok
        self.calls = 0
        self.seen = []

    def is_available(self):
        return True

    def chat(self, messages, tools=None, temperature=0.7):
        self.calls += 1
        self.seen.append(list(messages))
        if self.errors:
            return ChatResult(ok=False, error=self.errors.pop(0), model="stub")
        if not self.ok:
            return ChatResult(ok=False, error="stub 就是不想答", model="stub")
        return ChatResult(
            ok=True,
            content=self.reply,
            usage=Usage(prompt_tokens=5, completion_tokens=3),
            model="stub",
        )

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"DESKPET_ROOT": str(self.base)})
        env.start()
        self.addCleanup(env.stop)
        self.config = Config(load_env=False)
        self.database = Database(self.base / "t.sqlite3")
        self.database.migrate()
        self.memory = MemoryManager(MemoryRepository(self.database))

    def agent(self, companion=None, task=None, **kwargs):
        # 两个槽都显式给全，免得构造器拿位置参数去兜底，测不出"缺一个模型"的情况。
        slots = {
            "companion_slot": ModelSlot(
                role="companion", provider=companion, configured=companion is not None
            ),
            "task_slot": ModelSlot(
                role="task", provider=task, configured=task is not None
            ),
        }
        return AgentCore(
            companion or task,
            self.memory,
            ContextManager(token_budget=800),
            persona=DefaultPersonaProvider(),
            **slots,
            **kwargs,
        )


class DualModelTests(Base):
    """两个槽各配各的。缺一个，另一个照常。"""

    def test_with_no_model_at_all_the_app_still_answers_locally(self):
        agent = self.agent()
        self.assertEqual(agent.mode_name, "local")
        reply = agent.respond("s", "现在几点")
        self.assertTrue(reply.ok, reply.error)
        self.assertTrue((reply.debug or {}).get("capability"), "本地能力要报清楚是哪一条")

    def test_only_the_task_model_means_chat_says_so_plainly(self):
        agent = self.agent(task=StubProvider())
        self.assertTrue(agent.task_slot.available())
        self.assertFalse(agent.companion_slot.available())
        self.assertEqual(agent.mode_name, "local")
        # 用一句真的需要模型的话：本地能答的那几句（打招呼、报时间）不算。
        reply = agent.respond("s", "今天心情有点复杂，随便聊聊")
        self.assertFalse(reply.ok)
        self.assertEqual(reply.error, NO_COMPANION_REPLY)

    def test_locally_answerable_chats_still_work_without_any_model(self):
        agent = self.agent()
        greeting = agent.respond("s", "在吗")
        self.assertTrue(greeting.ok, "没有模型也该有句实在的回应")
        self.assertIn("没有连接聊天模型", greeting.content)
        who = agent.respond("s", "你是谁")
        self.assertTrue(who.ok)
        self.assertIn("小蝶", who.content)

    def test_only_the_companion_model_means_tasks_say_so_plainly(self):
        agent = self.agent(companion=StubProvider())
        self.assertTrue(agent.companion_slot.available())
        self.assertFalse(agent.task_slot.available())
        self.assertEqual(agent.mode_name, "companion")
        reply = agent.respond("s", TASK_REQUEST)
        self.assertFalse(reply.ok)
        self.assertEqual(reply.error, NO_TASK_REPLY)

    def test_both_slots_answer_from_their_own_model(self):
        companion = StubProvider(reply="陪聊这边")
        task = StubProvider(reply="任务这边")
        agent = self.agent(companion=companion, task=task)
        self.assertEqual(agent.mode_name, "companion")
        chat = agent.respond("s", "在吗")
        self.assertTrue(chat.ok, chat.error)
        self.assertIn("陪聊这边", chat.content)
        self.assertEqual(companion.calls, 1)
        self.assertEqual(task.calls, 0, "闲聊不该去叫任务模型")


class ErrorKindTests(unittest.TestCase):
    """报错先分类，再决定重试还是直接告诉用户。"""

    def test_common_errors_are_recognised(self):
        cases = (
            ("Request timed out.", ERROR_TIMEOUT),
            ("Error code: 429 - rate limit reached", ERROR_RATE_LIMIT),
            ("Connection error: getaddrinfo failed", ERROR_NETWORK),
            ("401 Incorrect API key provided", ERROR_AUTH),
            ("总之一句话说不清", ERROR_OTHER),
        )
        for text, want in cases:
            self.assertEqual(classify_error(text), want, text)

    def test_only_recoverable_errors_are_worth_retrying(self):
        self.assertTrue(retryable(ERROR_TIMEOUT))
        self.assertTrue(retryable(ERROR_RATE_LIMIT))
        self.assertTrue(retryable(ERROR_NETWORK))
        self.assertFalse(retryable(ERROR_AUTH))
        self.assertFalse(retryable(ERROR_OTHER))

    def test_a_timeout_is_not_reported_as_a_failure(self):
        self.assertEqual(state_for_error(ERROR_TIMEOUT), TaskState.TIMEOUT.value)
        self.assertEqual(state_for_error(ERROR_AUTH), TaskState.FAILED.value)
        self.assertIn(TaskState.TIMEOUT.value, TERMINAL_STATES)
        self.assertEqual(error_label(ERROR_TIMEOUT), "模型没有在时限内回应")


class TaskModelFailureTests(Base):
    """任务模型出错时，状态和话都要对得上。"""

    def run_task(self, provider, **kwargs):
        kwargs.setdefault("task_confirm", False)
        agent = self.agent(companion=StubProvider(), task=provider, **kwargs)
        reply = agent.respond("s", TASK_REQUEST)
        return agent, reply

    def test_a_timeout_ends_as_timeout_not_failed(self):
        agent, reply = self.run_task(StubProvider(errors=["Request timed out."] * 4))
        self.assertFalse(reply.ok)
        self.assertEqual(agent.task_state, TaskState.TIMEOUT.value)
        self.assertIn("时限内回应", reply.error)

    def test_a_rate_limit_is_retried_once_then_succeeds(self):
        provider = StubProvider(reply="做完了。", errors=["Error code: 429 rate limit"])
        agent, reply = self.run_task(provider)
        self.assertTrue(reply.ok, reply.error)
        self.assertEqual(provider.calls, 2, "限流要重试一次才放弃")

    def test_an_auth_error_is_not_retried(self):
        provider = StubProvider(errors=["401 Invalid API key"])
        agent, reply = self.run_task(provider)
        self.assertFalse(reply.ok)
        self.assertEqual(provider.calls, 1, "密钥不对重试没有意义")
        self.assertEqual(agent.task_state, TaskState.FAILED.value)
        self.assertIn("密钥或权限不对", reply.error)

    def test_the_kind_is_written_into_the_debug_payload(self):
        agent, reply = self.run_task(StubProvider(errors=["Request timed out."] * 4))
        self.assertEqual(reply.debug.get("model_error_kind"), ERROR_TIMEOUT)
        self.assertGreaterEqual(reply.debug.get("model_attempts", 0), 1)


class BoundaryTests(unittest.TestCase):
    """模型推理、工具失败、权限拒绝、真实完成，四件事数据上分得开。"""

    def make_executor(self, confirm=False):
        registry = ToolRegistry()
        registry.register(
            Tool(
                name="read_thing",
                description="只读",
                permission=PermissionLevel.READ.value,
                handler=lambda: "读到了",
                summarize=lambda data: str(data),
            )
        )
        registry.register(
            Tool(
                name="risky_thing",
                description="高风险",
                permission=PermissionLevel.HIGH.value,
                handler=lambda: "做了",
            )
        )
        registry.register(
            Tool(
                name="broken_thing",
                description="会炸",
                permission=PermissionLevel.READ.value,
                handler=lambda: (_ for _ in ()).throw(RuntimeError("炸了")),
            )
        )
        permissions = PermissionManager(confirm_callback=(lambda *a: True) if confirm else None)
        return ToolExecutor(registry, permissions)

    def test_a_real_completion_is_marked_ok(self):
        outcome = self.make_executor().execute("read_thing", {})
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.allowed)
        self.assertEqual(outcome.result, "读到了")

    def test_a_permission_denial_is_not_a_failure(self):
        outcome = self.make_executor(confirm=False).execute("risky_thing", {})
        self.assertFalse(outcome.ok)
        self.assertFalse(outcome.allowed, "没批准要说成没批准，不是执行失败")
        self.assertTrue(outcome.needs_confirmation)

    def test_a_tool_that_throws_is_reported_as_failed(self):
        outcome = self.make_executor().execute("broken_thing", {})
        self.assertFalse(outcome.ok)
        self.assertTrue(outcome.allowed, "它被允许了，只是自己炸了")
        self.assertIn("炸了", str(outcome.error))

    def test_an_unknown_tool_is_neither_allowed_nor_run(self):
        outcome = self.make_executor().execute("压根没有这个工具", {})
        self.assertFalse(outcome.ok)
        self.assertFalse(outcome.allowed)
        self.assertIn("未知工具", outcome.error)


class MemoryBehaviourTests(Base):
    """记忆：持久化、冲突、删除、暂停学习。"""

    def test_memory_survives_a_new_manager(self):
        self.memory.remember("用户喜欢深色主题", source="USER_EXPLICIT")
        fresh = MemoryManager(MemoryRepository(self.database))
        hits = fresh.search("深色主题")
        self.assertTrue(hits, "换个管理器读同一个库，记忆还在")

    def test_an_unknown_kind_is_stored_somewhere_searchable(self):
        """写进去却搜不到的记忆等于没记，不许悄悄发生。"""
        self.memory.remember("用户的工位在第三排", kind="随便编的层", source="USER_EXPLICIT")
        hits = self.memory.search("工位")
        self.assertTrue(hits, "没登记的层要归到能检索的那一层")
        self.assertEqual(hits[0]["kind"], "long_term")

    def test_every_stored_memory_is_reachable_by_search(self):
        for kind in ("long_term", "project", "relationship", "feedback"):
            self.memory.remember("第 %s 层的记录" % kind, kind=kind, source="USER_EXPLICIT")
        for kind in ("long_term", "project", "relationship", "feedback"):
            hits = self.memory.search("层的记录")
            self.assertTrue(hits, kind)
        kinds = {row["kind"] for row in self.memory.search("层的记录")}
        self.assertEqual(kinds, {"long_term", "project", "relationship", "feedback"})

    def test_a_stable_preference_is_not_overwritten_by_a_new_statement(self):
        self.memory.set_preference("persona.name", "小蝶")
        self.memory.remember("用户说以后叫小桌也行", source="USER_EXPLICIT")
        self.assertEqual(self.memory.get_preference("persona.name"), "小蝶")

    def test_a_forgotten_memory_is_not_found_again(self):
        memory_id = self.memory.remember("用户的工位在第 3 排", source="USER_EXPLICIT")
        self.assertTrue(self.memory.search("工位"))
        self.assertTrue(self.memory.forget(memory_id, confirm=True))
        self.assertFalse(self.memory.search("工位"), "删掉之后不能再被检索到")
        fresh = MemoryManager(MemoryRepository(self.database))
        self.assertFalse(fresh.search("工位"), "换个管理器也读不到")

    def test_forgetting_needs_confirmation(self):
        memory_id = self.memory.remember("临时记一下", source="USER_EXPLICIT")
        self.assertFalse(self.memory.forget(memory_id, confirm=False))
        self.assertTrue(self.memory.search("临时记一下"))

    def test_learning_can_be_paused_and_resumed(self):
        # 学习结果要落进偏好存储；没有存储时它不会假装记下了。
        learner = HabitLearner(preferences=self.memory)
        learner.set_enabled(False)
        self.assertFalse(learner.enabled())
        learner.observe("回答短一点")
        self.assertEqual(learner.rows(), [], "暂停学习时不该记下任何候选")
        learner.set_enabled(True)
        learner.observe("回答短一点")
        self.assertTrue(learner.rows(), "恢复学习后照常记")

    def test_learning_without_a_store_reports_nothing_instead_of_pretending(self):
        learner = HabitLearner(preferences=None)
        self.assertEqual(learner.observe("回答短一点"), [], "存不下去就如实返回空")


class PersonaRuntimeTests(Base):
    """人格改了要马上生效，不用重启。"""

    def test_a_persona_change_reaches_the_runtime(self):
        provider = StubProvider()
        agent = self.agent(companion=provider)
        agent.persona.name = "小蝶"
        self.assertEqual(agent.persona.name, "小蝶")
        self.assertIn("小蝶", build_persona_note(agent.persona))
        self.assertTrue(agent.respond("s", "在吗").ok)
        system = "\n".join(
            str(item.get("content") or "")
            for item in provider.seen[-1]
            if item.get("role") == "system"
        )
        self.assertIn("小蝶", system, "改完名字下一次对话就该用上")


if __name__ == "__main__":
    unittest.main()
