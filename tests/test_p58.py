"""阶段 V5-D 验收：流式回复与真正的停止生成。

模型边生成边出字，用户点停止就停在当下。几条底线：
    流里夹带工具请求就退回整段调用，不能漏掉工具；
    流到一半断了，已有的字如实保留，标记 partial，不重发；
    取消之后迟到的回复不许再画到界面上；
    没有 on_delta 时行为与以前完全一样。
"""

import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.context import ContextManager  # noqa: E402
from agent.core import AgentCore, AgentReply  # noqa: E402
from database.db import Database  # noqa: E402
from memory.manager import MemoryManager  # noqa: E402
from memory.repository import MemoryRepository  # noqa: E402
from model.base import ChatDelta, ChatResult, ModelProvider, ToolCall, Usage  # noqa: E402
from security.config import Config  # noqa: E402
from ui.chat_view import ChatView  # noqa: E402
from ui.desktop import DesktopPet  # noqa: E402


class StreamingProvider(ModelProvider):
    """能流式的模型替身。pieces 为空就当作"流不出东西"。"""

    name = "streamer"

    def __init__(self, pieces=("你", "好"), result="你好", fail_after=None, tool_in_stream=False):
        self.pieces = list(pieces)
        self.result = result
        self.fail_after = fail_after
        self.tool_in_stream = tool_in_stream
        self.stream_calls = 0
        self.chat_calls = 0

    def is_available(self):
        return True

    def chat(self, messages, tools=None, temperature=0.7):
        self.chat_calls += 1
        return ChatResult(
            ok=True,
            content=self.result,
            usage=Usage(prompt_tokens=4, completion_tokens=2),
            model="streamer",
        )

    def stream(self, messages, tools=None, temperature=0.7):
        self.stream_calls += 1
        for index, piece in enumerate(self.pieces):
            if self.fail_after is not None and index >= self.fail_after:
                raise RuntimeError("连接断了")
            yield ChatDelta(content=piece)
        if self.tool_in_stream:
            yield ChatDelta(tool_calls=[ToolCall(id="t1", name="read_file", arguments={})])
        yield ChatDelta(done=True, usage=Usage(prompt_tokens=5, completion_tokens=7))


class PlainProvider(StreamingProvider):
    """没有 stream 方法的模型：接口不完整时也要能用。"""

    def stream(self, messages, tools=None, temperature=0.7):  # pragma: no cover
        raise AssertionError("不该被调用")


class ChatViewStreamTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        self.root.withdraw()
        self.addCleanup(self._destroy)
        self.view = ChatView(self.root)

    def _destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def test_stream_shows_partial_text_before_it_finishes(self):
        self.view.begin_stream()
        self.view.push_stream("正在说")
        self.assertIn("正在说", self.view.history_text())
        self.assertTrue(self.view.streaming())

    def test_final_body_replaces_the_draft(self):
        self.view.begin_stream()
        self.view.push_stream("草稿")
        self.view.end_stream("桌宠：最终正文")
        text = self.view.history_text()
        self.assertIn("最终正文", text)
        self.assertNotIn("草稿", text)
        self.assertFalse(self.view.streaming())

    def test_empty_body_leaves_nothing_behind(self):
        self.view.begin_stream()
        self.view.push_stream("半句")
        self.view.end_stream(None)
        text = self.view.history_text()
        self.assertNotIn("半句", text)
        self.assertNotIn("桌宠：", text)

    def test_push_without_begin_is_ignored(self):
        self.assertEqual(self.view.push_stream("散字"), "")
        self.assertNotIn("散字", self.view.history_text())

    def test_end_without_begin_is_a_no_op(self):
        self.assertFalse(self.view.end_stream("正文"))

    def test_code_fences_in_the_final_body_still_render_as_blocks(self):
        fence = "\x60\x60\x60"
        self.view.begin_stream()
        self.view.push_stream("片段")
        self.view.end_stream("桌宠：看这个\n%s\ncode\n%s" % (fence, fence))
        text = self.view.history_text()
        self.assertIn("code", text)
        self.assertNotIn("片段", text)


class CoreStreamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.tmp.name) / "t.sqlite3")
        self.database.migrate()
        self.memory = MemoryManager(MemoryRepository(self.database))

    def tearDown(self):
        self.tmp.cleanup()

    def _agent(self, provider):
        return AgentCore(provider, self.memory, ContextManager(token_budget=800))

    def test_deltas_arrive_in_order_and_content_matches(self):
        provider = StreamingProvider(pieces=("你", "好", "呀"))
        agent = self._agent(provider)
        got = []
        reply = agent.respond("s1", "在吗", on_delta=got.append)
        self.assertEqual(got, ["你", "好", "呀"])
        self.assertEqual(reply.content, "你好呀")
        self.assertTrue(reply.ok)
        self.assertEqual(provider.chat_calls, 0)
        self.assertIn("你好呀", [row["content"] for row in self.memory.repository.recent_messages("s1", 5)])

    def test_streamed_reply_is_marked_streamed(self):
        reply = self._agent(StreamingProvider()).respond("s1", "在吗", on_delta=lambda chunk: None)
        self.assertTrue(reply.debug.get("streamed"))

    def test_stream_usage_is_recorded(self):
        agent = self._agent(StreamingProvider())
        agent.respond("s1", "在吗", on_delta=lambda chunk: None)
        totals = self.memory.usage_totals("s1")
        self.assertEqual(totals["completion_tokens"], 7)

    def test_without_callback_behaviour_is_unchanged(self):
        provider = StreamingProvider()
        reply = self._agent(provider).respond("s1", "在吗")
        self.assertTrue(reply.ok)
        self.assertEqual(provider.chat_calls, 1)
        self.assertEqual(provider.stream_calls, 0)
        self.assertNotIn("streamed", reply.debug)

    def test_partial_stream_is_kept_and_marked_partial(self):
        provider = StreamingProvider(pieces=("半", "句", "没说完"), fail_after=2)
        agent = self._agent(provider)
        got = []
        reply = agent.respond("s1", "在吗", on_delta=got.append)
        self.assertEqual(got, ["半", "句"])
        self.assertEqual(reply.content, "半句")
        self.assertTrue(reply.ok)
        self.assertTrue(reply.debug.get("partial"))
        self.assertEqual(provider.chat_calls, 0)

    def test_broken_stream_without_any_text_falls_back(self):
        provider = StreamingProvider(pieces=(), fail_after=0)
        agent = self._agent(provider)
        reply = agent.respond("s1", "在吗", on_delta=lambda chunk: None)
        self.assertTrue(reply.ok)
        self.assertEqual(reply.content, "你好")
        self.assertEqual(provider.chat_calls, 1)

    def test_empty_stream_falls_back_to_a_normal_call(self):
        provider = StreamingProvider(pieces=())
        reply = self._agent(provider).respond("s1", "在吗", on_delta=lambda chunk: None)
        self.assertEqual(reply.content, "你好")
        self.assertEqual(provider.chat_calls, 1)

    def test_tool_call_in_the_stream_falls_back_to_the_buffered_path(self):
        provider = StreamingProvider(tool_in_stream=True)
        agent = self._agent(provider)
        reply = agent.respond("s1", "在吗", on_delta=lambda chunk: None)
        self.assertEqual(provider.chat_calls, 1)
        self.assertFalse(reply.debug.get("streamed"))

    def test_cancel_stops_the_stream_and_marks_the_reply(self):
        provider = StreamingProvider(pieces=("一", "二", "三", "四"))
        agent = self._agent(provider)
        got = []

        def sink(chunk):
            got.append(chunk)
            if len(got) == 2:
                agent.cancel_generation()

        reply = agent.respond("s1", "在吗", on_delta=sink)
        self.assertEqual(got, ["一", "二"])
        self.assertFalse(reply.ok)
        self.assertEqual(reply.error, "已取消")
        self.assertTrue(reply.debug.get("cancelled"))
        self.assertEqual(reply.content, "一二")

    def test_cancel_flag_resets_for_the_next_turn(self):
        provider = StreamingProvider()
        agent = self._agent(provider)
        agent.cancel_generation()
        reply = agent.respond("s1", "在吗", on_delta=lambda chunk: None)
        self.assertTrue(reply.ok)
        self.assertFalse(agent.generation_cancelled())

    def test_broken_delta_callback_does_not_lose_the_reply(self):
        def boom(chunk):
            raise RuntimeError("界面回调炸了")

        reply = self._agent(StreamingProvider()).respond("s1", "在吗", on_delta=boom)
        self.assertTrue(reply.ok)
        self.assertEqual(reply.content, "你好")


class DesktopStreamTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except Exception as exc:
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
        self.config.tray = False
        database = Database(base / "t.sqlite3")
        database.migrate()
        self.memory = MemoryManager(MemoryRepository(database))

    def _destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _pet(self, provider):
        agent = AgentCore(provider, self.memory, ContextManager(token_budget=800))
        return DesktopPet(agent, self.config, memory=self.memory, root=self.root)

    def _drain(self, pet):
        pet._drain()
        return pet

    def test_reply_streams_into_the_chat_once(self):
        provider = StreamingProvider(pieces=("你", "好", "呀"))
        pet = self._pet(provider)
        pot = pet._next_reply_token()
        pet._worker_reply("在吗", pot)
        pet._drain()
        text = pet.chat.history_text()
        self.assertEqual(text.count("你好呀"), 1)
        self.assertFalse(pet._busy)
        pet.close()

    def test_busy_state_ends_after_the_stream(self):
        pet = self._pet(StreamingProvider())
        pot = pet._next_reply_token()
        pet._worker_reply("在吗", pot)
        pet._drain()
        self.assertFalse(pet.chat.busy())
        pet.close()

    def test_cancel_drops_the_late_reply(self):
        provider = StreamingProvider(pieces=("稍", "等"))
        pet = self._pet(provider)
        token = pet._next_reply_token()
        pet._set_busy(True)  # 真实场景里取消时就是「正在生成」
        pet._stream_chunk("稍", token)
        pet._cancel()
        pet._apply_reply(AgentReply(ok=True, content="迟到的回复"), token)
        text = pet.chat.history_text()
        self.assertNotIn("迟到的回复", text)
        self.assertIn("已取消", pet.chat.state.cget("text"))
        pet.close()

    def test_stale_deltas_are_ignored(self):
        pet = self._pet(StreamingProvider())
        old = pet._next_reply_token()
        pet._next_reply_token()
        pet._stream_chunk("过期内容", old)
        self.assertNotIn("过期内容", pet.chat.history_text())
        self.assertFalse(pet._stream_open)
        pet.close()

    def test_live_deltas_open_exactly_one_bubble(self):
        pet = self._pet(StreamingProvider(pieces=("甲", "乙")))
        token = pet._next_reply_token()
        pet._stream_chunk("甲", token)
        pet._stream_chunk("乙", token)
        text = pet.chat.history_text()
        self.assertIn("甲乙", text)
        self.assertEqual(text.count("桌宠："), 1)
        pet.close()

    def test_failed_reply_after_streaming_keeps_the_draft_out(self):
        provider = StreamingProvider(pieces=("草", "稿"), fail_after=0)
        pet = self._pet(provider)
        token = pet._next_reply_token()
        pet._worker_reply("在吗", token)
        pet._drain()
        text = pet.chat.history_text()
        self.assertIn("你好", text)
        self.assertNotIn("草稿", text)
        pet.close()


if __name__ == "__main__":
    unittest.main()