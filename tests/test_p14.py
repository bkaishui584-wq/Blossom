"""P14 验收：Memory V2 —— 记忆分层、来源、冲突覆盖、衰减、防幻觉。"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.context import ContextManager
from agent.core import DEFAULT_SYSTEM_PROMPT, AgentCore
from database import db as db_module
from database.db import Database
from memory.manager import KINDS, MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatResult, ModelProvider


class ScriptProvider(ModelProvider):
    name = "script"

    def __init__(self, reply="嗯。", available=True):
        self.reply = reply
        self.available = available
        self.calls = []

    def is_available(self):
        return self.available

    def chat(self, messages, tools=None, temperature=0.7):
        self.calls.append([dict(m) for m in messages])
        return ChatResult(ok=True, content=self.reply, model=self.name)

    def stream(self, messages, tools=None, temperature=0.7):
        raise NotImplementedError


def make_memory(tmp, **kwargs):
    database = Database(Path(tmp) / "t.sqlite3")
    database.migrate()
    return MemoryManager(MemoryRepository(database), **kwargs), database


def build(tmp, companion=None, **kwargs):
    memory, database = make_memory(tmp, **kwargs)
    agent = AgentCore(companion, memory, ContextManager(token_budget=800))
    return agent, memory, database


def age(database, memory_id, days):
    database.execute(
        "UPDATE memories SET created_at = datetime('now', ?) WHERE id = ?",
        ("-%d days" % days, memory_id),
    )


class MigrationTests(unittest.TestCase):
    def test_memory_v2_columns_exist_and_migrate_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, database = make_memory(tmp)
            columns = {row["name"] for row in database.query("PRAGMA table_info(memories)")}
            self.assertTrue({"source", "confidence", "superseded_by"} <= columns)
            self.assertEqual(database.migrate(), [])

    def test_old_database_upgrades_in_place_without_losing_memories(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / "t.sqlite3")
            with mock.patch.object(db_module, "MIGRATIONS", db_module.MIGRATIONS[:3]):
                database.migrate()
            database.insert(
                "INSERT INTO memories (kind, content, keywords, importance) VALUES (?, ?, ?, ?)",
                ("long_term", "旧版记忆", "旧版,记忆", 1),
            )
            self.assertIn(4, database.migrate())
            row = database.query_one("SELECT * FROM memories WHERE content = ?", ("旧版记忆",))
            self.assertEqual(row["content"], "旧版记忆")
            self.assertEqual(row["source"], "USER_EXPLICIT")
            self.assertEqual(row["superseded_by"], 0)


class SourceTests(unittest.TestCase):
    def test_source_and_confidence_are_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, database = make_memory(tmp)
            explicit = memory.remember("用户喜欢喝美式咖啡", source="USER_EXPLICIT")
            inferred = memory.remember("用户可能在用 Python", source="MODEL_INFERRED")
            rows = {row["id"]: row for row in database.query("SELECT * FROM memories")}
            self.assertEqual(rows[explicit]["source"], "USER_EXPLICIT")
            self.assertEqual(rows[explicit]["confidence"], 1.0)
            self.assertEqual(rows[inferred]["source"], "MODEL_INFERRED")
            self.assertLess(rows[inferred]["confidence"], 1.0)

    def test_less_trusted_memory_does_not_override_explicit_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, database = make_memory(tmp)
            memory.remember("用户喜欢喝美式咖啡", source="USER_EXPLICIT")
            memory.remember("用户喜欢喝拿铁", source="MODEL_INFERRED")
            hits = memory.search("咖啡")
            self.assertEqual(len(hits), 1)
            self.assertIn("美式", hits[0]["content"])
            kept = database.query_one(
                "SELECT superseded_by FROM memories WHERE content LIKE ?", ("%拿铁%",)
            )
            self.assertEqual(kept["superseded_by"], 0)


class ConflictTests(unittest.TestCase):
    def test_new_explicit_memory_replaces_older_overlapping_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, database = make_memory(tmp)
            old_id = memory.remember("用户喜欢喝美式咖啡")
            new_id = memory.remember("用户喜欢喝拿铁")
            self.assertEqual(memory.search("咖啡"), [])
            self.assertEqual([row["id"] for row in memory.search("拿铁")], [new_id])
            old_row = database.query_one("SELECT * FROM memories WHERE id = ?", (old_id,))
            self.assertEqual(old_row["superseded_by"], new_id)
            self.assertEqual(old_row["content"], "用户喜欢喝美式咖啡")

    def test_unrelated_memory_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, _ = make_memory(tmp)
            memory.remember("用户喜欢喝美式咖啡")
            memory.remember("项目截止日期是三月十号")
            self.assertEqual(len(memory.recent_memories(limit=10)), 2)

    def test_superseded_memory_leaves_listing_and_recall(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, _ = make_memory(tmp)
            memory.remember("用户喜欢喝美式咖啡")
            memory.remember("用户喜欢喝拿铁")
            contents = [row["content"] for row in memory.recent_memories(limit=10)]
            self.assertEqual(contents, ["用户喜欢喝拿铁"])
            _, block, _ = memory.recall("s", "我喜欢喝什么")
            self.assertIn("拿铁", block)
            self.assertNotIn("美式", block)


class DecayTests(unittest.TestCase):
    def test_important_memory_outlives_a_decayed_normal_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, database = make_memory(tmp)
            fresh = memory.remember("用户喜欢喝美式咖啡", kind="long_term", importance=1)
            stale = memory.remember("用户喜欢把项目放在 D 盘", kind="project", importance=1)
            valued = memory.remember("用户喜欢我说话简短一点", kind="relationship", importance=2)
            age(database, stale, 400)
            age(database, valued, 400)
            order = [row["id"] for row in memory.search("喜欢", limit=3)]
            self.assertLess(order.index(valued), order.index(stale))
            self.assertLess(order.index(fresh), order.index(stale))

    def test_uncertain_memory_ranks_below_confident_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, _ = make_memory(tmp)
            sure = memory.remember("用户喜欢喝美式咖啡", source="USER_EXPLICIT")
            guess = memory.remember("用户喜欢喝拿铁", kind="project", source="MODEL_INFERRED")
            order = [row["id"] for row in memory.search("喜欢", limit=2)]
            self.assertLess(order.index(sure), order.index(guess))


class RecallTests(unittest.TestCase):
    def test_relationship_memory_is_labelled_in_recall_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, _ = make_memory(tmp)
            memory.remember("用户喜欢被叫小菲", kind="relationship", importance=2)
            _, block, _ = memory.recall("s", "我喜欢什么样的称呼")
            self.assertIn("和用户的关系：", block)
            self.assertIn("小菲", block)

    def test_recall_stats_report_what_was_retrieved(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, _ = make_memory(tmp)
            memory.remember("用户喜欢喝美式咖啡")
            memory.recall("s", "咖啡")
            self.assertEqual(memory.last_recall["memory_count"], 1)
            self.assertEqual(len(memory.last_recall["memory_ids"]), 1)

    def test_relationship_and_feedback_layers_are_retrievable(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, _ = make_memory(tmp)
            memory.remember("用户喜欢被叫小菲", kind="relationship")
            memory.remember("用户不喜欢我长篇大论", kind="feedback")
            contents = [row["content"] for row in memory.recent_memories(limit=10)]
            self.assertEqual(len(contents), 2)
            self.assertTrue(set(KINDS) >= {"relationship", "feedback"})


class AntiHallucinationTests(unittest.TestCase):
    def test_prompt_forbids_inventing_user_facts(self):
        self.assertIn("已知记忆", DEFAULT_SYSTEM_PROMPT)
        self.assertIn("不要猜", DEFAULT_SYSTEM_PROMPT)

    def test_unknown_personal_question_says_it_has_not_remembered(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _, _ = build(tmp, None)
            reply = agent.respond("s", "我最喜欢什么")
            self.assertTrue(reply.ok)
            self.assertEqual(reply.content, "我还没有记住这个。")

    def test_unknown_personal_question_does_not_call_the_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = ScriptProvider(reply="你喜欢蓝色。")
            agent, _, _ = build(tmp, provider)
            reply = agent.respond("s", "我喜欢什么")
            self.assertEqual(provider.calls, [])
            self.assertEqual(reply.content, "我还没有记住这个。")
            self.assertEqual(agent.last_debug.get("capability"), "memory")

    def test_listing_question_still_lists_memories(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _, _ = build(tmp, None)
            agent.respond("s", "记住我喜欢喝美式咖啡")
            reply = agent.respond("s", "你还记得什么")
            self.assertTrue(reply.ok)
            self.assertTrue(reply.content.startswith("我记着"))
            self.assertIn("美式咖啡", reply.content)

    def test_personal_question_with_interrogative_in_the_middle(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, memory, _ = build(tmp, None)
            memory.remember("用户喜欢被叫小菲", kind="relationship", importance=2)
            reply = agent.respond("s", "我喜欢什么样的称呼")
            self.assertTrue(reply.ok)
            self.assertIn("小菲", reply.content)

    def test_known_personal_question_is_answered_from_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = ScriptProvider()
            agent, memory, _ = build(tmp, provider)
            memory.remember("用户喜欢喝美式咖啡")
            reply = agent.respond("s", "我喜欢喝什么")
            self.assertTrue(reply.ok)
            self.assertIn("美式咖啡", reply.content)
            self.assertEqual(provider.calls, [])


class RememberRoutingTests(unittest.TestCase):
    def test_relationship_and_feedback_get_their_own_layer(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _, database = build(tmp, None)
            agent.respond("s", "记住我喜欢你叫我小菲")
            agent.respond("s", "记住你别用这么长的句子说话")
            kinds = {
                row["content"]: row["kind"]
                for row in database.query("SELECT content, kind FROM memories")
            }
            self.assertEqual(kinds["记住我喜欢你叫我小菲"], "relationship")
            self.assertEqual(kinds["记住你别用这么长的句子说话"], "feedback")
            for row in database.query("SELECT * FROM memories"):
                self.assertEqual(row["importance"], 2)
                self.assertEqual(row["source"], "USER_EXPLICIT")

    def test_plain_remember_stays_long_term(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, memory, _ = build(tmp, None)
            reply = agent.respond("s", "记住我喜欢晚上学习")
            self.assertTrue(reply.ok)
            self.assertTrue(memory.search("晚上学习"))
            row = memory.recent_memories(limit=1)[0]
            self.assertEqual(row["kind"], "long_term")


if __name__ == "__main__":
    unittest.main()
