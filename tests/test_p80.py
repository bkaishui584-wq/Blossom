"""P0 验收：高清与缩放、主题切换重画、功能页进主工作台。

覆盖：缩放倍率的换算与边界、字号档位、偏好读写、换主题后整块重画不再白底白字、
功能页嵌进中央区域、页面开关与返回聊天，以及窗口重建后会话与页面都还在。
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
from database.db import Database
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatDelta, ChatResult, ModelProvider, Usage
from security.config import Config
from ui import panel, theme
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


class ScaleCase(unittest.TestCase):
    """缩放纯粹是算数与偏好，不需要窗口。"""

    def setUp(self):
        self.addCleanup(theme.reset_scale)
        theme.reset_scale()

    def test_the_steps_are_bounded(self):
        self.assertEqual(theme.step_scale(theme.SCALE_MAX, 1), theme.SCALE_MAX)
        self.assertEqual(theme.step_scale(theme.SCALE_MIN, -1), theme.SCALE_MIN)

    def test_a_step_moves_one_notch(self):
        self.assertEqual(theme.step_scale(1.0, 1), 1.1)
        self.assertEqual(theme.step_scale(1.0, -1), 0.9)

    def test_the_same_scale_is_reapplied_not_compounded(self):
        base = theme.PAD_MD
        theme.apply_scale(ui_scale=2.0, dpi=1.0)
        doubled = theme.PAD_MD
        theme.apply_scale(ui_scale=1.0, dpi=1.0)
        self.assertEqual(theme.PAD_MD, base)
        theme.apply_scale(ui_scale=2.0, dpi=1.0)
        self.assertEqual(theme.PAD_MD, doubled)

    def test_the_system_ratio_and_the_user_ratio_multiply(self):
        theme.apply_scale(ui_scale=1.5, dpi=2.0)
        self.assertAlmostEqual(theme.current_scale(), 3.0, places=3)
        self.assertAlmostEqual(theme.user_scale(), 1.5, places=3)
        self.assertAlmostEqual(theme.dpi_scale(), 2.0, places=3)

    def test_a_font_step_changes_the_font_size(self):
        normal = theme.FONT_BODY[1]
        theme.apply_scale(font_scale_name="huge", dpi=1.0)
        self.assertGreater(theme.FONT_BODY[1], normal)
        theme.apply_scale(font_scale_name="small", dpi=1.0)
        self.assertLess(theme.FONT_BODY[1], normal)

    def test_an_unknown_font_step_falls_back(self):
        theme.apply_scale(font_scale_name="gigantic", dpi=1.0)
        self.assertEqual(theme.font_scale(), theme.DEFAULT_FONT_SCALE)

    def test_button_padding_scales_with_the_ratio(self):
        theme.apply_scale(ui_scale=1.0, dpi=1.0)
        small = theme.button_padding()
        theme.apply_scale(ui_scale=2.0, dpi=1.0)
        self.assertGreater(theme.button_padding()[0], small[0])

    def test_reset_goes_back_to_one(self):
        theme.apply_scale(ui_scale=1.75, font_scale_name="large", dpi=1.0)
        theme.reset_scale()
        self.assertAlmostEqual(theme.user_scale(), 1.0, places=3)
        self.assertEqual(theme.font_scale(), theme.DEFAULT_FONT_SCALE)

    def test_reading_back_what_was_saved(self):
        class Prefs:
            def __init__(self, rows):
                self.rows = rows

            def get_preference(self, key):
                return self.rows.get(key)

        rows = {theme.SCALE_PREFERENCE: "1.25", theme.FONT_SCALE_PREFERENCE: "large"}
        theme.scale_saved(Prefs(rows))
        self.assertAlmostEqual(theme.user_scale(), 1.25, places=3)
        self.assertEqual(theme.font_scale(), "large")

    def test_no_stored_scale_means_standard(self):
        theme.scale_saved(None)
        self.assertAlmostEqual(theme.user_scale(), 1.0, places=3)
        self.assertEqual(theme.font_scale(), theme.DEFAULT_FONT_SCALE)


class PanelCase(unittest.TestCase):
    """功能页容器：嵌入给 Frame，单开给窗口。"""

    def setUp(self):
        self.addCleanup(theme.reset_scale)
        theme.reset_scale()
        try:
            self.root = tk.Tk()
        except Exception as exc:  # pragma: no cover - 没有图形环境
            self.skipTest("tkinter 不可用：%s" % exc)
        self.root.withdraw()
        self.addCleanup(self._destroy)

    def _destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def test_embedded_returns_a_frame(self):
        parent = tk.Frame(self.root)
        frame = panel.container(None, "测试", (400, 300), parent)
        self.assertIsInstance(frame, tk.Frame)
        self.assertTrue(panel.is_embedded(SimplePanel(frame, embedded=True)))

    def test_separate_mode_returns_a_window(self):
        class FakeHost:
            def card_position(self, width, height):
                return (10, 10)

        class FakePet:
            root = None
            host = FakeHost()

        FakePet.root = self.root
        win = panel.container(FakePet(), "测试", (400, 300), None)
        self.assertIsInstance(win, tk.Toplevel)
        self.assertFalse(panel.is_embedded(SimplePanel(win)))
        win.destroy()

    def test_the_close_button_does_not_duplicate_in_the_workbench(self):
        parent = tk.Frame(self.root)
        frame = panel.container(None, "测试", (400, 300), parent)
        placeholder = panel.close_button(frame, SimplePanel(frame, embedded=True))
        self.assertIsInstance(placeholder, tk.Frame)


class SimplePanel:
    """只带容器需要的那几个属性的替身。"""

    def __init__(self, win, embedded=False):
        self.win = win
        self.embedded = embedded


class WorkbenchCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(theme.reset_scale)
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
        self.config.live2d_model = ""

    def _destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def build_pet(self):
        database = Database(self.config.db_path)
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        agent = AgentCore(StubProvider(), memory, ContextManager(token_budget=800))
        pet = DesktopPet(agent, self.config, memory=memory, root=self.root)
        pet.show_chat()
        self.addCleanup(lambda: self._settle(pet))
        return pet

    def _settle(self, pet):
        try:
            pet.root.update_idletasks()
            pet.root.update()
        except tk.TclError:
            pass


class ThemeRebuildTests(WorkbenchCase):
    """换主题要整块重画，不能只刷底色留下白底白字。"""

    def test_switching_theme_repaints_the_whole_workbench(self):
        pet = self.build_pet()
        self.addCleanup(lambda: pet.set_theme("dark"))
        pet.set_theme("light")
        self.assertEqual(theme.active(), "light")
        self.assertEqual(str(pet.chat.parent.cget("bg")).lower(), theme.BG.lower())
        pet.set_theme("dark")
        self.assertEqual(str(pet.chat.parent.cget("bg")).lower(), theme.BG.lower())

    def test_the_chat_view_object_is_replaced(self):
        pet = self.build_pet()
        self.addCleanup(lambda: pet.set_theme("dark"))
        before = pet.chat
        pet.set_theme("light")
        self.assertIsNot(pet.chat, before)
        self.assertTrue(pet.chat.parent.winfo_exists())

    def test_the_open_page_survives_a_theme_change(self):
        pet = self.build_pet()
        self.addCleanup(lambda: pet.set_theme("dark"))
        pet.open_settings()
        self.assertTrue(pet.chat.page_is_open())
        pet.set_theme("light")
        self.assertTrue(pet.chat.page_is_open())
        self.assertEqual(pet.chat.page_key, "settings")

    def test_a_broken_theme_name_does_not_break_the_window(self):
        pet = self.build_pet()
        pet.set_theme("plaid")
        self.assertTrue(pet.chat.parent.winfo_exists())


class ScaleCommandTests(WorkbenchCase):
    def test_zoom_in_and_out_move_one_notch(self):
        pet = self.build_pet()
        pet.set_ui_scale(1.0)
        pet.zoom_in()
        self.assertAlmostEqual(pet.ui_scale(), 1.1, places=3)
        pet.zoom_out()
        self.assertAlmostEqual(pet.ui_scale(), 1.0, places=3)

    def test_the_choice_is_remembered(self):
        pet = self.build_pet()
        pet.set_ui_scale(1.25, font_scale_name="large")
        self.assertEqual(pet._preference(theme.SCALE_PREFERENCE), "1.25")
        self.assertEqual(pet._preference(theme.FONT_SCALE_PREFERENCE), "large")

    def test_reset_puts_both_back(self):
        pet = self.build_pet()
        pet.set_ui_scale(1.5, font_scale_name="huge")
        pet.zoom_reset()
        self.assertAlmostEqual(pet.ui_scale(), 1.0, places=3)
        self.assertEqual(pet.font_scale_name(), theme.DEFAULT_FONT_SCALE)

    def test_the_window_keeps_working_after_a_rebuild(self):
        pet = self.build_pet()
        pet.set_ui_scale(1.1)
        self.assertTrue(pet.card.winfo_exists())
        self.assertTrue(pet.chat.parent.winfo_exists())
        self.assertEqual(str(pet.chat.parent.cget("bg")).lower(), theme.BG.lower())

    def test_the_size_text_reports_both_ratios(self):
        pet = self.build_pet()
        pet.set_ui_scale(1.0)
        text = pet.scale_text()
        self.assertIn("100%", text)
        self.assertIn("界面", text)


class PageHostTests(WorkbenchCase):
    def test_opening_a_page_puts_the_chat_area_away(self):
        pet = self.build_pet()
        main_children = set(pet.chat.main.pack_slaves())
        pet.open_settings()
        self.assertTrue(pet.chat.page_is_open())
        self.assertEqual(str(pet.chat.page_host.winfo_manager()), "pack")
        self.assertEqual(pet.chat.page_title.cget("text"), "设置")
        self.assertNotIn(pet.chat.page_host, main_children)

    def test_returning_to_chat_restores_the_area(self):
        pet = self.build_pet()
        pet.open_settings()
        self.assertTrue(pet.chat.close_page())
        self.assertFalse(pet.chat.page_is_open())
        self.assertEqual(pet.chat.page_key, "")
        self.assertTrue(pet.chat.main.pack_slaves())

    def test_the_same_page_is_only_built_once(self):
        pet = self.build_pet()
        first = pet.open_settings()
        pet.close_panel()
        second = pet.open_settings()
        self.assertIs(first, second)

    def test_every_nav_route_opens_something_real(self):
        pet = self.build_pet()
        for key in ("settings", "persona", "tasks", "skills", "capabilities", "devices", "awareness"):
            pet.close_panel()
            page = pet._chat_nav(key)
            self.assertIsNotNone(page, key)
            self.assertTrue(pet.chat.page_is_open(), key)

    def test_an_unknown_nav_key_is_ignored(self):
        pet = self.build_pet()
        self.assertIsNone(pet._chat_nav("nope"))

    def test_the_empty_welcome_page_comes_back(self):
        pet = self.build_pet()
        pet.new_session()
        self.assertTrue(pet.chat.is_empty_shown())
        pet.open_settings()
        self.assertFalse(pet.chat.is_empty_shown())
        pet.close_panel()
        self.assertTrue(pet.chat.is_empty_shown())


class NavTests(unittest.TestCase):
    def test_the_screen_awareness_entry_has_an_icon(self):
        for key, _label in ChatView.NAV_ITEMS:
            self.assertIn(key, ChatView.NAV_ICONS, key)

    def test_the_four_tints_are_all_readable_in_both_themes(self):
        for palette in (theme.DARK, theme.LIGHT):
            for name in ("SKY", "CYAN", "PINK", "WHITE"):
                value = palette[name]
                self.assertTrue(value.startswith("#") and len(value) == 7, (name, value))
        # 浅色底上不能再用纯白当前景色，否则等于看不见。
        self.assertNotEqual(theme.LIGHT["WHITE"], "#ffffff")


if __name__ == "__main__":
    unittest.main()
