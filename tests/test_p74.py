"""阶段 P74 验收：V7 阶段 3 的任务中心。

要点：
    列表、计数、筛选只报数据库里真实有的；
    「进行中」这类筛掉已经收尾的任务；
    详情把目标和每一步摊开，没有分步计划就如实说；
    没在跑就不假装取消成功；
    「接着做」只把目标放回输入框，发不发由用户决定；
    任务中心只开一个窗口。
"""

import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.context import ContextManager
from agent.core import AgentCore
from agent.task import TaskRecord, TaskState, TaskStep, new_task_id
from agent.task_store import TaskStore
from database.db import Database
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatDelta, ChatResult, ModelProvider, Usage
from security.config import Config
from ui.desktop import DesktopPet
from ui.task_view import FILTERS, TaskWindow


class StubProvider(ModelProvider):
    name = "stub"

    def is_available(self):
        return True

    def chat(self, messages, tools=None, temperature=0.7):
        return ChatResult(ok=True, content="在的。", usage=Usage(5, 3), model="stub")

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


class FakeLabel:
    def __init__(self):
        self.text = ""

    def configure(self, **kwargs):
        if "text" in kwargs:
            self.text = str(kwargs["text"])


class FakeAgent:
    def __init__(self, store=None):
        self.task_store = store
        self.cancel_calls = 0

    def cancel_task(self):
        self.cancel_calls += 1
        return True


class FakePet:
    """只带任务中心要用到的那几样。"""

    def __init__(self, store=None, agent=None, in_flight=None):
        self.agent = agent if agent is not None else FakeAgent(store)
        if in_flight is not None:
            self._task_in_flight = in_flight
        self.prefilled = []

    def prefill_chat(self, text):
        self.prefilled.append(text)
        return text


class NullLogger:
    def _noop(self, *args, **kwargs):
        return None

    info = warning = error = debug = exception = _noop


def build_store(tmp):
    database = Database(Path(tmp) / "tasks.sqlite3")
    database.migrate()
    return TaskStore(database)


def seed(store, goal, state, steps=None, **kwargs):
    record = TaskRecord(id=new_task_id(), goal=goal, state=state, session="desktop", **kwargs)
    store.save(record)
    if steps:
        store.replace_steps(record.id, [TaskStep(**row) for row in steps])
    return record


def head(store=None, in_flight=None, agent=None):
    """不给窗口开真界面，只要那几个读数的方法。"""
    window = TaskWindow.__new__(TaskWindow)
    window.pet = FakePet(store=store, agent=agent, in_flight=in_flight)
    window.filter = ""
    window.selected = ""
    window.hint = FakeLabel()
    window.refresh = lambda: None
    return window


class TaskRowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = build_store(self.tmp.name)

    def test_rows_read_what_is_in_the_store(self):
        seed(self.store, "整理会议记录", TaskState.COMPLETED.value)
        seed(self.store, "查一下天气", TaskState.RUNNING.value)
        rows = head(self.store).rows("")
        self.assertEqual(len(rows), 2)
        goals = {row["goal"] for row in rows}
        self.assertEqual(goals, {"整理会议记录", "查一下天气"})

    def test_a_row_carries_the_plain_language_state(self):
        seed(self.store, "写周报", TaskState.WAITING_USER.value)
        row = head(self.store).rows("")[0]
        self.assertEqual(row["state"], "waiting_user")
        self.assertEqual(row["state_label"], "等待用户")

    def test_counting_by_state(self):
        seed(self.store, "a", TaskState.COMPLETED.value)
        seed(self.store, "b", TaskState.COMPLETED.value)
        seed(self.store, "c", TaskState.FAILED.value)
        seed(self.store, "d", TaskState.RUNNING.value)
        counts = head(self.store).counts()
        self.assertEqual(counts.get("completed"), 2)
        self.assertEqual(counts.get("failed"), 1)
        self.assertEqual(counts.get("running"), 1)

    def test_the_open_filter_drops_finished_ones(self):
        seed(self.store, "还在跑", TaskState.RUNNING.value)
        seed(self.store, "等用户", TaskState.WAITING_USER.value)
        seed(self.store, "做完了", TaskState.COMPLETED.value)
        seed(self.store, "失败了", TaskState.FAILED.value)
        seed(self.store, "取消了", TaskState.CANCELLED.value)
        window = head(self.store)
        states = {row["state"] for row in window.rows("open")}
        self.assertEqual(states, {"running", "waiting_user"})

    def test_one_state_filter_keeps_only_that_one(self):
        seed(self.store, "做完了", TaskState.COMPLETED.value)
        seed(self.store, "还在跑", TaskState.RUNNING.value)
        window = head(self.store)
        rows = window.rows("completed")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["goal"], "做完了")

    def test_filters_cover_every_face_the_window_shows(self):
        names = {value for _label, value in FILTERS}
        self.assertIn("", names)
        self.assertIn("open", names)
        self.assertIn("completed", names)
        self.assertIn("failed", names)
        self.assertIn("cancelled", names)

    def test_no_store_at_all_is_not_a_crash(self):
        window = head(None)
        self.assertEqual(window.rows(""), [])
        self.assertEqual(window.counts(), {})
        self.assertEqual(window.steps_of("nope"), [])

    def test_in_flight_is_false_without_a_checker(self):
        self.assertFalse(head(self.store).in_flight())

    def test_in_flight_asks_the_pet(self):
        self.assertTrue(head(self.store, in_flight=lambda: True).in_flight())


class TaskDetailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = build_store(self.tmp.name)

    def test_detail_lists_the_goal_and_every_step(self):
        record = seed(
            self.store,
            "把周报整理成表格",
            TaskState.COMPLETED.value,
            steps=[
                {"index": 0, "title": "读原始周报", "tool": "read_file", "status": "done",
                 "result": "共 12 条"},
                {"index": 1, "title": "写成表格", "tool": "write_file", "status": "done"},
            ],
            project="C:/work",
        )
        lines = head(self.store).describe_task(record.id)
        flat = ["%s|%s" % (label, value) for label, value in lines]
        self.assertTrue(any("目标|把周报整理成表格" in item for item in flat), flat)
        self.assertTrue(any("已完成" in item for item in flat), flat)
        self.assertTrue(any("读原始周报" in item and "read_file" in item for item in flat), flat)
        self.assertTrue(any("结果：共 12 条" in item for item in flat), flat)

    def test_detail_says_so_when_there_is_no_step_plan(self):
        record = seed(self.store, "查一下明天天气", TaskState.COMPLETED.value)
        lines = head(self.store).describe_task(record.id)
        self.assertTrue(any("没有分步计划" in value for _label, value in lines), lines)

    def test_detail_keeps_the_error_when_there_was_one(self):
        record = seed(self.store, "打开不存在的文件", TaskState.FAILED.value, error="文件不存在")
        lines = head(self.store).describe_task(record.id)
        self.assertTrue(any(label == "错误" and "文件不存在" in value for label, value in lines), lines)

    def test_detail_of_a_task_nobody_knows_is_empty(self):
        self.assertEqual(head(self.store).describe_task("task-nope"), [])

    def test_detail_text_prompts_when_nothing_is_picked(self):
        window = head(self.store)
        self.assertIn("选一条任务", window.detail_text())

    def test_detail_text_of_an_unknown_pick_reads_as_lost(self):
        window = head(self.store)
        window.selected = "task-nope"
        self.assertIn("读不到", window.detail_text())

    def test_detail_text_has_the_goal_in_it(self):
        record = seed(self.store, "整理照片", TaskState.COMPLETED.value)
        window = head(self.store)
        window.selected = record.id
        self.assertIn("整理照片", window.detail_text())


class TaskActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = build_store(self.tmp.name)

    def test_cancel_says_so_when_nothing_is_running(self):
        window = head(self.store, in_flight=lambda: False)
        self.assertFalse(window.cancel_current())
        self.assertIn("没有正在跑", window.hint.text)

    def test_cancel_calls_the_agent_when_something_is_running(self):
        agent = FakeAgent(self.store)
        window = head(self.store, in_flight=lambda: True, agent=agent)
        self.assertTrue(window.cancel_current())
        self.assertEqual(agent.cancel_calls, 1)
        self.assertIn("已取消", window.hint.text)

    def test_cancel_reports_a_real_failure(self):
        class Broken(FakeAgent):
            def cancel_task(self):
                raise RuntimeError("忙不过来")

        window = head(self.store, in_flight=lambda: True, agent=Broken(self.store))
        self.assertFalse(window.cancel_current())
        self.assertIn("忙不过来", window.hint.text)

    def test_resume_puts_the_goal_back_without_sending(self):
        record = seed(self.store, "把上周的周报补上", TaskState.WAITING_USER.value)
        pet = FakePet(store=self.store)
        window = head(self.store)
        window.pet = pet
        window.selected = record.id
        self.assertEqual(window.resume(), "把上周的周报补上")
        self.assertEqual(pet.prefilled, ["把上周的周报补上"])
        self.assertIn("按发送", window.hint.text)

    def test_resume_asks_first_when_nothing_is_picked(self):
        pet = FakePet(store=self.store)
        window = head(self.store)
        window.pet = pet
        self.assertFalse(window.resume())
        self.assertEqual(pet.prefilled, [])

    def test_resume_says_so_when_the_task_has_no_goal(self):
        record = seed(self.store, "   ", TaskState.WAITING_USER.value)
        pet = FakePet(store=self.store)
        window = head(self.store)
        window.pet = pet
        window.selected = record.id
        self.assertFalse(window.resume())
        self.assertEqual(pet.prefilled, [])
        self.assertIn("没有留下目标", window.hint.text)


class TaskWindowTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        self.root.withdraw()
        self.addCleanup(self._safe_destroy)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _safe_destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def build_pet(self):
        database = Database(Path(self.tmp.name) / "t.sqlite3")
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        agent = AgentCore(StubProvider(), memory, ContextManager(token_budget=800))
        agent.task_store = TaskStore(database)
        pet = DesktopPet(agent, Config(load_env=False), memory=memory, root=self.root)
        return pet, agent.task_store

    def test_menu_entry_is_wired(self):
        pet, _ = self.build_pet()
        self.assertIn(("任务", "open_tasks"), DesktopPet.MENU_ITEMS)
        self.assertTrue(callable(pet.open_tasks))

    def test_the_chat_bar_button_opens_it(self):
        pet, _ = self.build_pet()
        pet.chat.tasks_button.invoke()
        # 任务中心现在是主工作台里的一页，按钮点完要真的把那一页推到前面。
        self.assertEqual(pet.chat.page_key, "tasks")
        window = pet.open_tasks()
        self.assertIsInstance(window, TaskWindow)
        self.addCleanup(window.win.destroy)

    def test_window_only_opens_once(self):
        pet, _ = self.build_pet()
        first = pet.open_tasks()
        self.addCleanup(first.win.destroy)
        self.assertIs(first, pet.open_tasks())

    def test_the_window_shows_what_the_store_has(self):
        pet, store = self.build_pet()
        seed(store, "跑一下测试", TaskState.RUNNING.value)
        seed(store, "写文档", TaskState.COMPLETED.value)
        window = pet.open_tasks()
        self.addCleanup(window.win.destroy)
        self.assertEqual(len(window.list.get_children()), 2)
        window.set_filter("completed")
        self.assertEqual(len(window.list.get_children()), 1)
        window.set_filter("open")
        self.assertEqual(len(window.list.get_children()), 1)
        window.set_filter("")
        self.assertEqual(len(window.list.get_children()), 2)

    def test_the_window_survives_an_empty_store(self):
        pet, _ = self.build_pet()
        window = pet.open_tasks()
        self.addCleanup(window.win.destroy)
        self.assertEqual(len(window.list.get_children()), 0)
        self.assertEqual(window.selected, "")

    def test_prefill_chat_fills_the_box_and_does_not_send(self):
        pet, _ = self.build_pet()
        sent = []
        pet.chat.send = lambda *args, **kwargs: sent.append(args)
        self.assertEqual(pet.prefill_chat("把上周的周报补上"), "把上周的周报补上")
        self.assertEqual(pet.chat.read_input(), "把上周的周报补上")
        self.assertEqual(sent, [])
        self.assertEqual(pet.prefill_chat("   "), "")


class ProductTests(unittest.TestCase):
    """任务产物：只列真在磁盘上的文件，编不出来就不列。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = build_store(self.tmp.name)

    def test_products_list_real_files_only(self):
        real = Path(self.tmp.name) / "报告.md"
        real.write_text("ok", encoding="utf-8")
        record = seed(
            self.store, "写报告", TaskState.COMPLETED.value,
            steps=[{"index": 0, "title": "写文件", "status": "done",
                    "result": "已写入 %s" % real}],
        )
        self.assertEqual(head(self.store).products_of(record.id), [str(real)])

    def test_a_path_that_does_not_exist_is_not_a_product(self):
        record = seed(
            self.store, "写报告", TaskState.COMPLETED.value,
            steps=[{"index": 0, "title": "写文件", "status": "done",
                    "result": "写到 C:/nope/没有这份.md 了"}],
        )
        self.assertEqual(head(self.store).products_of(record.id), [])

    def test_describe_task_carries_the_products(self):
        real = Path(self.tmp.name) / "笔记.txt"
        real.write_text("ok", encoding="utf-8")
        record = seed(
            self.store, "记笔记", TaskState.COMPLETED.value,
            steps=[{"index": 0, "title": "写文件", "status": "done",
                    "result": "已写入 %s" % real}],
        )
        lines = head(self.store).describe_task(record.id)
        self.assertIn(("产物", str(real)), lines)


if __name__ == "__main__":
    unittest.main()