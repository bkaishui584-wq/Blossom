"""阶段 P75 验收：V7 阶段 4 的记忆恢复、清空与人格回滚入口。

要点：
    恢复整库要先验文件、先另存原库、再问一次；
    文件不是备份就拒绝，取消就不动数据；
    清空要问过用户才动，且不动用户自己设的偏好；
    人格版本回滚把原文写回生效位置，界面上的入口都在。
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
from database.db import Database
from memory import backup as memory_backup
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatDelta, ChatResult, ModelProvider, Usage
from security.config import Config
from ui import memory_view
from ui.desktop import DesktopPet
from ui.memory_view import MemoryWindow


class StubProvider(ModelProvider):
    name = "stub"

    def is_available(self):
        return True

    def chat(self, messages, tools=None, temperature=0.7):
        return ChatResult(ok=True, content="在的。", usage=Usage(5, 3), model="stub")

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


class TkTestCase(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        self.root.withdraw()
        self.addCleanup(self._safe_destroy)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.db_path = base / "t.sqlite3"
        self.config = Config(load_env=False)
        self.config.data_dir = base
        self.config.log_dir = base / "logs"
        self.config.db_path = self.db_path
        self.config.characters_dir = base / "characters"
        self.config.live2d_dir = base / "live2d"
        self.config.devices_dir = base / "devices"

    def _safe_destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def build_pet(self):
        database = Database(self.db_path)
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        agent = AgentCore(StubProvider(), memory, ContextManager(token_budget=800))
        pet = DesktopPet(agent, self.config, memory=memory, root=self.root)
        return pet, memory, database


class MemoryBackupEntryTests(TkTestCase):
    def test_the_window_offers_restore_and_clear(self):
        pet, _memory, _db = self.build_pet()
        window = pet.open_memory()
        self.addCleanup(window.win.destroy)
        self.assertTrue(callable(window.restore_from_file))
        self.assertTrue(callable(window.clear_all_memory))

    def test_a_file_that_is_not_a_backup_is_refused(self):
        pet, memory, _db = self.build_pet()
        memory.remember("用户喜欢手冲咖啡", kind="long_term")
        junk = Path(self.tmp.name) / "notes.txt"
        junk.write_text("这不是数据库", encoding="utf-8")
        window = pet.open_memory()
        self.addCleanup(window.win.destroy)
        with mock.patch.object(
            memory_view.filedialog, "askopenfilename", return_value=str(junk)
        ):
            self.assertFalse(window.restore_from_file())
        self.assertIn("不能用", window.hint.cget("text"))
        self.assertEqual(len(memory.browse()), 1)

    def test_picking_nothing_changes_nothing(self):
        pet, memory, _db = self.build_pet()
        memory.remember("用户喜欢手冲咖啡", kind="long_term")
        window = pet.open_memory()
        self.addCleanup(window.win.destroy)
        with mock.patch.object(
            memory_view.filedialog, "askopenfilename", return_value=""
        ):
            self.assertFalse(window.restore_from_file())
        self.assertEqual(len(memory.browse()), 1)

    def test_cancelling_the_confirm_changes_nothing(self):
        pet, memory, _db = self.build_pet()
        memory.remember("用户喜欢手冲咖啡", kind="long_term")
        backup = memory_backup.backup_database(self.db_path, dest_dir=Path(self.tmp.name))
        self.assertTrue(backup["ok"], backup)
        window = pet.open_memory()
        self.addCleanup(window.win.destroy)
        with mock.patch.object(
            memory_view.filedialog, "askopenfilename", return_value=str(backup["path"])
        ), mock.patch.object(memory_view.messagebox, "askyesno", return_value=False):
            self.assertFalse(window.restore_from_file())
        self.assertEqual(len(memory.browse()), 1)

    def test_a_real_backup_comes_back_and_the_old_one_is_kept(self):
        pet, memory, _db = self.build_pet()
        memory.remember("用户喜欢手冲咖啡", kind="long_term")
        backup = memory_backup.backup_database(self.db_path, dest_dir=Path(self.tmp.name))
        self.assertTrue(backup["ok"], backup)
        memory.remember("这句话是备份之后才有的", kind="long_term")
        self.assertEqual(len(memory.browse()), 2)
        window = pet.open_memory()
        self.addCleanup(window.win.destroy)
        with mock.patch.object(
            memory_view.filedialog, "askopenfilename", return_value=str(backup["path"])
        ), mock.patch.object(memory_view.messagebox, "askyesno", return_value=True):
            self.assertTrue(window.restore_from_file())
        kept = list(Path(self.tmp.name).glob("t-before-restore-*.sqlite3"))
        self.assertEqual(len(kept), 1)
        fresh = Database(self.db_path)
        self.assertEqual(fresh.query_one("SELECT COUNT(*) AS n FROM memories")["n"], 1)
        self.assertIn("重启桌宠", window.hint.cget("text"))

    def test_clear_asks_first(self):
        pet, memory, _db = self.build_pet()
        memory.remember("用户喜欢手冲咖啡", kind="long_term")
        window = pet.open_memory()
        self.addCleanup(window.win.destroy)
        with mock.patch.object(memory_view.messagebox, "askyesno", return_value=False):
            self.assertFalse(window.clear_all_memory())
        self.assertEqual(len(memory.browse()), 1)

    def test_clear_removes_the_memories(self):
        pet, memory, _db = self.build_pet()
        memory.remember("用户喜欢手冲咖啡", kind="long_term")
        memory.remember("在做一个桌宠项目", kind="project")
        window = pet.open_memory()
        self.addCleanup(window.win.destroy)
        with mock.patch.object(memory_view.messagebox, "askyesno", return_value=True):
            self.assertTrue(window.clear_all_memory())
        self.assertEqual(memory.browse(), [])
        self.assertIn("已清空", window.hint.cget("text"))

    def test_clear_keeps_the_preferences_the_user_set(self):
        pet, memory, _db = self.build_pet()
        memory.remember("用户喜欢手冲咖啡", kind="long_term")
        memory.set_preference("persona.habit.address", "阿云")
        window = pet.open_memory()
        self.addCleanup(window.win.destroy)
        with mock.patch.object(memory_view.messagebox, "askyesno", return_value=True):
            window.clear_all_memory()
        self.assertEqual(memory.get_preference("persona.habit.address"), "阿云")

    def test_clear_without_a_memory_object_says_so(self):
        pet, _memory, _db = self.build_pet()
        window = pet.open_memory()
        self.addCleanup(window.win.destroy)
        window.memory = None
        self.assertFalse(window.clear_all_memory())

    def test_restore_without_a_database_path_says_so(self):
        pet, _memory, _db = self.build_pet()
        window = pet.open_memory()
        self.addCleanup(window.win.destroy)
        pet.config.db_path = ""
        self.assertFalse(window.restore_from_file())


class PersonaRollbackEntryTests(TkTestCase):
    def test_the_versions_tab_can_revert(self):
        pet, _memory, _db = self.build_pet()
        window = pet.open_persona()
        self.addCleanup(window.win.destroy)
        self.assertTrue(callable(window.revert_version))
        self.assertTrue(callable(window.compare_version))

    def test_reverting_puts_the_old_text_back_in_charge(self):
        pet, _memory, _db = self.build_pet()
        pet.save_persona_text("第一版：说话简短。")
        pet.save_persona_text("第二版：说话详细。")
        window = pet.open_persona()
        self.addCleanup(window.win.destroy)
        versions = window.refresh_versions()
        self.assertGreaterEqual(len(versions), 2)
        oldest = str(versions[-1].get("v"))
        window.version_tree.selection_set(oldest)
        with mock.patch("ui.persona_workbench.messagebox.askyesno", return_value=True):
            self.assertTrue(window.revert_version())
        self.assertIn("第一版", pet.persona_text())


if __name__ == "__main__":
    unittest.main()