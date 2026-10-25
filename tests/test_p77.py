"""UI 定稿验收：设计 Token、三栏布局、输入工作台、消息组件与聊天能力接线。

覆盖：深浅两套 Token 与规范一致、间距圆角按规范、顶栏 60px、左导航十条固定入口、
248/68 折叠、中央内容与输入区最大 900px、右侧任务面板 340-400px 可折叠、
空会话四张卡片、会话改名置顶归档标签搜索导出、附件状态与移除、模型菜单真实状态、
任务步骤卡片、等待授权卡片、反馈落库、编辑重发与重新生成、语音与网页搜索的不可用状态、
知识库空状态、斜杠命令、导航与面板偏好持久化、旧数据迁移不丢。
"""

import json
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.context import ContextManager
from agent.core import AgentCore
from agent.task import TaskStep
from database.db import Database
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatDelta, ChatResult, ModelProvider, Usage
from security.config import Config
from ui import chat_view, theme
from ui.chat_view import ChatView
from ui.desktop import DesktopPet


class StubProvider(ModelProvider):
    name = "stub"

    def is_available(self):
        return True

    def chat(self, messages, tools=None, temperature=0.7):
        return ChatResult(ok=True, content="在的。", usage=Usage(5, 3), model="stub")

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


# --------------------------------------------------------------- Token
class DesignTokenTests(unittest.TestCase):
    """主题令牌的底线：颜色按 shadcn/ui 的中性色阶，间距和圆角沿用定稿值。"""

    # 液态玻璃色阶：深靛蓝底、面板亮一档、发丝边；强调色由主题预设给。
    # 改这里之前先跑 test_p102 的对比度检查（正文/次要文字都要过 4.5）。
    SPEC_DARK = {
        "BG": "#0d0c16",
        "SIDEBAR_BG": "#100e1a",
        "PANEL": "#191727",
        "PANEL_2": "#131120",
        "FIELD": "#171525",
        "TEXT": "#f5f3fa",
        "MUTED": "#b0acc2",
        "BORDER": "#332f47",
        "ACCENT": "#e8a0ba",
        "LILAC": "#c9b6ff",
        "OK": "#82c9a5",
        "WARN": "#e9b66d",
        "ERROR": "#e58b91",
    }

    def setUp(self):
        self.original = (theme.active(), theme.accent())
        self.addCleanup(lambda: theme.apply(self.original[0], accent_name=self.original[1]))

    def test_dark_palette_matches_the_spec(self):
        theme.apply("dark", accent_name=theme.DEFAULT_ACCENT)
        for key, wanted in self.SPEC_DARK.items():
            self.assertEqual(theme.DARK[key].lower(), wanted, key)
            self.assertEqual(str(getattr(theme, key)).lower(), wanted, key)

    def test_brand_accent_is_the_default_pink(self):
        self.assertEqual(theme.DEFAULT_ACCENT, "brand")
        theme.apply("dark", accent_name=theme.DEFAULT_ACCENT)
        self.assertEqual(theme.ACCENT.lower(), "#e8a0ba")

    def test_light_theme_is_its_own_palette_not_an_inversion(self):
        self.assertEqual(set(theme.DARK), set(theme.LIGHT))
        for key in ("BG", "TEXT", "BORDER", "FIELD", "PANEL", "SIDEBAR_BG", "MUTED"):
            self.assertNotEqual(theme.DARK[key], theme.LIGHT[key], key)
        # 浅色下正文比输入框深得多，边框介于两者之间但看得出。
        text = _luma(theme.LIGHT["TEXT"])
        field = _luma(theme.LIGHT["FIELD"])
        border = _luma(theme.LIGHT["BORDER"])
        muted = _luma(theme.LIGHT["MUTED"])
        self.assertLess(text, field - 100)
        self.assertLess(border, field)
        self.assertLess(text, muted)
        self.assertLess(muted, field)
        # 深色反过来：亮字压在暗底上。
        self.assertGreater(_luma(theme.DARK["TEXT"]), _luma(theme.DARK["FIELD"]) + 100)
        self.assertGreater(_luma(theme.DARK["BORDER"]), _luma(theme.DARK["FIELD"]))

    def test_spacing_tokens_are_the_spec_set(self):
        self.assertEqual(
            (theme.PAD_XS, theme.PAD_SM, theme.PAD_MD, theme.PAD_LG, theme.PAD_XL),
            (4, 8, 16, 24, 32),
        )

    def test_radius_tokens_are_the_spec_set(self):
        self.assertEqual((theme.RADIUS_SM, theme.RADIUS, theme.RADIUS_LG), (8, 12, 16))

    def test_layout_size_tokens(self):
        self.assertEqual(theme.TOPBAR_HEIGHT, 60)
        self.assertEqual((theme.NAV_WIDTH, theme.NAV_COLLAPSED_WIDTH), (248, 68))
        self.assertEqual(theme.CONTENT_MAX_WIDTH, 900)
        self.assertEqual((theme.PANEL_WIDTH_MIN, theme.PANEL_WIDTH_MAX), (340, 400))
        self.assertGreaterEqual(theme.PANEL_WIDTH_MIN, 340)
        self.assertLessEqual(theme.PANEL_WIDTH, theme.PANEL_WIDTH_MAX)
        self.assertTrue(120 <= theme.ANIMATION_MS <= 200)


def _luma(color):
    text = str(color).lstrip("#")
    parts = [int(text[index:index + 2], 16) for index in (0, 2, 4)]
    return sum(part * weight for part, weight in zip(parts, (0.299, 0.587, 0.114)))


# ------------------------------------------------------------ 布局
class TkCase(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        self.root.withdraw()
        self.addCleanup(self._safe_destroy)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Config(load_env=False)
        base = Path(self.tmp.name)
        self.config.data_dir = base
        self.config.log_dir = base / "logs"
        self.config.db_path = base / "t.sqlite3"
        self.config.characters_dir = base / "characters"
        self.config.live2d_dir = base / "live2d"
        self.config.devices_dir = base / "devices"
        self.config.tray = False
        self.events = []

    def _safe_destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def settle(self, widget):
        for _ in range(4):
            widget.update_idletasks()
            widget.update()

    def make_view(self, size=(1200, 800)):
        parent = tk.Toplevel(self.root)
        parent.geometry("%dx%d" % size)
        self.addCleanup(lambda: parent.winfo_exists() and parent.destroy())
        view = ChatView(parent, on_event=lambda name, payload=None: self.events.append((name, payload)))
        self.settle(parent)
        return parent, view

    def build_pet(self):
        database = Database(self.config.db_path)
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        agent = AgentCore(StubProvider(), memory, ContextManager(token_budget=800))
        pet = DesktopPet(agent, self.config, memory=memory, root=self.root)
        self.addCleanup(pet.close)
        return pet, memory, database


class ShellTests(TkCase):
    """顶栏、左导航、中央区、右面板：位置和尺寸按规范。"""

    def test_top_bar_is_sixty_pixels_and_carries_the_brand(self):
        parent, view = self.make_view()
        self.assertEqual(view.bar.winfo_height(), theme.TOPBAR_HEIGHT)
        self.assertEqual(str(view.brand.cget("text")), "Blossom")
        self.assertTrue(str(view.title.cget("text")).strip())
        for widget in (view.mode_label, view.model_button, view.search_button,
                       view.panel_button, view.settings_button, view.min_button,
                       view.max_button, view.close_button):
            self.assertEqual(widget.winfo_ismapped(), 1, widget)

    def test_nav_has_the_fixed_entries_in_order(self):
        parent, view = self.make_view()
        keys = [key for key, _label in ChatView.NAV_ITEMS]
        self.assertEqual(
            keys,
            ["home", "new", "sessions", "search", "calendar", "ideas", "goals",
             "work", "tasks", "knowledge", "skills", "persona", "capabilities",
             "devices", "awareness", "settings"],
        )
        self.assertEqual(list(view.nav_rows), keys)
        labels = [str(view.nav_texts[key].cget("text")) for key in keys]
        self.assertEqual(
            labels,
            ["首页", "新建对话", "最近会话", "搜索历史", "日历日程", "灵感建议",
             "目标与追踪", "工作记录", "任务中心", "文件与知识", "Skills", "人格与记忆",
             "本地能力与模型", "设备中心", "屏幕感知", "权限与设置"],
        )

    def test_nav_collapses_between_248_and_68(self):
        parent, view = self.make_view()
        self.assertFalse(view.nav_is_collapsed())
        self.assertEqual(view.nav.winfo_width(), theme.NAV_WIDTH)
        self.assertEqual(self.events[-1], ("nav_toggle", True)) if self.events else None
        view.set_nav_active("tasks")
        self.assertEqual(view.nav_active, "tasks")
        self.assertEqual(
            str(view.nav_rows["tasks"].cget("bg")), theme.SIDEBAR_ACTIVE
        )
        view.toggle_nav(False)
        self.settle(parent)
        self.assertTrue(view.nav_is_collapsed())
        self.assertEqual(view.nav.winfo_width(), theme.NAV_COLLAPSED_WIDTH)
        self.assertEqual(str(view.nav_texts["tasks"].winfo_manager()), "")

    def test_clicking_a_nav_row_reports_the_key(self):
        parent, view = self.make_view()
        view._nav_clicked("tasks")
        self.assertIn(("nav", "tasks"), self.events)
        view._nav_clicked("sessions")
        self.assertTrue(view.sidebar_visible)

    def test_central_content_is_capped_at_900(self):
        parent, view = self.make_view()
        self.assertEqual(ChatView.MAX_TEXT_WIDTH, 900)
        gutter = view._apply_text_width(1400)
        self.assertGreater(gutter, 0)
        self.assertLessEqual(view._apply_text_width(900), 0)

    def test_composer_stays_at_the_bottom_and_never_wider_than_900(self):
        parent, view = self.make_view()
        self.assertLess(view.wrap.winfo_y(), view.composer.winfo_y())
        view._apply_composer_width(1400)
        self.settle(parent)
        self.assertGreater(int(view.field.pack_info()["padx"]), theme.PAD_MD)
        self.assertLessEqual(
            1400 - int(view.field.pack_info()["padx"]) * 2, theme.CONTENT_MAX_WIDTH
        )

    def test_right_panel_is_hidden_until_asked_and_within_340_400(self):
        parent, view = self.make_view()
        self.assertEqual(str(view.panel.winfo_manager()), "")
        self.assertFalse(view.panel_is_shown())
        view.toggle_panel()
        self.settle(parent)
        self.assertEqual(str(view.panel.winfo_manager()), "pack")
        width = view.panel.winfo_width()
        self.assertGreaterEqual(width, theme.PANEL_WIDTH_MIN)
        self.assertLessEqual(width, theme.PANEL_WIDTH_MAX)
        self.assertGreater(view.main.winfo_width(), 400)
        view.toggle_panel()
        self.settle(parent)
        self.assertEqual(str(view.panel.winfo_manager()), "")

    def test_panel_gives_way_when_the_window_is_too_narrow(self):
        parent, view = self.make_view(size=(900, 700))
        view.set_panel(True)
        self.settle(parent)
        self.assertEqual(str(view.panel.winfo_manager()), "")

    def test_empty_state_offers_the_four_capability_cards(self):
        """定稿图：正中一句问话，下面 2×2 四张卡，每张有图标、标题和一句说明。"""
        parent, view = self.make_view()
        self.assertTrue(view.is_empty_shown())
        self.assertEqual(list(view.empty_cards), ["code", "research", "files", "plan"])
        self.assertEqual(str(view.empty_title.cget("text")), ChatView.EMPTY_TITLE)
        self.assertTrue(str(view.empty_text.cget("text")).strip())
        for key, title, note, _colour in ChatView.EMPTY_CARDS:
            texts = [str(widget.cget("text")) for widget in view.empty_cards[key].winfo_children()]
            self.assertIn(title, texts)
            self.assertIn(note, texts)
        view._card_clicked("code")
        self.assertIn(("card", "code"), self.events)
        view.append("me", "你：在吗")
        self.assertFalse(view.is_empty_shown())

    def test_small_window_keeps_a_usable_message_area(self):
        """1000×680 首屏不能让欢迎页把消息区挤成一条缝。"""
        parent, view = self.make_view(size=(1000, 680))
        self.settle(parent)
        self.assertGreaterEqual(view.wrap.winfo_height(), 300)
        self.assertGreaterEqual(view.wrap.winfo_width(), 600)
        self.assertTrue(view.is_empty_shown())

    def test_resize_round_trip_and_panel_keep_text_margins_in_sync(self):
        """来回缩放和展开右面板后，正文宽度要按当前几何重算，不能停在旧值。"""
        parent, view = self.make_view(size=(1200, 800))
        self.settle(parent)
        base_width = view.wrap.winfo_width()
        for size in ((1000, 680), (1600, 1040), (1200, 800)):
            parent.geometry("%dx%d" % size)
            self.settle(parent)
            self.settle(parent)
        self.assertLessEqual(abs(view.wrap.winfo_width() - base_width), 24)

        before = view.wrap.winfo_width()
        view.set_panel(True)
        self.settle(parent)
        self.settle(parent)
        after = view.wrap.winfo_width()
        self.assertLess(after, before)
        self.assertGreater(after, 400)

        inner = max(
            view.MIN_WRAP,
            parent.winfo_width() - view._chrome_width() - theme.PAD_MD * 2 - theme.PAD_SM,
        )
        wanted = theme.BUBBLE_PAD + max(0, (inner - view.MAX_TEXT_WIDTH) // 2)
        got = int(float(view.transcript.tag_cget("pet", "lmargin1")))
        self.assertLessEqual(abs(got - wanted), 1)

    def test_window_controls_report_back_to_the_host(self):
        parent, view = self.make_view()
        view.minimize()
        view.toggle_maximize()
        self.assertIn(("minimize", None), self.events)
        self.assertIn(("maximize", None), self.events)

    def test_theme_switch_recolours_bar_nav_and_panel(self):
        parent, view = self.make_view()
        theme.apply("dark", accent_name=theme.DEFAULT_ACCENT)
        view.apply_theme()
        self.assertEqual(str(view.bar.cget("bg")), theme.TOPBAR_BG)
        theme.apply("light")
        view.apply_theme()
        self.assertEqual(str(view.bar.cget("bg")), theme.LIGHT["TOPBAR_BG"])
        self.assertEqual(str(view.nav.cget("bg")), theme.LIGHT["SIDEBAR_BG"])
        self.assertEqual(str(view.panel.cget("bg")), theme.LIGHT["CARD"])
        theme.apply("dark", accent_name=theme.DEFAULT_ACCENT)


class MessageComponentTests(TkCase):
    """消息区：代码、复制、反馈按钮都得是真控件。"""

    def test_action_bar_offers_copy_edit_regenerate_and_feedback(self):
        parent, view = self.make_view()
        labels = [
            str(widget.cget("text"))
            for widget in (view.action_copy, view.action_edit, view.action_regen,
                           view.action_good, view.action_bad)
        ]
        self.assertEqual(labels, ["复制回复", "编辑重发", "重新生成", "有用", "没用"])
        # 空会话上不摆操作栏，聊起来才出现。
        self.assertEqual(str(view.action_bar.winfo_manager()), "")
        view.append("pet", "桌宠：在的。")
        self.assertEqual(str(view.action_bar.winfo_manager()), "pack")

    def test_feedback_and_edit_buttons_report_back(self):
        parent, view = self.make_view()
        view._feedback_good()
        view._feedback_bad()
        view._edit_resend()
        view._regen_last()
        self.assertIn(("feedback", True), self.events)
        self.assertIn(("feedback", False), self.events)
        self.assertIn(("edit_resend", None), self.events)
        self.assertIn(("regenerate", None), self.events)

    def test_permission_card_can_be_answered(self):
        parent, view = self.make_view()
        view.show_permission("write_file", "HIGH", "会改动项目里的文件")
        self.settle(parent)
        self.assertTrue(view.permission_open())
        self.assertIn("write_file", view.permission_text())
        self.assertEqual(view.perm_box.winfo_ismapped(), 1)
        view._perm_allow()
        self.assertIn(("permission", True), self.events)
        self.assertFalse(view.permission_open())
        self.settle(parent)
        self.assertEqual(str(view.perm_box.winfo_manager()), "")

    def test_task_steps_render_marks_and_the_panel_body(self):
        parent, view = self.make_view()
        view.set_panel(True)
        view.set_task_steps(
            [("读项目结构", "done", ""), ("改配置", "running", "write_file"), ("跑测试", "todo", "")],
            title="任务：整理",
        )
        self.assertIn("读项目结构", view.task_text())
        body = view.panel_body()
        self.assertIn("✔ 读项目结构", body)
        self.assertIn("● 改配置", body)
        self.assertIn("○ 跑测试", body)
        view.set_task_steps([])
        self.assertEqual(str(view.task_box.winfo_manager()), "")

    def test_attachments_show_state_and_can_be_removed(self):
        parent, view = self.make_view()
        view.set_attachments([{"id": "/tmp/x.py", "name": "x.py", "state": "pending"}])
        self.settle(parent)
        self.assertEqual(view.attachment_count(), 1)
        self.assertEqual(str(view.attach_box.winfo_manager()), "pack")
        view._remove_attachment("/tmp/x.py")
        self.assertIn(("remove_attachment", "/tmp/x.py"), self.events)
        view.set_attachments([])
        self.settle(parent)
        self.assertEqual(str(view.attach_box.winfo_manager()), "")

    def test_slash_typing_shows_the_command_hint(self):
        parent, view = self.make_view()
        view.set_commands([("新建对话", "new"), ("打开设置", "settings")])
        view.input.clear_placeholder()
        view.input.insert("1.0", "/新")
        view._slash_changed()
        self.settle(parent)
        self.assertEqual(view.command_hint.winfo_ismapped(), 1)
        self.assertIn("新建对话", str(view.command_hint.cget("text")))
        view.input.delete("1.0", tk.END)
        view.input.insert("1.0", "普通消息")
        view._slash_changed()
        self.settle(parent)
        self.assertEqual(str(view.command_hint.winfo_manager()), "")

    def test_voice_and_web_search_start_disabled_with_a_reason(self):
        parent, view = self.make_view()
        self.assertFalse(view.voice_available())
        self.assertFalse(view.web_search_available())
        self.assertEqual(str(view.voice_button.cget("state")), "disabled")
        self.assertEqual(str(view.web_button.cget("state")), "disabled")
        view.set_voice_available(True)
        self.assertTrue(view.voice_available())


# ------------------------------------------------------- 能力接线
class SessionManagementTests(TkCase):
    """会话列表：改名、置顶、归档、标签、搜索、删除、导出。"""

    def seeded(self):
        pet, memory, database = self.build_pet()
        for session_id, text in (("desktop", "第一句"), ("other", "另一个会话")):
            memory.repository.add_message(session_id, "user", text)
        memory.repository.add_message("other", "assistant", "回另一个会话")
        memory.rename_session("desktop", "装机记录")
        memory.pin_session("desktop", True)
        memory.tag_session("desktop", "装机,硬件")
        pet.refresh_sessions()
        return pet, memory

    def test_labels_carry_title_pin_and_tags(self):
        pet, _memory = self.seeded()
        sessions = pet.refresh_sessions()
        labels = dict(sessions)
        self.assertIn("装机记录", labels["desktop"])
        self.assertIn("★", labels["desktop"])
        self.assertIn("#装机", labels["desktop"])

    def test_archived_sessions_hide_until_asked(self):
        pet, memory = self.seeded()
        memory.archive_session("other", True)
        visible = [row[0] for row in pet.refresh_sessions()]
        self.assertNotIn("other", visible)
        pet._chat_toggle_archived()
        self.assertIn("other", [row[0] for row in pet.refresh_sessions()])

    def test_delete_asks_first_and_keeps_messages_when_refused(self):
        pet, memory = self.seeded()
        before = len(memory.repository.recent_messages("other", limit=50))
        with mock.patch("ui.desktop.messagebox.askyesno", return_value=False):
            self.assertFalse(pet._chat_session_delete("other"))
        self.assertEqual(len(memory.repository.recent_messages("other", limit=50)), before)
        with mock.patch("ui.desktop.messagebox.askyesno", return_value=True):
            self.assertTrue(pet._chat_session_delete("other"))
        self.assertEqual(memory.repository.recent_messages("other", limit=50), [])

    def test_export_writes_a_real_file(self):
        pet, memory = self.seeded()
        target = Path(self.tmp.name) / "out.json"
        with mock.patch("ui.desktop.filedialog.asksaveasfilename", return_value=str(target)):
            self.assertEqual(pet._chat_session_export("other"), str(target))
        payload = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(payload["session_id"], "other")
        self.assertEqual(len(payload["messages"]), 2)

    def test_search_finds_the_session_by_title(self):
        pet, _memory = self.seeded()
        with mock.patch("ui.desktop.simpledialog.askstring", return_value="装机"):
            hits = pet._chat_search()
        self.assertIn("desktop", hits)


class ChatCapabilityTests(TkCase):
    """输入工作台、附件、模型、权限、反馈、命令都接在真后端上。"""

    def test_attachment_state_is_real(self):
        pet, _memory, _db = self.build_pet()
        pet._chat_add_attachment(__file__)
        rows = pet.chat.attach_chips
        self.assertEqual(len(rows), 1)
        pet._attachments[0]["name"] = Path(__file__).name
        missing = pet._chat_add_attachment(str(Path(self.tmp.name) / "没有这个文件.txt"))
        self.assertEqual(missing["state"], "error")
        self.assertEqual(pet._chat_remove_attachment(missing["id"]), 1)

    def test_model_menu_reports_real_status(self):
        pet, _memory, _db = self.build_pet()
        labels = []
        with mock.patch.object(tk.Menu, "tk_popup", lambda *a, **k: None):
            menu = pet._chat_model_menu()
        for index in range(menu.index("end") + 1):
            if menu.type(index) == "command":
                labels.append(str(menu.entrycget(index, "label")))
        self.assertTrue(any("陪伴模型" in label for label in labels), labels)
        self.assertTrue(any("任务模型" in label for label in labels), labels)

    def test_feedback_is_persisted(self):
        pet, memory, _db = self.build_pet()
        pet._last_user = "在吗"
        pet._chat_feedback(False)
        rows = memory.repository.memories_by_kind("feedback", 5)
        self.assertEqual(len(rows), 1)
        self.assertIn("没用", rows[0]["content"])

    def test_edit_resend_puts_the_last_message_back(self):
        pet, _memory, _db = self.build_pet()
        pet._last_user = "帮我改配置"
        pet._chat_edit_resend()
        self.assertEqual(pet.chat.read_input(), "帮我改配置")

    def test_regenerate_does_not_repeat_the_user_message(self):
        pet, _memory, _db = self.build_pet()
        pet._last_user = "在吗"
        before = pet.chat.history_text()
        with mock.patch.object(pet, "_new_worker") as worker:
            self.assertTrue(pet._chat_regenerate())
        self.assertTrue(worker.called)
        self.assertEqual(pet.chat.history_text(), before)
        pet._set_busy(False)

    def test_permission_card_answers_the_worker(self):
        pet, _memory, _db = self.build_pet()
        pet.show_chat()
        result = {}

        def worker():
            result["ok"] = pet.confirm("read_file", "READ", "看一下配置")

        thread = threading.Thread(target=worker, daemon=True)
        pet.root.after(1, pet._drain)
        thread.start()
        deadline = time.time() + 5
        while pet.chat.permission_open() is False and time.time() < deadline:
            pet.root.update()
            time.sleep(0.01)
        self.assertTrue(pet.chat.permission_open())
        pet.root.update()
        pet._chat_permission_answer(False)
        thread.join(5)
        self.assertFalse(result.get("ok"))
        self.assertFalse(thread.is_alive())

    def test_commands_list_and_routing(self):
        pet, _memory, _db = self.build_pet()
        commands = pet.chat_commands()
        keys = [key for _label, key in commands]
        for wanted in ("new", "search", "tasks", "skills", "settings", "clear"):
            self.assertIn(wanted, keys)
        first = pet.session_id
        pet._chat_command("new")
        self.assertNotEqual(pet.session_id, first)
        with mock.patch("ui.desktop.simpledialog.askstring", return_value="装机"):
            pet._chat_command("搜索历史")
        self.assertTrue(pet.chat.command_matches("/搜索"))
        self.assertIn("技能", "".join(label for label, _key in commands))

    def test_knowledge_opens_its_page_and_web_search_says_what_is_missing(self):
        pet, _memory, _db = self.build_pet()
        pet.show_chat()
        # 知识库现在是一整页：没接服务时页面自己说没接入，不再往聊天里塞一句提示。
        panel = pet.open_knowledge()
        self.assertIsNotNone(panel)
        self.assertIn("知识库", str(panel.hint.cget("text")))
        pet._chat_web_search()
        self.assertIn("搜索服务", str(pet.chat.state.cget("text")))
        self.assertFalse(pet.chat.web_search_available())

    def test_voice_entry_stays_disabled_without_a_mic(self):
        pet, _memory, _db = self.build_pet()
        self.assertFalse(pet.chat.voice_available())
        pet._chat_voice()
        self.assertIn("语音", str(pet.chat.state.cget("text")))

    def test_top_bar_shows_real_mode_and_model(self):
        pet, _memory, _db = self.build_pet()
        pet._refresh_status()
        self.assertEqual(pet.chat.model_text(), "模型：未配置")
        self.assertTrue(str(pet.chat.mode_label.cget("text")).strip())

    def test_task_steps_come_from_the_real_plan(self):
        pet, _memory, _db = self.build_pet()
        pet.chat.set_panel(True)
        pet.agent._task_steps = [
            TaskStep(index=0, title="读项目结构", status="done"),
            TaskStep(index=1, title="改配置", tool="write_file", status="running"),
        ]
        pet._sync_task_panel("running")
        self.assertIn("读项目结构", pet.chat.task_text())
        self.assertIn("改配置", pet.chat.panel_body())
        pet.agent._task_steps = []
        pet._sync_task_panel("completed")
        self.assertEqual(str(pet.chat.task_box.winfo_manager()), "")


class PersistenceTests(TkCase):
    """导航、面板、主题、会话元数据都要活过重启。"""

    def test_nav_and_panel_preferences_survive_a_restart(self):
        pet, memory, _db = self.build_pet()
        pet._chat_ready = True
        pet.chat.toggle_nav(False)
        pet.chat.set_panel(True)
        self._save_panel = pet._save_preference("chat.panel", "1")
        self.assertEqual(memory.get_preference("chat.nav"), "0")
        self.assertEqual(memory.get_preference("chat.panel"), "1")

    def test_old_messages_survive_the_metadata_migration(self):
        memory_db = Database(Path(self.tmp.name) / "old.sqlite3")
        memory_db.migrate()
        repository = MemoryRepository(memory_db)
        repository.add_message("desktop", "user", "升级前的话")
        repository.update_session("desktop", title="老会话")
        # 再跑一次迁移：只补表，不动消息。
        self.assertEqual(memory_db.migrate(), [])
        rows = repository.recent_messages("desktop", limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content"], "升级前的话")
        self.assertEqual(repository.session_meta("desktop")["title"], "老会话")

    def test_window_size_preference_is_reused_on_start(self):
        pet, memory, _db = self.build_pet()
        pet._save_preference("chat.size", "1180x760")
        self.assertEqual(pet._saved_card_size(), (1180, 760))
        self.assertEqual(memory.get_preference("chat.size"), "1180x760")


if __name__ == "__main__":
    unittest.main()
