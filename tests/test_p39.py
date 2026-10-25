"""阶段 B 验收：持久记忆 V2。

覆盖：向前兼容的迁移、元数据、去重、取代、来源可信度、容量策略、
本地摘要、内部偏好不外泄、备份校验、管理界面，以及退出重启后数据还在。
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
from database.db import MIGRATIONS, Database
from memory import backup as memory_backup
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatDelta, ChatResult, ModelProvider, Usage
from persona.interface import DefaultPersonaProvider
from security.config import Config
from ui import memory_view
from ui.desktop import DesktopPet
from ui.memory_view import KIND_LABELS, MemoryWindow, describe_row, source_text


def make_memory(tmp, **kwargs):
    database = Database(Path(tmp) / "t.sqlite3")
    database.migrate()
    return MemoryManager(MemoryRepository(database), **kwargs), database


def make_v8_database(path):
    """照老版本建库：只到第 8 号迁移，用来验证升级不丢数据。"""
    database = Database(path)
    with database.connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL,"
            " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        for version, name, sql in sorted(MIGRATIONS):
            if version > 8:
                continue
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                (version, name),
            )
    return database


def columns_of(database, table):
    rows = database.query("PRAGMA table_info(" + table + ")")
    return {row["name"] for row in rows}


class MigrationTests(unittest.TestCase):
    def test_old_database_upgrades_without_losing_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.sqlite3"
            old = make_v8_database(path)
            old.insert(
                "INSERT INTO memories (kind, content, keywords, importance)"
                " VALUES ('long_term', '老记忆', '老记忆', 1)"
            )
            old.insert(
                "INSERT INTO messages (session_id, role, content) VALUES ('s', 'user', '老消息')"
            )

            applied = old.migrate()
            self.assertIn(9, applied)

            kept = old.query_one("SELECT * FROM memories")
            self.assertEqual(kept["content"], "老记忆")
            # 老行自动补上默认元数据：已确认、没锁、没归档。
            self.assertEqual(int(kept["confirmed"]), 1)
            self.assertEqual(int(kept["locked"]), 0)
            self.assertEqual(int(kept["archived"]), 0)
            self.assertEqual(
                old.query_one("SELECT content FROM messages")["content"], "老消息"
            )

    def test_new_columns_and_history_table_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            _memory, database = make_memory(tmp)
            columns = columns_of(database, "memories")
            self.assertTrue({"confirmed", "locked", "archived"} <= columns)
            tables = set(database.health()["tables"])
            self.assertIn("preference_history", tables)

    def test_migrate_is_repeatable(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / "t.sqlite3")
            database.migrate()
            self.assertEqual(database.migrate(), [])


class DedupTests(unittest.TestCase):
    def test_identical_memory_is_not_stored_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, database = make_memory(tmp)
            first = memory.remember("用户喜欢喝美式咖啡")
            again = memory.remember("用户喜欢喝美式咖啡")
            self.assertEqual(first, again)
            row = database.query_one("SELECT COUNT(*) AS n FROM memories")
            self.assertEqual(int(row["n"]), 1)
            touched = database.query_one("SELECT last_used_at FROM memories WHERE id = ?", (first,))
            self.assertTrue(touched["last_used_at"])

    def test_different_wording_is_a_new_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, _ = make_memory(tmp)
            memory.remember("用户喜欢喝美式咖啡")
            memory.remember("用户住在杭州")
            self.assertEqual(memory.stats()["total"], 2)

    def test_blank_memory_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, _ = make_memory(tmp)
            self.assertIsNone(memory.remember("   "))
            self.assertEqual(memory.stats()["total"], 0)


class PreferenceTests(unittest.TestCase):
    def test_new_preference_replaces_old_and_keeps_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, _ = make_memory(tmp)
            memory.set_preference("称呼", "主人")
            memory.set_preference("称呼", "老板")
            self.assertEqual(memory.get_preference("称呼"), "老板")
            history = memory.repository.preference_history("称呼")
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["old_value"], "主人")
            self.assertEqual(history[0]["new_value"], "老板")

    def test_same_value_writes_no_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, _ = make_memory(tmp)
            memory.set_preference("称呼", "主人")
            memory.set_preference("称呼", "主人")
            self.assertEqual(memory.repository.preference_history("称呼"), [])

    def test_internal_preferences_never_reach_the_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, _ = make_memory(tmp)
            memory.set_preference("pet_position", "120,240")
            memory.set_preference("privacy.listen", "1")
            memory.set_preference("habit.inferred", '{"samples": 0}')
            memory.set_preference("proactive_enabled", "1")
            memory.set_preference("quiet_mode", "1")
            memory.set_preference("quiet_hours", "23-7")
            memory.set_preference("persona.name", "小桌")
            memory.set_preference("称呼", "主人")

            _, block, _ = memory.recall("s", "叫法")
            self.assertIn("用户偏好 称呼: 主人", block)
            for leaked in ("pet_position", "privacy.", "habit.", "proactive_enabled",
                           "quiet_", "persona."):
                self.assertNotIn(leaked, block)


class SourceTrustTests(unittest.TestCase):
    def test_user_words_are_confirmed_and_inference_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, _ = make_memory(tmp)
            user = memory.remember("用户喜欢喝美式咖啡", source="USER_EXPLICIT")
            guess = memory.remember("用户可能喜欢拿铁", source="MODEL_INFERRED")
            rows = {row["id"]: row for row in memory.recent_memories(limit=10)}
            self.assertEqual(rows[user]["confirmed"], 1)
            self.assertEqual(rows[guess]["confirmed"], 0)

    def test_inference_is_labelled_in_the_recall_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, _ = make_memory(tmp)
            memory.remember("用户可能喜欢拿铁", source="MODEL_INFERRED")
            _, block, _ = memory.recall("s", "拿铁")
            self.assertIn("（推测）", block)

    def test_observed_memory_is_labelled_apart_from_user_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, _ = make_memory(tmp)
            memory.remember("用户常在晚上出现", source="SYSTEM_OBSERVED")
            _, block, _ = memory.recall("s", "晚上")
            self.assertIn("（观察到）", block)

    def test_confirmed_fact_has_no_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, _ = make_memory(tmp)
            memory.remember("用户喜欢喝美式咖啡", source="USER_EXPLICIT")
            _, block, _ = memory.recall("s", "咖啡")
            self.assertIn("- 用户喜欢喝美式咖啡", block)
            self.assertNotIn("（推测）", block)


class CapacityTests(unittest.TestCase):
    def test_overflow_is_archived_not_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, database = make_memory(tmp, memory_capacity=3)
            for text in (
                "用户喜欢喝美式咖啡",
                "用户住在杭州",
                "项目截止日期是三月十号",
                "用户养了一只猫",
                "用户每天早上跑步",
            ):
                memory.remember(text, kind="long_term")
            stats = memory.stats()
            self.assertEqual(stats["total"], 3)
            self.assertEqual(stats["archived"], 2)
            # 一条都没删，只是收起来了。
            row = database.query_one("SELECT COUNT(*) AS n FROM memories")
            self.assertEqual(int(row["n"]), 5)

    def test_archived_memories_stay_browsable(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, _ = make_memory(tmp, memory_capacity=1)
            memory.remember("第一条", kind="long_term")
            memory.remember("第二条", kind="long_term")
            self.assertEqual(len(memory.browse()), 1)
            self.assertEqual(len(memory.browse(include_archived=True)), 2)

    def test_locked_memory_is_never_archived(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, database = make_memory(tmp, memory_capacity=2)
            first = memory.remember("这条很重要", kind="long_term", importance=1)
            memory.set_locked(first, True)
            memory.remember("随便一条", kind="long_term")
            memory.remember("再来一条", kind="long_term")
            kept = memory.repository.get_memory(first)
            self.assertEqual(int(kept["archived"]), 0)
            self.assertEqual(int(kept["locked"]), 1)
            self.assertEqual(memory.stats()["archived"], 1)

    def test_capacity_defaults_are_not_tiny(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, _ = make_memory(tmp)
            self.assertGreaterEqual(memory.memory_capacity, 1000)
            self.assertGreaterEqual(memory.memory_limit, 10)


class SummaryTests(unittest.TestCase):
    def test_local_summary_extracts_todos_without_a_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, _ = make_memory(
                tmp, recent_window=2, summary_trigger=4, summary_max_chars=800
            )
            memory.record("s", "user", "帮我记住明天下午三点开会")
            for i in range(8):
                memory.record("s", "user", f"普通消息{i}")
            summary = memory.repository.latest_summary("s")
            self.assertIsNotNone(summary)
            self.assertIn("【待办与未解决】", summary["content"])
            self.assertIn("明天下午三点开会", summary["content"])
            self.assertLessEqual(len(summary["content"]), memory.summary_max_chars)

    def test_summary_stays_within_its_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, _ = make_memory(
                tmp, recent_window=2, summary_trigger=4, summary_max_chars=120
            )
            for i in range(20):
                memory.record("s", "user", f"很长的一句话内容{i}" + "补充" * 20)
            summary = memory.repository.latest_summary("s")
            self.assertLessEqual(len(summary["content"]), 120)

    def test_history_and_summary_are_kept_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, _ = make_memory(tmp, recent_window=2, summary_trigger=4)
            for i in range(8):
                memory.record("s", "user", f"消息{i}")
            # 长期历史可回看：消息一条没删。
            self.assertEqual(len(memory.repository.recent_messages("s", 50)), 8)
            self.assertIsNotNone(memory.repository.latest_summary("s"))


class ManagementTests(unittest.TestCase):
    def test_edit_recomputes_keywords(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, _ = make_memory(tmp)
            number = memory.remember("用户喜欢喝美式咖啡")
            self.assertTrue(memory.edit(number, content="用户喜欢喝拿铁", importance=3))
            row = memory.repository.get_memory(number)
            self.assertEqual(row["content"], "用户喜欢喝拿铁")
            self.assertEqual(int(row["importance"]), 3)
            self.assertIn("拿铁", row["keywords"])

    def test_edit_rejects_blank_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, _ = make_memory(tmp)
            number = memory.remember("用户喜欢喝美式咖啡")
            self.assertFalse(memory.edit(number, content="   "))
            self.assertEqual(
                memory.repository.get_memory(number)["content"], "用户喜欢喝美式咖啡"
            )

    def test_user_can_confirm_an_inference(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, _ = make_memory(tmp)
            number = memory.remember("用户可能喜欢拿铁", source="MODEL_INFERRED")
            self.assertTrue(memory.confirm_memory(number, True))
            self.assertEqual(memory.repository.get_memory(number)["confirmed"], 1)

    def test_delete_needs_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, _ = make_memory(tmp)
            number = memory.remember("用户喜欢喝美式咖啡")
            self.assertFalse(memory.forget(number))
            self.assertIsNotNone(memory.repository.get_memory(number))
            self.assertTrue(memory.forget(number, confirm=True))
            self.assertIsNone(memory.repository.get_memory(number))

    def test_stats_break_down_by_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, _ = make_memory(tmp)
            memory.remember("长期一条", kind="long_term")
            memory.remember("项目一条", kind="project")
            memory.remember("关系一条", kind="relationship")
            stats = memory.stats()
            self.assertEqual(stats["total"], 3)
            self.assertEqual(stats["kinds"]["long_term"], 1)
            self.assertEqual(stats["kinds"]["project"], 1)


class BackupValidationTests(unittest.TestCase):
    def test_broken_backup_is_refused_before_touching_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, database = make_memory(tmp)
            memory.remember("不能被覆盖的记忆", kind="long_term")
            broken = Path(tmp) / "broken.sqlite3"
            broken.write_text("这不是数据库", encoding="utf-8")
            result = memory_backup.restore_database(database.path, broken)
            self.assertFalse(result["ok"])
            self.assertEqual(result["safety"], "")
            self.assertEqual(memory.stats()["total"], 1)

    def test_declining_confirmation_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, database = make_memory(tmp)
            memory.remember("现在的记忆", kind="long_term")
            path = memory_backup.backup_database(database.path)["path"]
            result = memory_backup.restore_database(database.path, path, confirm=False)
            self.assertFalse(result["ok"])
            self.assertEqual(memory.stats()["total"], 1)

    def test_validate_accepts_a_real_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, database = make_memory(tmp)
            memory.remember("记忆", kind="long_term")
            path = memory_backup.backup_database(database.path)["path"]
            check = memory_backup.validate_backup(path)
            self.assertTrue(check["ok"])
            self.assertIn("memories", check["tables"])

    def test_export_carries_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, _ = make_memory(tmp)
            memory.remember("用户喜欢喝美式咖啡", source="USER_EXPLICIT", importance=2)
            payload = memory_backup.export_payload(memory)
            entry = payload["memories"][0]
            self.assertEqual(entry["content"], "用户喜欢喝美式咖啡")
            self.assertIn("confirmed", entry)
            self.assertIn("created_at", entry)

    def test_import_preserves_unconfirmed_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, _ = make_memory(tmp)
            source.remember("用户可能喜欢拿铁", source="MODEL_INFERRED")
            payload = memory_backup.export_payload(source)

            other_dir = Path(tmp) / "other"
            other, _ = make_memory(other_dir)
            result = memory_backup.import_payload(other, payload)
            self.assertTrue(result["ok"])
            rows = other.recent_memories(limit=10)
            self.assertEqual(rows[0]["source"], "MODEL_INFERRED")
            self.assertEqual(rows[0]["confirmed"], 0)


class RestartTests(unittest.TestCase):
    def test_everything_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.sqlite3"
            database = Database(path)
            database.migrate()
            memory = MemoryManager(
                MemoryRepository(database), recent_window=2, summary_trigger=4
            )
            memory.set_preference("称呼", "主人")
            memory.set_preference("pet_position", "300,200")
            memory.remember("用户喜欢喝美式咖啡", kind="long_term", importance=2)
            for i in range(6):
                memory.record("s", "user", f"消息{i}")
            memory.relationship.touch()

            reopened = Database(path)
            reopened.migrate()
            again = MemoryManager(MemoryRepository(reopened))

            self.assertEqual(again.get_preference("称呼"), "主人")
            self.assertEqual(again.get_preference("pet_position"), "300,200")
            self.assertEqual(
                [row["content"] for row in again.search("咖啡")], ["用户喜欢喝美式咖啡"]
            )
            self.assertLess(again.repository.count_unsummarized("s"), 6)
            self.assertIsNotNone(again.repository.latest_summary("s"))
            self.assertEqual(len(again.repository.recent_messages("s", 50)), 6)
            self.assertGreaterEqual(again.relationship.interactions(), 1)


class ContextBudgetTests(unittest.TestCase):
    def test_memory_block_is_capped_by_the_budget(self):
        context = ContextManager(token_budget=200)
        block = "- " + ("很长的记忆内容" * 100)
        bundle = context.build(
            system_prompt="sys", recent_messages=[], memory_block=block
        )
        recalled = bundle.messages[1]["content"]
        self.assertLess(len(recalled), len(block))
        self.assertIn("已截断", recalled)
        self.assertEqual(bundle.stats["memory_chars"], len(recalled))

    def test_recent_window_default_is_generous(self):
        context = ContextManager(token_budget=4000)
        self.assertGreaterEqual(context.recent_window, 20)

    def test_old_messages_are_trimmed_by_tokens(self):
        context = ContextManager(token_budget=80, recent_window=30)
        recent = [{"role": "user", "content": "x" * 100} for _ in range(10)]
        bundle = context.build(
            system_prompt="s", recent_messages=recent, current_input="现在的问题"
        )
        self.assertLess(bundle.stats["recent_sent"], 10)
        self.assertEqual(bundle.messages[-1]["content"], "现在的问题")


class HelperTests(unittest.TestCase):
    def test_source_labels_are_readable(self):
        self.assertEqual(source_text({"source": "USER_EXPLICIT", "confirmed": 1}), "用户亲口说")
        self.assertEqual(
            source_text({"source": "MODEL_INFERRED", "confirmed": 0}),
            "模型推测（未确认）",
        )

    def test_source_detail_shows_provenance(self):
        text = describe_row(
            {
                "kind": "long_term",
                "source": "USER_EXPLICIT",
                "confirmed": 1,
                "importance": 2,
                "confidence": 1.0,
                "locked": 0,
                "created_at": "2026-01-01 10:00:00",
                "last_used_at": None,
            }
        )
        self.assertIn("长期事实", text)
        self.assertIn("用户亲口说", text)
        self.assertIn("还没用过", text)

    def test_kind_labels_cover_every_layer(self):
        for kind in ("long_term", "project", "relationship", "feedback"):
            self.assertIn(kind, KIND_LABELS)


class StubProvider(ModelProvider):
    name = "stub"

    def __init__(self, reply="在的。"):
        self.reply = reply

    def is_available(self):
        return True

    def chat(self, messages, tools=None, temperature=0.7):
        return ChatResult(
            ok=True, content=self.reply, usage=Usage(prompt_tokens=5, completion_tokens=3),
            model="stub",
        )

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


class TkTestCase(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except Exception as exc:
            self.skipTest(f"tkinter 不可用：{exc}")
        self.root.withdraw()
        self.addCleanup(self._safe_destroy)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _safe_destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def build_pet(self, **kwargs):
        database = Database(Path(self.tmp.name) / "t.sqlite3")
        database.migrate()
        memory = MemoryManager(MemoryRepository(database), **kwargs)
        agent = AgentCore(
            StubProvider(),
            memory,
            ContextManager(token_budget=800),
            persona=DefaultPersonaProvider(),
        )
        pet = DesktopPet(agent, Config(load_env=False), memory=memory, root=self.root)
        return pet, memory


class MemoryWindowTests(TkTestCase):
    def test_menu_entry_is_wired(self):
        pet, _ = self.build_pet()
        self.assertIn(("记忆", "open_memory"), DesktopPet.MENU_ITEMS)
        self.assertTrue(callable(pet.open_memory))

    def test_window_lists_and_searches(self):
        pet, memory = self.build_pet()
        memory.remember("用户喜欢喝美式咖啡", kind="long_term")
        window = pet.open_memory()
        self.addCleanup(window.win.destroy)
        self.assertIsInstance(window, MemoryWindow)
        self.assertEqual(len(window.tree.get_children()), 1)
        window.search_entry.insert(0, "拿铁")
        window.refresh()
        self.assertEqual(len(window.tree.get_children()), 0)
        window.search_entry.delete(0, tk.END)
        window.refresh()
        self.assertEqual(len(window.tree.get_children()), 1)

    def test_window_is_not_stacked_twice(self):
        pet, _ = self.build_pet()
        first = pet.open_memory()
        self.addCleanup(first.win.destroy)
        self.assertIs(first, pet.open_memory())

    def test_delete_from_the_interface_needs_confirmation(self):
        pet, memory = self.build_pet()
        number = memory.remember("用户喜欢喝美式咖啡", kind="long_term")
        window = pet.open_memory()
        self.addCleanup(window.win.destroy)
        window.tree.selection_set(str(number))
        with mock.patch.object(memory_view.messagebox, "askyesno", return_value=False):
            self.assertFalse(window.delete_selected())
        self.assertIsNotNone(memory.repository.get_memory(number))
        with mock.patch.object(memory_view.messagebox, "askyesno", return_value=True):
            self.assertTrue(window.delete_selected())
        self.assertIsNone(memory.repository.get_memory(number))

    def test_lock_from_the_interface_sticks(self):
        pet, memory = self.build_pet()
        number = memory.remember("重要的一条", kind="long_term")
        window = pet.open_memory()
        self.addCleanup(window.win.destroy)
        window.tree.selection_set(str(number))
        self.assertTrue(window.toggle_lock())
        self.assertEqual(memory.repository.get_memory(number)["locked"], 1)

    def test_window_degrades_when_memory_is_missing(self):
        pet, _ = self.build_pet()
        pet.memory = None
        window = pet.open_memory()
        self.addCleanup(window.win.destroy)
        self.assertEqual(window.refresh(), [])
        self.assertIn("记忆不可用", str(window.hint.cget("text")))

    def test_stats_line_reports_counts(self):
        pet, memory = self.build_pet()
        memory.remember("长期一条", kind="long_term")
        memory.remember("项目一条", kind="project")
        window = pet.open_memory()
        self.addCleanup(window.win.destroy)
        text = str(window.stats_label.cget("text"))
        self.assertIn("共 2 条", text)
        self.assertIn("长期事实", text)


if __name__ == "__main__":
    unittest.main()
