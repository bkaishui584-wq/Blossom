"""阶段 A 验收：聊天界面、视觉规范、设置分区与安全边界。

测真实行为，不测「类和接口存在」：发消息真的走通、取消真的收尾、
清空真的要确认、模型没配真的不给红色报错、调完设置真的存进记忆。
"""

import sys
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.context import ContextManager
from agent.core import AgentCore, AgentReply
from database.db import Database
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatDelta, ChatResult, ModelProvider, Usage
from persona.interface import DefaultPersonaProvider
from security.config import Config
from ui import desktop as desktop_module
from ui import theme
from ui.chat_view import BUSY, ERROR, OK, WARN, ChatView
from ui.desktop import (
    CARD_HEIGHT,
    CARD_WIDTH,
    DesktopPet,
    PetCanvas,
    TkAvatarProvider,
    format_debug,
    format_status,
)
from ui.pet_host import TRANSPARENT_KEY

UI_FILES = ("ui/theme.py", "ui/chat_view.py", "ui/settings_view.py", "ui/desktop.py")
BANNED = ("os.system", "subprocess", "eval(", "exec(", "pickle.load")


class StubProvider(ModelProvider):
    name = "stub"

    def __init__(self, reply="在的。", fail=False):
        self.reply = reply
        self.fail = fail
        self.calls = 0

    def is_available(self):
        return True

    def chat(self, messages, tools=None, temperature=0.7):
        self.calls += 1
        if self.fail:
            return ChatResult(ok=False, error="模型不可用", model="stub")
        return ChatResult(
            ok=True,
            content=self.reply,
            usage=Usage(prompt_tokens=5, completion_tokens=3),
            model="stub",
        )

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


class FakeEvent:
    def __init__(self, x_root=0, y_root=0):
        self.x_root = x_root
        self.y_root = y_root


def label_texts(widget):
    """把一棵控件树里的文字全收出来，用来检查页面到底显示了什么。"""
    texts = []
    for child in widget.winfo_children():
        try:
            if child.winfo_class() == "Label":
                texts.append(str(child.cget("text")))
        except Exception:
            pass
        texts.extend(label_texts(child))
    return texts


class ThemeSpecTests(unittest.TestCase):
    """视觉规范集中在一处：颜色、字体、间距、按钮状态。"""

    def test_message_styles_cover_every_role(self):
        for tag in ("me", "pet", "err", "hint", "notice"):
            self.assertIn(tag, theme.MESSAGE_STYLES)
        self.assertEqual(theme.MESSAGE_STYLES["me"][2], "right")
        self.assertEqual(theme.MESSAGE_STYLES["pet"][2], "left")

    def test_state_colors_cover_every_state(self):
        for state in ("idle", "busy", "ok", "warn", "error"):
            self.assertIn(state, theme.STATE_COLORS)

    def test_error_bubble_is_not_the_pet_bubble(self):
        self.assertNotEqual(theme.MESSAGE_STYLES["err"][1], theme.MESSAGE_STYLES["pet"][1])
        self.assertEqual(theme.MESSAGE_STYLES["err"][0], theme.ERROR)

    def test_fonts_and_spacing_are_named_constants(self):
        self.assertTrue(theme.FONT_TITLE and theme.FONT_BODY and theme.FONT_SMALL)
        self.assertGreaterEqual(theme.PAD_LG, theme.PAD_SM >= theme.PAD_XS)
        self.assertGreater(theme.RADIUS, 0)


class TkTestCase(unittest.TestCase):
    """需要显示环境。没有显示时自动跳过。"""

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

    def build(self, provider=None, config=None, behavior=None, dev=False):
        database = Database(Path(self.tmp.name) / "t.sqlite3")
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        provider = provider or StubProvider()
        agent = AgentCore(
            provider,
            memory,
            ContextManager(token_budget=600),
            persona=DefaultPersonaProvider(),
        )
        config = config or Config(load_env=False)
        config.dev_mode = dev
        pet = DesktopPet(agent, config, memory=memory, root=self.root, behavior=behavior)
        return pet, agent, memory


class ChatViewTests(TkTestCase):
    def make_view(self):
        parent = tk.Toplevel(self.root)
        view = ChatView(parent, wraplength=300)
        return view

    def test_bubbles_are_styled_on_the_transcript(self):
        view = self.make_view()
        self.assertEqual(str(view.transcript.tag_cget("me", "justify")), "right")
        self.assertEqual(str(view.transcript.tag_cget("pet", "justify")), "left")
        self.assertEqual(str(view.transcript.tag_cget("err", "foreground")), theme.ERROR)

    def test_append_keeps_the_speaker_prefix_and_newline(self):
        view = self.make_view()
        view.append("pet", "桌宠：在的。")
        text = view.history_text()
        self.assertIn("桌宠：在的。", text)
        self.assertTrue(text.endswith(chr(10)))

    def test_append_leaves_the_view_read_only_for_users(self):
        view = self.make_view()
        view.append("me", "你：在吗")
        self.assertEqual(str(view.transcript.cget("state")), "disabled")

    def test_input_reads_and_clears(self):
        view = self.make_view()
        view.input.insert(0, "  在吗  ")
        self.assertEqual(view.read_input(), "在吗")
        view.clear_input()
        self.assertEqual(view.read_input(), "")

    def test_clear_empties_everything_and_resets_state(self):
        view = self.make_view()
        view.append("me", "你：在吗")
        view.set_state(ERROR, "出错了")
        view.clear()
        self.assertEqual(view.history_text().strip(), "")
        self.assertEqual(str(view.state.cget("text")), "")

    def test_state_uses_the_state_palette(self):
        view = self.make_view()
        view.set_state(ERROR, "出错了")
        self.assertEqual(str(view.state.cget("fg")), theme.ERROR)
        view.set_state(OK, "已完成")
        self.assertEqual(str(view.state.cget("fg")), theme.OK)

    def test_busy_swaps_send_for_cancel(self):
        view = self.make_view()
        view.set_busy(True)
        self.assertTrue(view.busy())
        self.assertEqual(view.cancel_button.winfo_manager(), "pack")
        view.set_busy(False)
        self.assertFalse(view.busy())
        self.assertEqual(view.cancel_button.winfo_manager(), "")

    def test_retry_button_shows_and_hides(self):
        view = self.make_view()
        self.assertEqual(view.retry_button.winfo_manager(), "")
        view.show_retry(True)
        self.assertEqual(view.retry_button.winfo_manager(), "pack")
        view.show_retry(False)
        self.assertEqual(view.retry_button.winfo_manager(), "")

    def test_copy_all_puts_the_history_on_the_clipboard(self):
        view = self.make_view()
        view.append("me", "你：在吗")
        copied = view.copy_all()
        self.assertIn("在吗", copied)
        self.assertIn("在吗", self.root.clipboard_get())

    def test_buttons_have_distinct_kinds(self):
        primary = theme.button(self.root, "发送", None, kind="primary")
        ghost = theme.button(self.root, "设置", None, kind="ghost")
        self.assertNotEqual(str(primary.cget("bg")), str(ghost.cget("bg")))


class DesktopChatFlowTests(TkTestCase):
    def wait_for(self, pet, needle, deadline=15.0):
        limit = time.time() + deadline
        while needle not in pet.transcript.get("1.0", "end") and time.time() < limit:
            pet._drain()
            self.root.update()
            time.sleep(0.02)
        return pet.transcript.get("1.0", "end")

    def test_send_flow_writes_both_sides_and_clears_input(self):
        pet, _, _ = self.build()
        pet.input.insert(0, "在吗")
        pet._send()
        text = self.wait_for(pet, "在的。")
        self.assertIn("你：在吗", text)
        self.assertIn("桌宠：在的。", text)
        self.assertEqual(pet.input.get(), "")

    def test_user_message_is_a_me_bubble_and_reply_a_pet_bubble(self):
        pet, _, _ = self.build()
        pet.input.insert(0, "在吗")
        pet._send()
        self.wait_for(pet, "在的。")
        self.assertTrue(pet.transcript.tag_ranges("me"))
        self.assertTrue(pet.transcript.tag_ranges("pet"))

    def test_send_is_ignored_while_busy(self):
        pet, _, _ = self.build()
        pet._set_busy(True)
        pet.input.insert(0, "再来一句")
        self.assertEqual(pet._send(), "break")
        self.assertNotIn("再来一句", pet.transcript.get("1.0", "end"))
        self.assertEqual(pet.input.get(), "再来一句")

    def test_busy_state_shows_thinking(self):
        pet, _, _ = self.build()
        pet._set_busy(True)
        self.assertEqual(str(pet.state.cget("text")), "思考中……")
        self.assertTrue(pet.chat.busy())

    def test_reply_marks_done_and_hides_retry(self):
        pet, _, _ = self.build()
        pet._set_busy(True)
        pet._apply_reply(AgentReply(ok=True, content="我在呢。"))
        self.assertEqual(str(pet.state.cget("text")), "已完成")
        self.assertEqual(str(pet.state.cget("fg")), theme.OK)
        self.assertEqual(pet.chat.retry_button.winfo_manager(), "")

    def test_failure_marks_error_and_offers_retry(self):
        pet, _, _ = self.build(provider=StubProvider(fail=True))
        pet.input.insert(0, "在吗")
        pet._send()
        self.wait_for(pet, "模型不可用")
        self.assertIn("模型不可用", pet.transcript.get("1.0", "end"))
        self.assertEqual(str(pet.state.cget("fg")), theme.ERROR)
        self.assertEqual(pet.chat.retry_button.winfo_manager(), "pack")
        self.assertTrue(pet.transcript.tag_ranges("err"))

    def test_cancel_marks_cancelled_and_offers_retry(self):
        pet, agent, _ = self.build()
        pet._set_busy(True)
        self.assertTrue(pet._cancel())
        self.assertFalse(pet._busy)
        self.assertEqual(str(pet.state.cget("text")), "已取消")
        self.assertEqual(pet.chat.retry_button.winfo_manager(), "pack")
        self.assertTrue(agent._cancel_requested)

    def test_cancel_without_anything_running_does_nothing(self):
        pet, agent, _ = self.build()
        self.assertFalse(pet._cancel())
        self.assertFalse(agent._cancel_requested)

    def test_retry_resends_the_last_user_message(self):
        provider = StubProvider()
        pet, _, _ = self.build(provider=provider)
        pet.input.insert(0, "在吗")
        pet._send()
        self.wait_for(pet, "在的。")
        before = provider.calls
        pet._retry()
        limit = time.time() + 5
        while (
            pet.transcript.get("1.0", "end").count("桌宠：在的。") < 2
            and time.time() < limit
        ):
            pet._drain()
            self.root.update()
            time.sleep(0.02)
        self.assertEqual(provider.calls, before + 1)
        self.assertEqual(pet.transcript.get("1.0", "end").count("你：在吗"), 2)

    def test_clear_chat_asks_before_wiping(self):
        pet, _, _ = self.build()
        pet.input.insert(0, "在吗")
        pet._send()
        self.wait_for(pet, "在的。")
        with mock.patch.object(
            desktop_module.messagebox, "askyesno", return_value=False
        ) as ask:
            self.assertFalse(pet._clear_chat())
            ask.assert_called_once()
        self.assertIn("在的。", pet.transcript.get("1.0", "end"))
        with mock.patch.object(
            desktop_module.messagebox, "askyesno", return_value=True
        ):
            self.assertTrue(pet._clear_chat())
        self.assertEqual(pet.transcript.get("1.0", "end").strip(), "")

    def test_unconfigured_model_is_a_short_notice_not_a_red_error(self):
        pet, _, _ = self.build()
        text = pet.transcript.get("1.0", "end")
        self.assertIn("尚未配置模型", text)
        self.assertTrue(pet.transcript.tag_ranges("notice"))
        self.assertFalse(pet.transcript.tag_ranges("err"))
        self.assertNotIn("Traceback", text)

    def test_normal_mode_hides_debug_panel(self):
        pet, _, _ = self.build()
        self.assertIsNone(pet.dev)

    def test_dev_mode_shows_debug_panel_only_there(self):
        pet, _, _ = self.build(dev=True)
        self.assertIsNotNone(pet.dev)
        self.assertEqual(str(pet.dev.cget("state")), "disabled")

    def test_status_line_still_reports_model_and_mic(self):
        pet, _, _ = self.build()
        pet._refresh_status()
        text = str(pet.status.cget("text"))
        self.assertIn("情绪", text)
        self.assertNotIn("tokens", text)
        detail = pet._status_full_text()
        self.assertIn("未配置", detail)
        self.assertIn("tokens", detail)

    def test_resize_keeps_a_minimum_size(self):
        pet, _, _ = self.build()
        start = pet._card_size
        pet._resize_start(FakeEvent(100, 100))
        pet._resize_move(FakeEvent(140, 160))
        self.assertEqual(pet._card_size, (start[0] + 40, start[1] + 60))
        pet._resize_move(FakeEvent(-2000, -2000))
        self.assertEqual(
            pet._card_size, (DesktopPet.MIN_CARD_WIDTH, DesktopPet.MIN_CARD_HEIGHT)
        )

    def test_chat_card_uses_the_resized_size(self):
        pet, _, _ = self.build()
        pet._card_size = (1000, 680)
        pet.show_chat()
        self.assertTrue(pet.card.geometry().startswith("1000x680"))

    def test_pet_interactions_still_work(self):
        pet, agent, _ = self.build()
        pet._on_pet_click()
        pet._on_pet_double_click()
        pet._on_pointer_near()
        pet._on_pointer_away()
        self.assertTrue(pet.toggle_chat())
        self.assertTrue(pet.chat_visible)
        self.assertFalse(pet.toggle_chat())
        pet.hide_pet()
        self.assertTrue(pet.host.hidden)

    def test_emotion_is_still_rendered_on_the_pet(self):
        pet, agent, _ = self.build()
        self.assertIsInstance(agent.avatar, TkAvatarProvider)
        agent.avatar.set_state({"emotion": "sad", "intensity": 0.9})
        pet._drain()
        self.assertEqual(pet.pet.expression, "sad")
        self.assertIsInstance(pet.pet, PetCanvas)

    def test_settings_window_uses_tabs(self):
        pet, _, _ = self.build()
        window = pet.open_settings()
        self.addCleanup(window.win.destroy)
        titles = {window.book.tab(tab, "text") for tab in window.book.tabs()}
        self.assertEqual(
            titles,
            {
                "基础",
                "个性化",
                "模型",
                "角色",
                "人格",
                "记忆",
                "技能",
                "语音",
                "主动行为",
                "权限",
                "开发者",
            },
        )

    def test_settings_window_is_not_stacked_twice(self):
        pet, _, _ = self.build()
        first = pet.open_settings()
        self.addCleanup(first.win.destroy)
        second = pet.open_settings()
        self.assertIs(first, second)

    def test_settings_window_wires_the_existing_preferences(self):
        pet, _, memory = self.build()
        window = pet.open_settings()
        self.addCleanup(window.win.destroy)
        self.assertIsNotNone(pet.proactive_var)
        self.assertIsNotNone(pet.quiet_var)
        self.assertIsNotNone(pet.quiet_entry)
        pet.proactive_var.set(False)
        pet._save_proactive()
        self.assertEqual(memory.get_preference("proactive_enabled"), "0")
        pet.quiet_entry.delete(0, tk.END)
        pet.quiet_entry.insert(0, "22-8")
        pet._save_quiet_hours()
        self.assertEqual(memory.get_preference("quiet_hours"), "22-8")

    def test_memory_tab_shows_real_values_not_errors(self):
        pet, _, _ = self.build()
        window = pet.open_settings()
        self.addCleanup(window.win.destroy)
        frame = None
        for tab in window.book.tabs():
            if window.book.tab(tab, "text") == "记忆":
                frame = window.book.nametowidget(tab)
        self.assertIsNotNone(frame)
        shown = " ".join(label_texts(frame))
        self.assertIn("数据位置", shown)
        self.assertIn("total_tokens", shown)
        self.assertNotIn("读取失败", shown)

    def test_model_test_reports_status_and_never_crashes(self):
        pet, _, _ = self.build()
        window = pet.open_settings()
        self.addCleanup(window.win.destroy)
        self.assertTrue(pet.test_model("companion"))
        self.assertIn("已连接", str(pet.settings_hint.cget("text")))
        self.assertFalse(pet.test_model("nonsense"))
        self.assertIn("没有", str(pet.settings_hint.cget("text")))

    def test_hint_writes_to_the_settings_line(self):
        pet, _, _ = self.build()
        window = pet.open_settings()
        self.addCleanup(window.win.destroy)
        pet._hint("已保存")
        self.assertEqual(str(pet.settings_hint.cget("text")), "已保存")


class StaticSafetyTests(unittest.TestCase):
    """界面只负责显示，不许绕过程序层。"""

    def read(self, relative):
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_agent_core_has_no_ui_widgets(self):
        text = self.read("agent/core.py").lower()
        for word in ("tkinter", "messagebox", "transcript", "chat_view", "desktop"):
            self.assertNotIn(word, text)

    def test_ui_never_shells_out_or_evals(self):
        for relative in UI_FILES:
            text = self.read(relative)
            for needle in BANNED:
                self.assertNotIn(needle, text, f"{needle} 出现在 {relative}")

    def test_ui_does_not_talk_to_a_model_vendor(self):
        for relative in UI_FILES:
            text = self.read(relative).lower()
            for word in ("openai", "deepseek", "qwen", "anthropic"):
                self.assertNotIn(word, text)

    def test_desktop_only_calls_the_agent_not_a_provider(self):
        text = self.read("ui/desktop.py")
        self.assertIn("agent.respond", text)
        self.assertNotIn("provider.chat(", text)


class HelperApiTests(unittest.TestCase):
    """老入口不能被界面重构弄丢。"""

    def test_status_helper_still_works(self):
        text = format_status("happy", {"total_tokens": 42}, None, "test-model")
        self.assertIn("开心", text)
        self.assertIn("42", text)

    def test_debug_helper_still_works(self):
        self.assertEqual(format_debug({}), "")
        self.assertIn("emotion: happy", format_debug({"emotion": "happy"}))

    def test_chat_state_constants_are_stable(self):
        self.assertEqual((BUSY, OK, WARN, ERROR), ("busy", "ok", "warn", "error"))

    def test_transparent_host_key_is_unchanged(self):
        self.assertEqual(TRANSPARENT_KEY, "#010203")


if __name__ == "__main__":
    unittest.main()
