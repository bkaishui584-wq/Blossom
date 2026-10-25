"""P48：任务规划、执行边界与持久化（V3 阶段 C / D）。

这一层要保证的事：
    任务状态覆盖规划、等确认、验证、取消这些中间态；
    任务有明确上限：轮数、工具次数、时间，到顶就停下来问；
    任务记录写进 SQLite，重启后还能翻出来；
    上次没跑完的任务只列出来给用户看，绝不自动重跑；
    取消之后不再执行下一步；
    项目记忆只记真实发生的事，来源标成程序观察。
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.context import ContextManager
from agent.core import AgentCore
from agent.task import (
    TASK_STATE_LABELS,
    TERMINAL_STATES,
    TaskLimits,
    TaskPlan,
    TaskRecord,
    TaskState,
    TaskStep,
)
from agent.task_store import TaskStore
from database.db import Database
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatResult, ModelProvider, ToolCall, Usage
from security.audit import AuditLog
from security.config import Config
from tools.builtin import register_default_tools
from tools.permission import PermissionManager
from tools.registry import Tool, ToolExecutor, ToolRegistry


class ScriptProvider(ModelProvider):
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


def tool_call(name, **arguments):
    return ChatResult(
        ok=True,
        tool_calls=[ToolCall(id=name, name=name, arguments=arguments)],
        usage=Usage(prompt_tokens=3, completion_tokens=1),
        model="script",
    )


def counting_tool(name, recorder):
    def handler(**kwargs):
        recorder.append(kwargs)
        return "ok"

    return Tool(
        name=name,
        description="测试工具",
        keywords=("文件", "任务"),
        permission="READ",
        handler=handler,
    )


def cancelling_tool(name, recorder, holder):
    """第一次被调用时就模拟用户喊停，用来验证取消之后不会再跑下一步。"""

    def handler(**kwargs):
        recorder.append(kwargs)
        agent = holder.get("agent")
        if agent is not None:
            agent.cancel_task()
        return "ok"

    return Tool(
        name=name,
        description="测试工具",
        keywords=("文件", "任务"),
        permission="READ",
        handler=handler,
    )


def build(tmp, companion=None, task=None, tools=(), store=True):
    database = Database(Path(tmp) / "t.sqlite3")
    database.migrate()
    audit = AuditLog(database)
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    executor = ToolExecutor(
        registry, PermissionManager(confirm_callback=lambda *a: True, audit=audit), audit=audit
    )
    memory = MemoryManager(MemoryRepository(database))
    agent = AgentCore(
        companion,
        memory,
        ContextManager(token_budget=800),
        executor=executor,
        task_provider=task,
        task_store=TaskStore(database) if store else None,
        task_confirm=False,
    )
    return agent, database, memory


class TaskStateTests(unittest.TestCase):
    def test_new_states_exist(self):
        for name in ("PLANNING", "WAITING_CONFIRMATION", "VERIFYING"):
            self.assertTrue(hasattr(TaskState, name), name)

    def test_every_state_has_a_chinese_label(self):
        for state in TaskState:
            self.assertIn(state.value, TASK_STATE_LABELS)

    def test_terminal_states_are_the_endings(self):
        # 四种收尾：做完、失败、超时、被取消。超时单独一类，不并进失败。
        self.assertEqual(len(TERMINAL_STATES), 4)
        self.assertIn(TaskState.CANCELLED.value, TERMINAL_STATES)
        self.assertIn(TaskState.TIMEOUT.value, TERMINAL_STATES)


class TaskPlanTests(unittest.TestCase):
    def test_current_step_is_the_first_unfinished_one(self):
        plan = TaskPlan(goal="改文件", steps=[
            TaskStep(index=0, title="读", status="done"),
            TaskStep(index=1, title="写"),
        ])
        self.assertEqual(plan.current.index, 1)

    def test_plan_without_pending_steps_has_no_current(self):
        plan = TaskPlan(goal="写", steps=[TaskStep(index=0, title="写", status="done")])
        self.assertIsNone(plan.current)

    def test_limits_have_a_ceiling_for_everything(self):
        data = TaskLimits().to_dict()
        for key in ("max_rounds", "max_tool_calls", "max_seconds", "max_retries"):
            self.assertGreater(data[key], 0)


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "t.sqlite3")
        self.applied = self.db.migrate()
        self.store = TaskStore(self.db)

    def test_migration_is_applied_and_repeatable(self):
        self.assertIn(11, self.applied)
        self.assertEqual(self.db.migrate(), [])

    def test_tables_exist(self):
        names = [row["name"] for row in self.db.query("PRAGMA table_info(tasks)")]
        self.assertIn("pending_question", names)
        step_names = [row["name"] for row in self.db.query("PRAGMA table_info(task_steps)")]
        self.assertIn("verify", step_names)

    def test_round_trip(self):
        record = TaskRecord(goal="改 main.py", session="desk", project="main.py")
        self.store.save(record)
        loaded = self.store.get(record.id)
        self.assertEqual(loaded.goal, "改 main.py")
        self.assertEqual(loaded.project, "main.py")

    def test_saving_twice_updates_instead_of_duplicating(self):
        record = TaskRecord(goal="一件事")
        self.store.save(record)
        record.state = TaskState.RUNNING.value
        self.store.save(record)
        self.assertEqual(self.store.stats()["total"], 1)
        self.assertEqual(self.store.get(record.id).state, TaskState.RUNNING.value)

    def test_steps_are_replaced_not_appended(self):
        record = TaskRecord(goal="一件事")
        self.store.save(record)
        self.store.replace_steps(record.id, [TaskStep(index=0, title="读")])
        self.store.replace_steps(record.id, [
            TaskStep(index=0, title="读", status="done"),
            TaskStep(index=1, title="写"),
        ])
        rows = self.store.steps(record.id)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["status"], "done")

    def test_unfinished_only_lists_live_tasks(self):
        done = TaskRecord(goal="做完了", state=TaskState.COMPLETED.value)
        live = TaskRecord(goal="还没完", state=TaskState.RUNNING.value)
        self.store.save(done)
        self.store.save(live)
        goals = [row.goal for row in self.store.unfinished()]
        self.assertEqual(goals, ["还没完"])

    def test_interrupted_run_becomes_waiting_for_user(self):
        record = TaskRecord(goal="跑一半", state=TaskState.RUNNING.value)
        self.store.save(record)
        self.store.mark_interrupted()
        self.assertEqual(
            self.store.get(record.id).state, TaskState.WAITING_USER.value
        )

    def test_history_survives_a_new_connection(self):
        record = TaskRecord(goal="历史任务")
        self.store.save(record)
        fresh = TaskStore(Database(self.db.path))
        self.assertEqual(fresh.get(record.id).goal, "历史任务")


class TaskBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_tool_call_ceiling_stops_the_loop(self):
        hits = []
        task = ScriptProvider(script=[tool_call("touch", path="a")] * 6, reply="好了。")
        agent, _, _ = build(
            self.tmp.name, ScriptProvider(available=False), task,
            tools=[counting_tool("touch", hits)],
        )
        agent.limits = TaskLimits(max_rounds=6, max_tool_calls=2, max_seconds=600)
        reply = agent.respond("s", "帮我改文件")
        self.assertLessEqual(len(hits), 2)
        self.assertTrue(reply.ok)
        self.assertIn("上限", reply.content)

    def test_limit_hit_is_recorded_in_the_debug(self):
        hits = []
        task = ScriptProvider(script=[tool_call("touch", path="a")] * 6, reply="好了。")
        agent, _, _ = build(
            self.tmp.name, ScriptProvider(available=False), task,
            tools=[counting_tool("touch", hits)],
        )
        agent.limits = TaskLimits(max_rounds=6, max_tool_calls=1, max_seconds=600)
        reply = agent.respond("s", "帮我改文件")
        self.assertIn("limit_hit", reply.debug)

    def test_budget_helper_is_honest_when_there_is_room(self):
        agent, _, _ = build(self.tmp.name, ScriptProvider(available=False), ScriptProvider())
        self.assertEqual(agent._out_of_budget(), "")


class TaskLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_finished_task_is_persisted_with_its_state(self):
        agent, db, _ = build(self.tmp.name, ScriptProvider(available=False), ScriptProvider())
        agent.respond("s", "帮我改文件")
        record = agent.task_record
        self.assertIsNotNone(record)
        self.assertIn(record.state, TERMINAL_STATES)
        self.assertEqual(agent.task_store.get(record.id).state, record.state)

    def test_completed_task_writes_project_memory(self):
        agent, _, memory = build(
            self.tmp.name, ScriptProvider(available=False), ScriptProvider()
        )
        agent.respond("s", "帮我改 main.py")
        rows = memory.browse(kind="project", limit=5)
        self.assertTrue(rows)
        self.assertIn("main.py", rows[0]["content"])

    def _start_then_cancel(self):
        """任务跑到一半喊停：工具第一次执行时触发取消。"""
        hits = []
        holder = {}
        task = ScriptProvider(script=[tool_call("touch", path="a")] * 4, reply="好了。")
        agent, _, _ = build(
            self.tmp.name,
            ScriptProvider(available=False),
            task,
            tools=[cancelling_tool("touch", hits, holder)],
        )
        holder["agent"] = agent
        return agent, hits

    def test_cancel_stops_further_steps(self):
        agent, hits = self._start_then_cancel()
        reply = agent.respond("s", "帮我改文件")
        self.assertEqual(len(hits), 1)
        self.assertIn("取消", reply.content)

    def test_cancel_is_recorded_as_cancelled(self):
        agent, _ = self._start_then_cancel()
        agent.respond("s", "帮我改文件")
        self.assertEqual(agent.task_state, TaskState.CANCELLED.value)
        self.assertEqual(
            agent.task_store.get(agent.task_record.id).state, TaskState.CANCELLED.value
        )

    def test_cancel_before_a_new_task_does_not_block_it(self):
        """取消只针对进行中的任务；之后的新指令照常执行。"""
        hits = []
        agent, _, _ = build(
            self.tmp.name,
            ScriptProvider(available=False),
            ScriptProvider(script=[tool_call("touch", path="a")]),
            tools=[counting_tool("touch", hits)],
        )
        agent.cancel_task()
        agent.respond("s", "帮我改文件")
        self.assertEqual(len(hits), 1)

    def test_unfinished_tasks_are_listed_not_resumed(self):
        agent, _, _ = build(self.tmp.name, ScriptProvider(available=False), ScriptProvider())
        agent._begin_task_record("s", "帮我改 main.py")
        agent.task_state = TaskState.RUNNING.value
        agent.save_task_record()
        rows = agent.unfinished_tasks()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].state, TaskState.RUNNING.value)

    def test_task_history_is_readable(self):
        agent, _, _ = build(self.tmp.name, ScriptProvider(available=False), ScriptProvider())
        agent.respond("s", "帮我改文件")
        self.assertTrue(agent.task_history())

    def test_agent_without_store_still_runs(self):
        agent, _, _ = build(
            self.tmp.name, ScriptProvider(available=False), ScriptProvider(), store=False
        )
        reply = agent.respond("s", "帮我改文件")
        self.assertTrue(reply.ok)
        self.assertEqual(agent.unfinished_tasks(), [])

class ToolVerifyTests(unittest.TestCase):
    """工具可以声明执行后校验；没声明的如实算「未校验」。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "t.sqlite3")
        self.db.migrate()
        self.registry = ToolRegistry()
        register_default_tools(self.registry, Config(load_env=False), self.db)
        self.executor = ToolExecutor(
            self.registry, PermissionManager(confirm_callback=lambda *a: True)
        )

    def test_calendar_write_is_checked_after_it_lands(self):
        outcome = self.executor.execute(
            "add_calendar_event", {"title": "测试日程", "when": "2026-05-05 09:00"}
        )
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.verified)
        self.assertTrue(outcome.checks)

    def test_tool_without_a_checker_is_marked_unverified(self):
        recorder = []
        plain = counting_tool("plain", recorder)
        self.registry.register(plain)
        outcome = self.executor.execute("plain", {})
        self.assertTrue(outcome.ok)
        self.assertFalse(outcome.verified)
        self.assertEqual(outcome.checks, [])


if __name__ == "__main__":
    unittest.main()
