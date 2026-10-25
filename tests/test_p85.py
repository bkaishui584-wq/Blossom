"""P5 验收：发布前的说明与找回入口。

覆盖：第一次说明看过就不再弹、设置页能再看一遍、重置之后确实会再弹、
用过一段时间的库不再打扰、「以后不提」的建议能在设置页一次恢复、
恢复后首页重新出现这些建议、分发文档写清数据目录、卸载、隐私和第三方许可。
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
from ui import wizard
from ui.desktop import DesktopPet


class SilentProvider(ModelProvider):
    """一个没配好的模型：不该被调用。"""

    name = "silent"

    def is_available(self):
        return False

    def chat(self, messages, tools=None, temperature=0.7):
        return ChatResult(ok=False, error="no model", usage=Usage(), model="silent")

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
        agent.learning = HabitLearner(preferences=memory, persona=agent.persona)
        agent.learning.apply()
        pet = DesktopPet(agent, self.config, memory=memory, root=self.root)
        self.addCleanup(pet.close)
        pet.show_chat()
        return pet, memory, database

    def keep(self, window):
        """窗口留到测试结束再关，免得 Tk 中途抱怨。"""
        if window is not None:
            self.addCleanup(self._close_window, window)
        return window

    def _close_window(self, window):
        try:
            window.win.destroy()
        except Exception:
            pass

    def hint_text(self, pet):
        widget = getattr(pet, "settings_hint", None)
        if widget is None:
            return ""
        try:
            return str(widget.cget("text") or "")
        except Exception:
            return ""


class WizardReopenTests(TkCase):
    """第一次说明的状态机：看过、重置、再看一遍。"""

    def test_a_fresh_install_still_owes_the_explanation(self):
        pet, _memory, _db = self.build_pet()
        self.assertFalse(wizard.is_done(pet))

    def test_finishing_marks_it_done(self):
        pet, _memory, _db = self.build_pet()
        wizard.mark_done(pet)
        self.assertTrue(wizard.is_done(pet))

    def test_resetting_makes_it_show_again(self):
        pet, _memory, _db = self.build_pet()
        wizard.mark_done(pet)
        self.assertTrue(wizard.reset(pet))
        self.assertFalse(wizard.is_done(pet))

    def test_reopening_does_not_count_as_finished_by_itself(self):
        pet, _memory, _db = self.build_pet()
        window = self.keep(pet.open_wizard())
        self.assertIsNotNone(window)
        self.assertFalse(wizard.is_done(pet))

    def test_forcing_the_reopen_records_it(self):
        pet, _memory, _db = self.build_pet()
        self.keep(pet.open_wizard(force=True))
        self.assertTrue(wizard.is_done(pet))

    def test_a_used_database_never_sees_the_wizard(self):
        pet, memory, _db = self.build_pet()
        memory.record("desktop", "user", "在吗")
        wizard.reset(pet)
        self.assertTrue(wizard.looks_used(pet))
        self.assertIsNone(pet._maybe_first_run())
        self.assertTrue(wizard.is_done(pet))


class SettingsHelpTests(TkCase):
    """设置页那两个找回入口真的能用。"""

    def settings(self, pet):
        view = pet.open_settings()
        self.assertIsNotNone(view)
        return view

    def test_the_settings_page_can_reopen_the_explanation(self):
        pet, _memory, _db = self.build_pet()
        view = self.settings(pet)
        window = self.keep(view._reopen_wizard())
        self.assertIsNotNone(window)
        self.assertTrue(wizard.is_done(pet))
        self.assertIn("已经打开", self.hint_text(pet))

    def test_the_settings_page_has_nothing_to_restore_by_default(self):
        pet, _memory, _db = self.build_pet()
        view = self.settings(pet)
        self.assertEqual(view._unmute_suggestions(), 0)
        self.assertIn("本来就没有", self.hint_text(pet))

    def test_a_muted_card_comes_back_after_restoring(self):
        pet, _memory, database = self.build_pet()
        TaskStore(database).save(TaskRecord(
            id=new_task_id(), goal="写周报", state=TaskState.RUNNING.value,
            session="desktop",
        ))
        self.assertIn("today", [card.get("key") for card in pet.home_feed()])

        pet.dismiss_suggestion("feed", "today", forever=True)
        self.assertIn("feed:today", pet.muted_suggestions())
        self.assertNotIn("today", [card.get("key") for card in pet.home_feed()])

        view = self.settings(pet)
        self.assertEqual(view._unmute_suggestions(), 1)
        self.assertEqual(pet.muted_suggestions(), [])
        self.assertIn("today", [card.get("key") for card in pet.home_feed()])

    def test_restoring_also_clears_this_rounds_ignore(self):
        pet, _memory, _db = self.build_pet()
        pet.dismiss_suggestion("feed", "today")
        self.assertTrue(pet.suggestion_muted("feed", "today"))
        view = self.settings(pet)
        view._unmute_suggestions()
        self.assertFalse(pet.suggestion_muted("feed", "today"))

    def test_muted_list_survives_a_reopen(self):
        pet, _memory, _db = self.build_pet()
        pet.dismiss_suggestion("idea", "habits", forever=True)
        pet.forget_panels()
        self.assertIn("idea:habits", pet.muted_suggestions())


class DistributionDocTests(unittest.TestCase):
    """分发文档得把该说的说清楚。"""

    def doc(self, name):
        path = ROOT / "docs" / name
        self.assertTrue(path.exists(), "缺文件：%s" % path)
        return path.read_text(encoding="utf-8")

    def test_the_distribution_doc_covers_the_basics(self):
        text = self.doc("distribution.md")
        for word in ("数据目录", "卸载", "隐私", "第三方", "许可"):
            self.assertIn(word, text, word)

    def test_the_acceptance_doc_covers_the_latest_rounds(self):
        text = self.doc("acceptance.md")
        for word in ("贴边", "屏幕感知", "灵感"):
            self.assertIn(word, text, word)


if __name__ == "__main__":
    unittest.main()