"""P1 验收：桌宠贴边、缩入与滑出。

覆盖：四条边都能吸附、离得远就不吸附、缩入只留一条且能滑回来、开关立刻生效、
重启后按记着的边恢复、复位会取消贴边、缩入时存的位置不会把桌宠丢到屏幕外。
"""

import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.context import ContextManager
from agent.core import AgentCore
from database.db import Database
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatDelta, ChatResult, ModelProvider, Usage
from security.config import Config
from ui import pet_host, theme
from ui.desktop import DesktopPet


class StubProvider(ModelProvider):
    name = "stub"

    def is_available(self):
        return True

    def chat(self, messages, tools=None, temperature=0.7):
        return ChatResult(ok=True, content="在的。", usage=Usage(5, 3), model="stub")

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


class DockCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(theme.reset_scale)
        theme.reset_scale()
        try:
            self.root = tk.Tk()
        except Exception as exc:  # pragma: no cover
            self.skipTest("tkinter 不可用：%s" % exc)
        self.root.withdraw()
        self.addCleanup(self._destroy)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.config = Config(load_env=False)
        self.config.data_dir = self.base
        self.config.log_dir = self.base / "logs"
        self.config.db_path = self.base / "t.sqlite3"
        self.config.characters_dir = self.base / "characters"
        self.config.live2d_dir = self.base / "live2d"
        self.config.devices_dir = self.base / "devices"
        self.config.tray = False
        self.config.live2d_model = ""
        self._built = 0

    def _destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def build_pet(self):
        self._built += 1
        database = Database(self.config.db_path)
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        agent = AgentCore(StubProvider(), memory, ContextManager(token_budget=800))
        pet = DesktopPet(agent, self.config, memory=memory, root=self.root)
        pet.show_chat()
        return pet

    # ---------- 位置辅助 ----------
    def screen(self):
        return pet_host.virtual_screen(self.root)

    def park(self, pet, edge, offset=280):
        """把桌宠拖到指定边附近并松手，走的是真实的按下、移动、松手那条路。"""
        left, top, width, height = self.screen()
        w, h = pet.host.size()
        stops = {
            "left": (left + 20, top + offset),
            "right": (left + width - w - 20, top + offset),
            "top": (left + offset, top + 20),
            "bottom": (left + offset, top + height - h - 20),
        }
        x, y = stops[edge]
        pet.host._press(SimpleNamespace(x_root=x + 4, y_root=y + 4))
        pet.host.move_to(x, y)
        # 松手位置和按下位置差得够多，才算一次拖动而不是点击。
        pet.host._release(SimpleNamespace(x_root=x + 60, y_root=y + 60))
        return pet.host.docked_edge()


class SnapTests(DockCase):
    def test_every_edge_can_be_docked(self):
        for edge in ("left", "right", "top", "bottom"):
            pet = self.build_pet()
            self.assertEqual(self.park(pet, edge), edge, edge)

    def test_a_dock_sits_flush_to_that_edge(self):
        pet = self.build_pet()
        self.park(pet, "left")
        left, _top, _w, _h = self.screen()
        self.assertLessEqual(pet.host.position()[0] - left, pet_host.DOCK_GAP)

    def test_being_far_from_every_edge_does_not_dock(self):
        pet = self.build_pet()
        left, top, width, height = self.screen()
        x, y = left + width // 2, top + height // 2
        pet.host._press(SimpleNamespace(x_root=x, y_root=y))
        pet.host.move_to(x, y)
        pet.host._release(SimpleNamespace(x_root=x + 60, y_root=y + 60))
        self.assertEqual(pet.host.docked_edge(), "")

    def test_turning_docking_off_stops_the_snap(self):
        pet = self.build_pet()
        pet.set_dock(False)
        self.assertFalse(pet.host.docking)
        left, top, _w, _h = self.screen()
        x, y = left + 20, top + 300
        pet.host._press(SimpleNamespace(x_root=x, y_root=y))
        pet.host.move_to(x, y)
        pet.host._release(SimpleNamespace(x_root=x + 60, y_root=y + 60))
        self.assertEqual(pet.host.docked_edge(), "")

    def test_turning_it_back_on_lets_it_snap_again(self):
        pet = self.build_pet()
        pet.set_dock(False)
        pet.set_dock(True)
        self.assertEqual(self.park(pet, "right"), "right")

    def test_undocking_stays_where_it_is(self):
        pet = self.build_pet()
        self.park(pet, "left")
        before = pet.host.position()
        pet.undock_pet()
        self.assertEqual(pet.host.docked_edge(), "")
        self.assertEqual(pet.host.position(), before)


class PeekTests(DockCase):
    def test_sliding_in_hides_most_of_it(self):
        pet = self.build_pet()
        self.park(pet, "left")
        rest = pet.host.position()
        self.assertTrue(pet.host.slide_in())
        self.assertTrue(pet.host.peeked())
        left, _top, _w, _h = self.screen()
        self.assertLess(pet.host.position()[0], rest[0])
        # 还要留一截在屏幕里，不然用户点不回来。
        w, _h2 = pet.host.window_size()
        visible = (pet.host.position()[0] + w) - left
        self.assertGreaterEqual(visible, pet_host.DOCK_PEEK_MIN)

    def test_sliding_out_restores_the_full_body(self):
        pet = self.build_pet()
        self.park(pet, "bottom")
        rest = pet.host.position()
        pet.host.slide_in()
        self.assertTrue(pet.host.slide_out())
        self.assertFalse(pet.host.peeked())
        self.assertEqual(pet.host.position(), rest)

    def test_a_hover_brings_it_back(self):
        pet = self.build_pet()
        self.park(pet, "right")
        pet.host.slide_in()
        pet.host._pointer_near(None)
        self.assertFalse(pet.host.peeked())

    def test_leaving_arms_the_slide_but_does_not_hide_instantly(self):
        pet = self.build_pet()
        self.park(pet, "left")
        pet.host._pointer_away(None)
        self.assertFalse(pet.host.peeked())
        self.assertIsNotNone(pet.host._peek_timer)
        pet.host._cancel_peek_timer()

    def test_auto_hide_off_keeps_it_visible(self):
        pet = self.build_pet()
        pet.set_dock_auto(False)
        self.park(pet, "left")
        pet.host._pointer_away(None)
        self.assertIsNone(pet.host._peek_timer)
        self.assertFalse(pet.host.peeked())

    def test_peeking_without_a_dock_is_refused(self):
        pet = self.build_pet()
        self.assertFalse(pet.toggle_peek())
        self.assertFalse(pet.host.peeked())

    def test_the_menu_can_toggle_it(self):
        pet = self.build_pet()
        self.park(pet, "top")
        self.assertTrue(pet.toggle_peek())
        self.assertFalse(pet.toggle_peek())


class PositionMemoryTests(DockCase):
    def test_the_docked_edge_is_remembered(self):
        pet = self.build_pet()
        self.park(pet, "right")
        self.assertEqual(pet._preference(pet.DOCK_EDGE_PREFERENCE), "right")

    def test_a_restart_comes_back_to_the_same_edge(self):
        pet = self.build_pet()
        self.park(pet, "left")
        again = self.build_pet()
        self.assertEqual(again.host.docked_edge(), "left")

    def test_the_saved_position_ignores_the_peek_offset(self):
        pet = self.build_pet()
        self.park(pet, "left")
        pet.host.slide_in()
        pet._save_position()
        rest_x, rest_y = pet.host.dock_rest_position()
        self.assertEqual(
            pet._preference(pet.POSITION_KEY),
            "%d,%d" % (pet._unpx(rest_x), pet._unpx(rest_y)),
        )

    def test_resetting_the_position_also_undocks(self):
        pet = self.build_pet()
        self.park(pet, "left")
        pet.host.slide_in()
        pet.reset_position()
        self.assertEqual(pet.host.docked_edge(), "")
        self.assertFalse(pet.host.peeked())
        self.assertEqual(pet._preference(pet.DOCK_EDGE_PREFERENCE), "")

    def test_hiding_slides_it_back_out_first(self):
        pet = self.build_pet()
        self.park(pet, "left")
        pet.host.slide_in()
        pet.host.set_hidden(True)
        self.assertFalse(pet.host.peeked())

    def test_the_status_line_says_where_it_is(self):
        pet = self.build_pet()
        self.assertEqual(pet.dock_text(), "没贴边")
        self.park(pet, "bottom")
        self.assertIn("下边", pet.dock_text())
        pet.host.slide_in()
        self.assertIn("缩入", pet.dock_text())


if __name__ == "__main__":
    unittest.main()
