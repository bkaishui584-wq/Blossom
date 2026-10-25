"""阶段 A（V6）验收：聊天工作台的布局、输入区与空状态。

测的是真实行为：窗口矮的时候输入框还在不在、任务面板是不是在输入框上面、
Shift+Enter 真的换行、问候语什么时候收起来。用临时数据库，不碰用户数据。
"""

import re
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
from agent.core import AgentCore
from database.db import Database
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatDelta, ChatResult, ModelProvider
from persona.interface import DefaultPersonaProvider
from security.config import Config
from ui import theme
from ui.chat_view import ChatInput, ChatView
from ui.desktop import DesktopPet


class StubProvider(ModelProvider):
    name = "stub"

    def __init__(self, reply="在的。"):
        self.reply = reply
        self.calls = 0

    def is_available(self):
        return True

    def chat(self, messages, tools=None, temperature=0.7):
        self.calls += 1
        return ChatResult(ok=True, content=self.reply, model="stub")

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


class FakeEvent:
    """只带 state 的事件替身：state 的最低位是 Shift。"""

    def __init__(self, shift=False):
        self.state = 0x0001 if shift else 0


class TkCase(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        self.root.withdraw()
        self.addCleanup(self._destroy)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def make_view(self, size=None, **kw):
        parent = tk.Toplevel(self.root)
        if size:
            parent.geometry("%dx%d+0+0" % size)
        view = ChatView(parent, **kw)
        parent.update_idletasks()
        return parent, view

    def settle(self, widget, rounds=6):
        for _ in range(rounds):
            try:
                widget.update_idletasks()
                self.root.update()
            except tk.TclError:
                return
            time.sleep(0.02)

    def build_pet(self):
        database = Database(Path(self.tmp.name) / "t.sqlite3")
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        agent = AgentCore(
            StubProvider(), memory, ContextManager(token_budget=600),
            persona=DefaultPersonaProvider(),
        )
        pet = DesktopPet(agent, Config(load_env=False), memory=memory, root=self.root)
        self.addCleanup(pet.close)
        return pet


class ComposerLayoutTests(TkCase):
    """输入区必须一直在，而且永远在消息区下面。"""

    def test_composer_survives_a_short_window(self):
        parent, view = self.make_view(size=(320, 360))
        view.append("me", "你：写一段很长的东西试试窗口会不会被挤爆")
        view.set_status("人格：芳菲 · 模型：stub · 网络：在线 · 本地工具：12")
        view.set_task("正在读取项目结构 … 3/8 步")
        view.set_state("busy", "思考中……")
        self.settle(parent)
        for widget in (view.composer, view.field, view.input, view.send_button, view.more_button):
            self.assertEqual(widget.winfo_ismapped(), 1, widget)
            self.assertGreater(widget.winfo_height(), 1, widget)
        # 工具收进了「＋」，展开以后也得在窄窗口里站得住。
        view.toggle_tools()
        self.settle(parent)
        for widget in (view.tool_box, view.attach_button, view.skill_button):
            self.assertEqual(widget.winfo_ismapped(), 1, widget)
            self.assertGreater(widget.winfo_height(), 1, widget)

    def test_task_panel_sits_above_the_composer(self):
        parent, view = self.make_view(size=(400, 520))
        view.set_task("正在读取项目结构 … 3/8 步")
        self.settle(parent)
        self.assertEqual(view.task_box.winfo_ismapped(), 1)
        self.assertLess(view.task_box.winfo_y(), view.composer.winfo_y())
        self.assertLess(view.state.winfo_y(), view.composer.winfo_y())
        view.set_task("", active=False)
        self.settle(parent)
        self.assertEqual(view.task_box.winfo_manager(), "")

    def test_message_area_is_above_the_task_panel(self):
        parent, view = self.make_view(size=(400, 520))
        view.set_task("跑第 2 步")
        self.settle(parent)
        self.assertLess(view.wrap.winfo_y(), view.task_box.winfo_y())

    def test_tall_window_gives_the_extra_room_to_messages(self):
        short, short_view = self.make_view(size=(400, 400))
        self.settle(short)
        tall, tall_view = self.make_view(size=(400, 760))
        self.settle(tall)
        self.assertGreater(tall_view.wrap.winfo_height(), short_view.wrap.winfo_height())
        self.assertEqual(tall_view.composer.winfo_height(), short_view.composer.winfo_height())


class InputTests(TkCase):
    """输入框：多行、Enter 发送、Shift+Enter 换行、空的时候有提示。"""

    def test_shift_enter_does_not_send(self):
        sent = []
        parent, view = self.make_view(on_send=lambda: sent.append(1))
        result = view.input._return(FakeEvent(shift=True))
        self.assertIsNone(result)
        self.assertEqual(sent, [])

    def test_enter_sends_and_stops_the_newline(self):
        sent = []
        parent, view = self.make_view(on_send=lambda: sent.append(1))
        view.input.insert(0, "在吗")
        result = view.input._return(FakeEvent(shift=False))
        self.assertEqual(result, "break")
        self.assertEqual(sent, [1])

    def test_enter_on_an_empty_box_does_nothing(self):
        sent = []
        parent, view = self.make_view(on_send=lambda: sent.append(1))
        result = view.input._return(FakeEvent(shift=False))
        self.assertEqual(result, "break")
        self.assertEqual(sent, [])

    def test_shift_return_is_bound(self):
        parent, view = self.make_view()
        self.assertTrue(view.input.bind("<Shift-Return>"))

    def test_the_old_附_press_keys_are_gone(self):
        parent, view = self.make_view()
        self.assertEqual(str(view.attach_button.cget("text")), "附件")
        self.assertEqual(str(view.skill_button.cget("text")), "技能")
        for widget in (view.attach_button, view.skill_button, view.send_button):
            self.assertTrue(widget.bind("<Enter>"), "缺少悬停说明")

    def test_empty_input_shows_a_hint_and_reads_as_empty(self):
        parent, view = self.make_view()
        self.assertTrue(view.input.has_placeholder())
        self.assertEqual(view.read_input(), "")
        self.assertIn("发送", view.input.placeholder)

    def test_typing_clears_the_hint(self):
        parent, view = self.make_view()
        view.input.insert(0, "  在吗  ")
        self.assertFalse(view.input.has_placeholder())
        self.assertEqual(view.read_input(), "在吗")

    def test_clear_input_puts_the_hint_back(self):
        parent, view = self.make_view()
        view.input.insert(0, "在吗")
        view.clear_input()
        self.assertEqual(view.read_input(), "")
        self.assertTrue(view.input.has_placeholder())

    def test_input_grows_with_lines_and_stops_at_the_cap(self):
        parent, view = self.make_view()
        view.input.clear_placeholder()
        view.input.insert("1.0", "第一行")
        view.input.insert("end", "\n第二行")
        view.input._sync_height()
        self.assertGreaterEqual(int(view.input.cget("height")), 2)
        view.input.delete(0, tk.END)
        view.input.insert("1.0", "\n".join("第%d行" % index for index in range(20)))
        view.input._sync_height()
        self.assertEqual(int(view.input.cget("height")), ChatInput.MAX_LINES)

    def test_hint_always_fits_the_field(self):
        parent, view = self.make_view(size=(300, 400))
        self.settle(parent)
        view.input.fit_placeholder()
        from tkinter import font as tkfont

        measured = tkfont.Font(font=view.input.cget("font")).measure(view.input.placeholder)
        self.assertLessEqual(measured, view.input.winfo_width())

    def test_paste_of_long_text_is_read_whole(self):
        parent, view = self.make_view()
        body = "第一行\n第二行\n第三行"
        view.input.insert(0, body)
        self.assertIn("第三行", view.read_input())


class EmptyStateTests(TkCase):
    """空会话的问候：真的聊起来才收，提示和通知不算。"""

    def test_greeting_visible_on_a_fresh_chat(self):
        parent, view = self.make_view()
        self.assertTrue(view.is_empty_shown())
        self.assertTrue(str(view.empty_text.cget("text")).strip())

    def test_hint_and_notice_do_not_dismiss_the_greeting(self):
        parent, view = self.make_view()
        view.append("hint", "提示：随便说点什么。")
        view.append("notice", "尚未配置模型：本地功能仍然可用。")
        self.assertTrue(view.is_empty_shown())

    def test_first_real_message_dismisses_the_greeting(self):
        parent, view = self.make_view()
        view.append("me", "你：在吗")
        self.assertFalse(view.is_empty_shown())

    def test_markdown_reply_also_dismisses_it(self):
        parent, view = self.make_view()
        fence = chr(96) * 3
        view.append_markdown("pet", "桌宠：看这个\n%s\ncode\n%s" % (fence, fence))
        self.assertFalse(view.is_empty_shown())

    def test_clear_brings_the_greeting_back(self):
        parent, view = self.make_view()
        view.append("me", "你：在吗")
        view.clear()
        self.assertTrue(view.is_empty_shown())

    def test_greeting_can_be_replaced(self):
        parent, view = self.make_view()
        view.set_greeting("我是测试桌宠。")
        self.assertEqual(str(view.empty_text.cget("text")), "我是测试桌宠。")
        view.set_greeting("   ")
        self.assertEqual(str(view.empty_text.cget("text")), ChatView.DEFAULT_GREETING)


class SidebarTests(TkCase):
    """抽屉：窗口宽就占一列，窄就浮在中央区上，收起时不占地方。

    定稿之后左边常驻一条导航，抽屉是叠在中央区上的，所以中央区的 x 起点
    是导航宽度而不是 0。
    """

    def test_hidden_by_default(self):
        parent, view = self.make_view(size=(400, 520))
        self.settle(parent)
        self.assertFalse(view.sidebar_visible)
        self.assertEqual(view.sidebar_mode(), "hidden")
        self.assertEqual(view.side.winfo_manager(), "")

    def test_wide_window_pins_the_sidebar(self):
        parent, view = self.make_view(size=(1100, 700))
        view.show_sidebar(True)
        self.settle(parent)
        self.assertEqual(view.sidebar_mode(), "pinned")
        self.assertEqual(view.side.winfo_manager(), "pack")
        self.assertGreater(view.main.winfo_x(), 0)

    def test_narrow_window_floats_the_sidebar(self):
        parent, view = self.make_view(size=(380, 520))
        view.show_sidebar(True)
        self.settle(parent)
        self.assertEqual(view.sidebar_mode(), "overlay")
        self.assertEqual(view.side.winfo_manager(), "place")
        # 导航和中央区之间留一条缝，中央区不再因为导航展开而重新排版。
        self.assertGreaterEqual(view.main.winfo_x(), view.nav.winfo_width())
        self.assertLessEqual(
            view.main.winfo_x(), view.nav.winfo_width() + theme.PAD_MD
        )

    def test_toggle_reports_the_state_back(self):
        seen = []
        parent, view = self.make_view(size=(640, 520), on_toggle_sidebar=seen.append)
        view.toggle_sidebar()
        self.assertEqual(seen, [True])
        view.toggle_sidebar()
        self.assertEqual(seen, [True, False])


class TooltipTests(TkCase):
    """悬停说明：鼠标停上去才出现，移开就收。"""

    @staticmethod
    def _tips(widget, found=None):
        found = [] if found is None else found
        for child in widget.winfo_children():
            if isinstance(child, tk.Toplevel):
                found.append(child)
            TooltipTests._tips(child, found)
        return found

    def test_hover_shows_a_note_and_leaving_hides_it(self):
        parent, view = self.make_view(size=(400, 520))
        view.toggle_tools()
        self.settle(parent)
        view.attach_button.event_generate("<Enter>")
        limit = time.time() + 3
        while time.time() < limit and not self._tips(parent):
            parent.update()
            time.sleep(0.05)
        tips = self._tips(parent)
        self.assertEqual(len(tips), 1)
        labels = [str(c.cget("text")) for c in tips[0].winfo_children() if isinstance(c, tk.Label)]
        self.assertIn("路径", "".join(labels))
        view.attach_button.event_generate("<Leave>")
        limit = time.time() + 2
        while time.time() < limit and self._tips(parent):
            parent.update()
            time.sleep(0.05)
        self.assertEqual(self._tips(parent), [])


class TimestampTests(TkCase):
    """消息带一个小时间戳，但不影响正文的可搜索性。"""

    def test_a_time_appears_on_a_message(self):
        parent, view = self.make_view()
        view.append_markdown("me", "你：在吗")
        text = view.history_text()
        self.assertIn("你：在吗", text)
        self.assertRegex(text, r"\d\d:\d\d")

    def test_other_roles_get_no_stamp(self):
        parent, view = self.make_view()
        view.append("hint", "提示：随便说点什么。")
        self.assertNotRegex(view.history_text().strip(), r"\d\d:\d\d")

    def test_timestamps_can_be_turned_off(self):
        parent, view = self.make_view(timestamps=False)
        view.append_markdown("me", "你：在吗")
        self.assertNotRegex(view.history_text().strip(), r"\d\d:\d\d")


class ThemeTests(TkCase):
    """换主题只改颜色，不丢控件、不换掉输入区的位置。"""

    def test_switching_theme_keeps_the_field_and_task_colors(self):
        parent, view = self.make_view()
        view.set_task("跑第 2 步")
        for name in ("light", "dark"):
            theme.apply(name)
            try:
                view.apply_theme()
                self.assertEqual(str(view.field.cget("bg")), theme.FIELD)
                self.assertEqual(str(view.task_box.cget("bg")), theme.BUBBLE_SYS)
                self.assertEqual(str(view.input.cget("bg")), theme.FIELD)
                # 首屏空着的时候消息区融进主背景，免得留一块空框。
                self.assertTrue(view.is_empty_shown())
                self.assertEqual(str(view.transcript.cget("bg")), theme.BG)
            finally:
                theme.apply("dark")
        # 有了消息才回到输入区底色，气泡两侧不留接缝。
        view.apply_theme()
        view.append("pet", "在。")
        self.assertFalse(view.is_empty_shown())
        self.assertEqual(str(view.transcript.cget("bg")), theme.FIELD)

    def test_switching_theme_keeps_the_composer_where_it_was(self):
        parent, view = self.make_view(size=(400, 520))
        self.settle(parent)
        before = (view.composer.winfo_x(), view.composer.winfo_y())
        view.apply_theme()
        self.settle(parent)
        self.assertEqual((view.composer.winfo_x(), view.composer.winfo_y()), before)


class HostWiringTests(TkCase):
    """宿主这一侧：问候语跟人格走，输入区照旧能收发。"""

    def test_greeting_uses_the_persona_name(self):
        pet = self.build_pet()
        self.assertIn("小蝶", str(pet.chat.empty_text.cget("text")))

    def test_default_greeting_when_no_persona(self):
        pet = self.build_pet()
        pet.agent.persona = None
        self.assertEqual(pet._greeting_text(), ChatView.DEFAULT_GREETING)

    def test_send_flow_still_works_from_the_new_input(self):
        pet = self.build_pet()
        pet.chat.input.insert(0, "在吗")
        pet._send()
        limit = time.time() + 5
        while "在的。" not in pet.chat.history_text() and time.time() < limit:
            pet._drain()
            self.root.update()
            time.sleep(0.02)
        self.assertIn("你：在吗", pet.chat.history_text())
        self.assertIn("桌宠：在的。", pet.chat.history_text())
        self.assertEqual(pet.chat.read_input(), "")
        self.assertFalse(pet.chat.is_empty_shown())

    def test_retry_resends_the_text_without_the_hint(self):
        pet = self.build_pet()
        pet.chat.input.insert(0, "在吗")
        pet._send()
        limit = time.time() + 5
        while "在的。" not in pet.chat.history_text() and time.time() < limit:
            pet._drain()
            self.root.update()
            time.sleep(0.02)
        pet._retry()
        limit = time.time() + 5
        while pet.chat.history_text().count("你：在吗") < 2 and time.time() < limit:
            pet._drain()
            self.root.update()
            time.sleep(0.02)
        text = pet.chat.history_text()
        self.assertEqual(text.count("你：在吗"), 2)
        self.assertNotIn("说点什么", text)
        self.assertTrue(pet.chat.input.has_placeholder())


class NoModelTests(TkCase):
    """没有模型也要能启动，输入框和问候语照常。"""

    def test_starts_without_any_model_configured(self):
        database = Database(Path(self.tmp.name) / "n.sqlite3")
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        agent = AgentCore(
            StubProvider(), memory, ContextManager(token_budget=400),
            persona=DefaultPersonaProvider(),
        )
        pet = DesktopPet(agent, Config(load_env=False), memory=memory, root=self.root)
        self.addCleanup(pet.close)
        self.assertIn("尚未配置模型", pet.chat.history_text())
        self.assertTrue(pet.chat.is_empty_shown())
        self.assertTrue(pet.chat.input.has_placeholder())


if __name__ == "__main__":
    unittest.main()
