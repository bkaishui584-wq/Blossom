"""P2 关系状态与人格样本：认识多久、聊过多少次；样本只收集不训练。"""

import os
import sys
import tempfile
import time
import tkinter as tk
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.capabilities import CapabilityRouter
from app.main import build_agent
from database.db import Database
from logkit.logger import get_logger
from memory.manager import MemoryManager
from memory.relationship import STAGES, RelationshipState
from memory.repository import MemoryRepository
from persona.interface import SAFETY_BOUNDARY
from persona.state import EmotionState
from persona.training import KIND_LABELS, KINDS, LocalTrainingCollector
from security.config import Config
from ui.desktop import DesktopPet


class Clock:
    def __init__(self, now=None):
        self.now = now or time.time()

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


class StubAgent:
    def __init__(self):
        self.avatar = None
        self.emotion = EmotionState()
        self.persona = None
        self.mode_name = "local"


class TempDatabase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(os.path.join(self.tmp.name, "deskpet.sqlite3"))
        self.db.migrate()


class RelationshipTests(TempDatabase):
    def state(self, now=None):
        return RelationshipState(self.db, clock=Clock(now))

    def test_first_touch_starts_the_record(self):
        state = self.state()
        self.assertEqual(state.note(), "")
        state.touch()
        info = state.describe()
        self.assertEqual(info["interactions"], 1)
        self.assertEqual(info["first_seen"], datetime.now().strftime("%Y-%m-%d"))
        self.assertEqual(info["stage"], "刚认识")

    def test_interactions_add_up(self):
        state = self.state()
        for _ in range(5):
            state.touch()
        self.assertEqual(state.interactions(), 5)

    def test_days_and_stage_grow(self):
        clock = Clock()
        state = RelationshipState(self.db, clock=clock)
        state.touch()
        clock.advance(8 * 24 * 3600)
        self.assertEqual(state.days_known(), 8)
        self.assertEqual(state.stage(), "老朋友")
        clock.advance(30 * 24 * 3600)
        self.assertEqual(state.stage(), "很亲近")

    def test_note_reads_like_a_sentence(self):
        state = self.state()
        state.touch()
        note = state.note()
        self.assertIn("认识", note)
        self.assertIn("聊过", note)

    def test_missing_database_stays_quiet(self):
        state = RelationshipState(None)
        self.assertEqual(state.describe()["interactions"], 0)
        self.assertFalse(state._write({"interactions": 1}))
        self.assertEqual(state.describe()["interactions"], 0)

    def test_stage_table_is_ordered(self):
        days = [item[0] for item in STAGES]
        self.assertEqual(days, sorted(days, reverse=True))


class MigrationTests(TempDatabase):
    def test_new_tables_are_created(self):
        names = [row["name"] for row in self.db.query("SELECT name FROM sqlite_master")]
        self.assertIn("diary_entries", names)
        self.assertIn("relationship_state", names)
        versions = [row["version"] for row in self.db.query("SELECT version FROM schema_migrations")]
        self.assertIn(6, versions)
        self.assertIn(7, versions)

    def test_migrating_again_changes_nothing(self):
        self.db.migrate()
        self.assertEqual(self.db.migrate(), [])
        self.db.insert("INSERT INTO memories (kind, content, keywords) VALUES (?, ?, ?)",
                       ("long_term", "用户喜欢早睡", "早睡"))
        self.db.migrate()
        rows = self.db.query("SELECT content FROM memories")
        self.assertEqual([row["content"] for row in rows], ["用户喜欢早睡"])


class MemoryTests(TempDatabase):
    def test_recording_does_not_double_count_relationship(self):
        memory = MemoryManager(MemoryRepository(self.db))
        memory.record("s", "user", "你好")
        memory.record("s", "assistant", "在呢")
        # 计数由 Agent Core 的用户回合负责，存储层不重复算。
        self.assertEqual(memory.relationship.interactions(), 0)
        memory.relationship.touch()
        self.assertEqual(memory.relationship.interactions(), 1)

    def test_recall_carries_one_relationship_line(self):
        memory = MemoryManager(MemoryRepository(self.db))
        memory.relationship.touch()
        block = memory.recall("s", "你")[1]
        self.assertEqual(block.count("聊过"), 1)
        self.assertIn("我们认识", block)

    def test_recall_without_relationship_stays_clean(self):
        class Bare:
            def add_message(self, *args):
                return 1

            def recent_messages(self, *args, **kwargs):
                return []

            def latest_summary(self, *args):
                return None

            def candidate_memories(self, *args, **kwargs):
                return []

            def all_preferences(self):
                return []

        memory = MemoryManager(Bare())
        self.assertEqual(memory.recall("s", "你")[1], "")
        self.assertEqual(memory.relationship.note(), "")


class RelationshipCapabilityTests(TempDatabase):
    def test_local_answer_needs_no_model(self):
        memory = MemoryManager(MemoryRepository(self.db))
        memory.relationship.touch()
        router = CapabilityRouter(memory=memory)
        answer = router.resolve("我们认识多久了")
        self.assertIsNotNone(answer)
        self.assertEqual(answer.capability, "relationship")
        self.assertIn("聊过", answer.text)

    def test_plain_chat_is_not_swallowed(self):
        memory = MemoryManager(MemoryRepository(self.db))
        router = CapabilityRouter(memory=memory)
        self.assertIsNone(router.resolve("我们去看电影吧"))

    def test_agent_counts_turns_without_a_model(self):
        config = Config(load_env=False)
        config.db_path = os.path.join(self.tmp.name, "deskpet.sqlite3")
        db = Database(config.db_path)
        db.migrate()
        agent, memory = build_agent(config, db, None, get_logger())
        agent.respond("s", "你好")
        agent.respond("s", "现在几点")
        self.assertEqual(memory.relationship.interactions(), 2)


class TrainingTests(TempDatabase):
    def collector(self):
        return LocalTrainingCollector(self.db)

    def test_samples_are_collected_but_never_trained(self):
        collector = self.collector()
        self.assertIsNotNone(collector.collect("feedback", "别叫我亲"))
        status = collector.status()
        self.assertEqual(status["total"], 1)
        self.assertEqual(status["kinds"]["feedback"], 1)
        self.assertFalse(status["training"])

    def test_unknown_kind_is_rejected(self):
        collector = self.collector()
        self.assertIsNone(collector.collect("whatever", "x"))
        self.assertEqual(collector.status()["total"], 0)

    def test_export_keeps_everything_for_the_user(self):
        collector = self.collector()
        collector.collect("feedback", "说话短一点")
        collector.collect("dialogue", {"user": "在吗", "pet": "在"})
        exported = collector.export()
        self.assertEqual(len(exported["samples"]), 2)
        self.assertEqual(exported["version"], collector.version)

    def test_kind_labels_cover_every_kind(self):
        for kind in KINDS:
            self.assertIn(kind, KIND_LABELS)


class PanelTests(TempDatabase):
    def setUp(self):
        super().setUp()
        try:
            self.root = tk.Tk()
        except Exception as exc:
            self.skipTest(f"tkinter 不可用：{exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy)

    def _destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def test_panel_shows_relationship_and_samples(self):
        config = Config(load_env=False)
        config.dev_mode = True
        # 这台机器上装了真模型时，测试不该顺手把真渲染窗口拉起来。
        config.live2d_dir = os.path.join(self.tmp.name, "live2d")
        db = Database(os.path.join(self.tmp.name, "panel.sqlite3"))
        db.migrate()
        agent, memory = build_agent(config, db, None, get_logger())
        pet = DesktopPet(agent, config, memory=memory, root=self.root)
        pet._set_dev({})
        text = pet.dev.get("1.0", "end")
        self.assertIn("关系", text)
        self.assertIn("人格样本", text)

    def test_safety_boundary_survives_any_persona(self):
        from persona.interface import build_persona_note

        config = Config(load_env=False)
        config.persona_traits = "黏人:80"
        agent, _ = build_agent(config, Database(os.path.join(self.tmp.name, "b.sqlite3")), None, get_logger())
        note = build_persona_note(agent.persona)
        self.assertIn(SAFETY_BOUNDARY, note)


if __name__ == "__main__":
    unittest.main()
