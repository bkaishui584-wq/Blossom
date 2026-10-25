"""P4 验收：Feed、Ideas、Goals 与连续性上下文。

覆盖：首页信息流每张卡能单独忽略、忽略只影响这一次、以后不提才写进偏好、
灵感建议必须写清依据并且没有依据就不提、目标按真实步骤摆里程碑、
没有任务模型时不假装接着做、上次没做完的活只报事实、做完的不再当成待续。
全程临时数据库，不碰真实用户数据。
"""

import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path

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
from persona.interface import DefaultPersonaProvider
from persona.learner import HabitLearner
from security.config import Config
from ui import theme
from ui.desktop import DesktopPet


class SilentProvider(ModelProvider):
    """一个没配好的模型：不该被调用。"""

    name = "silent"

    def is_available(self):
        return False

    def chat(self, messages, tools=None, temperature=0.7):
        return ChatResult(ok=False, error="没配模型", usage=Usage(), model="silent")

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


class TkCase(unittest.TestCase):
    """真窗口测试，桌面宠物按启动路径建出来。"""

    def setUp(self):
        self.addCleanup(theme.reset_scale)
        theme.reset_scale()
        try:
            self.root = tk.Tk()
        except Exception as exc:  # pragma: no cover
            self.skipTest("tkinter 不可用：%s" % exc)
        self.root.withdraw()
        self.addCleanup(self._destroy)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.config = Config(load_env=False)
        self.config.data_dir = base
        self.config.log_dir = base / "logs"
        self.config.db_path = base / "t.sqlite3"
        self.config.characters_dir = base / "characters"
        self.config.live2d_dir = base / "live2d"
        self.config.devices_dir = base / "devices"
        self.config.tray = False

    def _destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def build_pet(self):
        database = Database(self.config.db_path)
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        agent = AgentCore(
            SilentProvider(), memory, ContextManager(token_budget=800),
            persona=DefaultPersonaProvider(),
        )
        agent.task_store = TaskStore(database)
        # 学习接线和 app/main.py 一样。
        agent.learning = HabitLearner(preferences=memory, persona=agent.persona)
        agent.learning.apply()
        pet = DesktopPet(agent, self.config, memory=memory, root=self.root)
        self.addCleanup(pet.close)
        pet.show_chat()
        return pet, memory, database

    def seed_task(self, store, goal, state, steps=None):
        record = TaskRecord(id=new_task_id(), goal=goal, state=state, session="desktop")
        store.save(record)
        if steps:
            store.replace_steps(record.id, [TaskStep(**row) for row in steps])
        return record

    def learn_habit(self, pet, times=3):
        for _ in range(times):
            pet.agent.learning.observe("以后回答简短一点")
        return pet.agent.learning.rows()

    def feed_keys(self, pet):
        return [card.get("key") for card in pet.home_feed()]


class FeedTests(TkCase):
    """首页信息流：每张卡能单独忽略。"""

    def test_every_card_can_be_ignored_on_its_own(self):
        pet, _memory, _db = self.build_pet()
        cards = pet.home_feed()
        self.assertTrue(cards)
        for card in cards:
            self.assertTrue(card.get("key"), card)
            actions = [action for _label, action in card.get("actions") or []]
            self.assertIn("dismiss:%s" % card["key"], actions, card)

    def test_ignoring_a_card_only_hides_that_card(self):
        pet, _memory, database = self.build_pet()
        self.seed_task(pet.agent.task_store, "写周报", TaskState.RUNNING.value)
        before = self.feed_keys(pet)
        self.assertIn("today", before)
        pet.dismiss_feed("today")
        after = self.feed_keys(pet)
        self.assertNotIn("today", after)
        # 数据一条都没动。
        self.assertEqual(len(pet.task_rows()), 1)
        self.assertIn("progress", after)

    def test_ignoring_lasts_only_until_the_window_closes(self):
        pet, _memory, _db = self.build_pet()
        pet.dismiss_feed("today")
        self.assertNotIn("today", self.feed_keys(pet))
        # 重开一次窗口，上一轮的忽略就忘了。
        pet._suggest_ignored().clear()
        self.assertIn("today", self.feed_keys(pet))

    def test_stop_this_kind_for_good_is_remembered(self):
        pet, memory, _db = self.build_pet()
        pet.dismiss_suggestion("feed", "today", forever=True)
        self.assertIn("feed:today", memory.get_preference(pet.SUGGEST_MUTE_PREFERENCE))
        pet._suggest_ignored().clear()
        self.assertNotIn("today", self.feed_keys(pet))
        # 想反悔随时能撤销。
        pet.unmute_suggestions()
        self.assertIn("today", self.feed_keys(pet))
        self.assertEqual(memory.get_preference(pet.SUGGEST_MUTE_PREFERENCE), "")


class IdeasPageTests(TkCase):
    """灵感建议页：有依据才提，每条都说清依据。"""

    def test_the_page_opens_in_the_workbench(self):
        pet, _memory, _db = self.build_pet()
        self.assertEqual(pet.open_ideas(), [])
        self.assertTrue(pet.chat.page_is_open())
        self.assertEqual(pet.chat.page_key, "ideas")
        self.assertIsNotNone(pet._panels.get("ideas"))

    def test_no_basis_means_an_empty_page(self):
        pet, _memory, _db = self.build_pet()
        pet.open_ideas()
        self.assertEqual(pet._panels["ideas"].text(), ["现在没有可以提的建议"])

    def test_every_idea_says_why_and_what_it_needs(self):
        pet, _memory, _db = self.build_pet()
        self.learn_habit(pet)
        rows = pet.open_ideas()
        self.assertTrue(rows)
        for card in rows:
            self.assertTrue(card.get("why"), card)
            self.assertTrue(card.get("context"), card)
            self.assertTrue(card.get("requires"), card)
        text = "\n".join(pet._panels["ideas"].lines())
        self.assertIn("为什么提", text)
        self.assertIn("用到什么", text)
        self.assertIn("需要什么", text)

    def test_an_idea_about_habits_opens_the_persona_page(self):
        pet, _memory, _db = self.build_pet()
        self.learn_habit(pet)
        pet.open_ideas()
        page = pet._panels["ideas"]
        card = [item for item in page.cards if item["key"] == "habits"][0]
        self.assertEqual(card["action"][1], "panel")
        page.run_action(card)
        self.assertEqual(pet.chat.page_key, "persona")

    def test_ignoring_an_idea_takes_it_off_the_page(self):
        pet, _memory, database = self.build_pet()
        self.seed_task(pet.agent.task_store, "整理资料", TaskState.RUNNING.value)
        pet.open_ideas()
        page = pet._panels["ideas"]
        self.assertTrue(page.cards)
        page.ignore(page.cards[0])
        self.assertFalse([item for item in page.cards if item["key"] == "resume"])
        # 只是这次不提，任务记录还在。
        self.assertEqual(len(pet.task_rows()), 1)
        self.assertEqual(pet.muted_suggestions(), [])

    def test_stop_this_kind_for_good_leaves_a_trace(self):
        pet, _memory, _db = self.build_pet()
        self.seed_task(pet.agent.task_store, "整理资料", TaskState.RUNNING.value)
        pet.open_ideas()
        page = pet._panels["ideas"]
        page.ignore(page.cards[0], forever=True)
        self.assertIn("idea:resume", pet.muted_suggestions())
        self.assertEqual(page.cards, [])
        page.unmute_all()
        self.assertEqual(pet.muted_suggestions(), [])
        self.assertTrue(page.cards)


class GoalsPageTests(TkCase):
    """目标与追踪页：按真实步骤摆里程碑。"""

    def test_the_page_opens_in_the_workbench(self):
        pet, _memory, _db = self.build_pet()
        self.assertEqual(pet.open_goals(), [])
        self.assertTrue(pet.chat.page_is_open())
        self.assertEqual(pet.chat.page_key, "goals")

    def test_no_goals_means_an_empty_page(self):
        pet, _memory, _db = self.build_pet()
        pet.open_goals()
        self.assertEqual(pet._panels["goals"].text(), ["还没有在追的目标"])

    def test_milestones_come_from_the_real_steps(self):
        pet, _memory, _db = self.build_pet()
        self.seed_task(
            pet.agent.task_store, "整理论文资料", TaskState.RUNNING.value,
            steps=[
                {"index": 0, "title": "下载", "status": "done"},
                {"index": 1, "title": "分类", "status": "running"},
                {"index": 2, "title": "写摘要", "status": "pending"},
            ],
        )
        rows = pet.open_goals()
        self.assertEqual(len(rows), 1)
        text = "\n".join(pet._panels["goals"].text())
        self.assertIn("整理论文资料", text)
        self.assertIn("执行中", text)
        self.assertIn("已完成 1 / 共 3 步", text)
        self.assertIn("\u2714 下载", text)
        self.assertIn("\u25cf 分类", text)
        self.assertIn("\u25cb 写摘要", text)

    def test_a_goal_without_steps_says_so(self):
        pet, _memory, _db = self.build_pet()
        self.seed_task(pet.agent.task_store, "一件事", TaskState.RUNNING.value)
        pet.open_goals()
        text = "\n".join(pet._panels["goals"].text())
        self.assertIn("还没有列步骤", text)

    def test_done_work_is_not_a_goal(self):
        pet, _memory, _db = self.build_pet()
        self.seed_task(pet.agent.task_store, "已经做完的事", TaskState.COMPLETED.value)
        self.assertEqual(pet.open_goals(), [])

    def test_continuing_without_a_task_model_does_not_fake_it(self):
        pet, _memory, _db = self.build_pet()
        self.seed_task(pet.agent.task_store, "整理论文资料", TaskState.RUNNING.value)
        pet.open_goals()
        page = pet._panels["goals"]
        goal = page.goals[0]
        self.assertTrue(goal["needs_model"])
        self.assertFalse(page.continue_goal(goal))
        self.assertIn("任务模型", page.hint.cget("text"))
        self.assertEqual(pet.chat.read_input().strip(), "")

    def test_continuing_with_a_task_model_fills_the_input(self):
        pet, _memory, _db = self.build_pet()
        self.seed_task(pet.agent.task_store, "整理论文资料", TaskState.RUNNING.value)
        # 任务模型配好了：这里只换掉「有没有配好」这一个判断。
        pet._task_model_ready = lambda: True
        pet.open_goals()
        page = pet._panels["goals"]
        goal = page.goals[0]
        self.assertFalse(goal["needs_model"])
        text = page.continue_goal(goal)
        self.assertTrue(text)
        self.assertIn("整理论文资料", text)
        self.assertIn("接着做", pet.chat.read_input())


class ContinuityTests(TkCase):
    """连续性上下文：上次停在哪，只报事实。"""

    def test_an_unfinished_task_becomes_a_resume_card(self):
        pet, _memory, _db = self.build_pet()
        self.seed_task(
            pet.agent.task_store, "整理论文资料", TaskState.WAITING_USER.value,
            steps=[
                {"index": 0, "title": "下载", "status": "done"},
                {"index": 1, "title": "分类", "status": "pending"},
            ],
        )
        capsule = pet.continuity_capsule()
        self.assertEqual(capsule["goal"], "整理论文资料")
        self.assertEqual(capsule["done"], ["下载"])
        self.assertEqual(capsule["next_step"], "分类")
        self.assertIn("resume", self.feed_keys(pet))
        self.assertIn("分类", pet.capsule_text(capsule))

    def test_finished_work_is_not_a_resume_card(self):
        pet, _memory, _db = self.build_pet()
        self.seed_task(pet.agent.task_store, "已经做完的事", TaskState.COMPLETED.value)
        self.assertIsNone(pet.continuity_capsule())
        self.assertNotIn("resume", self.feed_keys(pet))

    def test_no_task_at_all_says_nothing(self):
        pet, _memory, _db = self.build_pet()
        self.assertIsNone(pet.continuity_capsule())
        self.assertEqual(pet.capsule_text(None), "")


if __name__ == "__main__":
    unittest.main()