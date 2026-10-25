"""P1 验收：记忆读写、旧对话压缩、相关性检索、上下文预算、Agent Core 降级。"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.context import ContextManager, estimate_tokens
from agent.core import AgentCore
from database.db import Database
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatDelta, ChatResult, ModelProvider, Usage


def make_memory(tmp, **kwargs):
    database = Database(Path(tmp) / "t.sqlite3")
    database.migrate()
    return MemoryManager(MemoryRepository(database), **kwargs)


class FakeProvider(ModelProvider):
    name = "fake"

    def __init__(self, reply="好的", usage=None, fail=False):
        self.reply = reply
        self.usage = usage or Usage(prompt_tokens=10, completion_tokens=5)
        self.fail = fail
        self.seen = None

    def is_available(self):
        return True

    def chat(self, messages, tools=None, temperature=0.7):
        self.seen = messages
        if self.fail:
            return ChatResult(ok=False, error="boom", model="fake")
        return ChatResult(ok=True, content=self.reply, usage=self.usage, model="fake")

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


class MemoryTests(unittest.TestCase):
    def test_recent_window_returns_newest_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = make_memory(tmp)
            for i in range(5):
                memory.record("s", "user", f"m{i}")
            rows = memory.repository.recent_messages("s", 3)
            self.assertEqual([r["content"] for r in rows], ["m2", "m3", "m4"])

    def test_old_messages_compress_into_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = make_memory(tmp, recent_window=2, summary_trigger=4)
            for i in range(8):
                memory.record("s", "user", f"消息{i}")
            summary_row = memory.repository.latest_summary("s")
            self.assertIsNotNone(summary_row)
            self.assertIn("消息0", summary_row["content"])
            self.assertLess(memory.repository.count_unsummarized("s"), 8)
            self.assertLessEqual(len(summary_row["content"]), memory.summary_max_chars)

    def test_search_returns_only_relevant_memories(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = make_memory(tmp, memory_limit=1)
            memory.remember("用户喜欢喝美式咖啡")
            memory.remember("项目截止日期是三月十号")
            hits = memory.search("咖啡")
            self.assertEqual(len(hits), 1)
            self.assertIn("美式咖啡", hits[0]["content"])

    def test_preferences_and_summary_enter_recall_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = make_memory(tmp)
            memory.set_preference("称呼", "主人")
            memory.record("s", "user", "你好")
            _, block, _ = memory.recall("s", "你好")
            self.assertIn("用户偏好 称呼: 主人", block)

    def test_usage_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = make_memory(tmp)
            memory.record_usage("s", "fake", Usage(prompt_tokens=7, completion_tokens=3))
            totals = memory.usage_totals("s")
            self.assertEqual(totals["total_tokens"], 10)


class ContextTests(unittest.TestCase):
    def test_budget_drops_oldest_but_keeps_system_and_input(self):
        context = ContextManager(token_budget=60, recent_window=20)
        recent = [{"role": "user", "content": "x" * 200} for _ in range(5)]
        bundle = context.build(
            system_prompt="sys", recent_messages=recent, current_input="现在的问题"
        )
        self.assertEqual(bundle.messages[0]["role"], "system")
        self.assertEqual(bundle.messages[-1]["content"], "现在的问题")
        self.assertLess(bundle.stats["recent_sent"], 5)
        self.assertIn("estimated_tokens", bundle.stats)

    def test_memory_block_and_summary_are_separate_system_message(self):
        context = ContextManager(token_budget=500)
        bundle = context.build(
            system_prompt="sys",
            recent_messages=[],
            memory_block="- 喜欢咖啡",
            summary="更早聊过天气",
        )
        self.assertIn("喜欢咖啡", bundle.messages[1]["content"])
        self.assertIn("更早聊过天气", bundle.messages[1]["content"])

    def test_estimate_tokens_grows_with_text(self):
        self.assertLess(estimate_tokens("短"), estimate_tokens("长" * 100))


class AgentCoreTests(unittest.TestCase):
    def test_reply_saves_pair_and_sends_current_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = make_memory(tmp)
            provider = FakeProvider(reply="我在")
            agent = AgentCore(provider, memory, ContextManager(token_budget=800))
            reply = agent.respond("s", "在吗")
            self.assertTrue(reply.ok)
            self.assertEqual(reply.content, "我在")
            self.assertEqual(provider.seen[-1], {"role": "user", "content": "在吗"})
            roles = [r["role"] for r in memory.repository.recent_messages("s", 10)]
            self.assertEqual(roles, ["user", "assistant"])
            self.assertEqual(memory.usage_totals("s")["total_tokens"], 15)

    def test_model_failure_reports_error_and_saves_no_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = make_memory(tmp)
            agent = AgentCore(FakeProvider(fail=True), memory, ContextManager())
            reply = agent.respond("s", "在吗")
            self.assertFalse(reply.ok)
            self.assertEqual(reply.error, "boom")
            roles = [r["role"] for r in memory.repository.recent_messages("s", 10)]
            self.assertEqual(roles, ["user"])

    def test_missing_provider_is_reported_not_faked(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = AgentCore(None, make_memory(tmp), ContextManager())
            reply = agent.respond("s", "讲个笑话")
            self.assertFalse(reply.ok)
            self.assertIn("没有连接聊天模型", reply.error)
            self.assertFalse(reply.content)

    def test_memory_failure_degrades_to_memoryless_chat(self):
        class BrokenMemory:
            def record(self, *args, **kwargs):
                raise RuntimeError("db down")

            def recall(self, *args, **kwargs):
                raise RuntimeError("db down")

            def record_usage(self, *args, **kwargs):
                raise RuntimeError("db down")

        provider = FakeProvider(reply="降级也能聊")
        agent = AgentCore(provider, BrokenMemory(), ContextManager())
        reply = agent.respond("s", "在吗")
        self.assertTrue(reply.ok)
        self.assertEqual(reply.content, "降级也能聊")
        self.assertEqual(provider.seen[-1]["content"], "在吗")


if __name__ == "__main__":
    unittest.main()
