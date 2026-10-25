"""V2 P0 验收：透明桌宠宿主 —— 窗口只是角色载体，聊天是旁边的小浮层。"""

import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.context import ContextManager
from agent.core import AgentCore
from database.db import Database
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatDelta, ChatResult, ModelProvider, Usage
from persona.interface import DefaultPersonaProvider
from security.config import Config
from ui.desktop import CARD_HEIGHT, CARD_WIDTH, DesktopPet
from ui.pet_host import DOT_SIZE, TRANSPARENT_KEY, PetHost


class StubProvider(ModelProvider):
    """记调用次数的假 Provider：空闲时一次都不该被调用。"""

    name = "stub"

    def __init__(self):
        self.calls = 0

    def is_available(self):
        return True

    def chat(self, messages, tools=None, temperature=0.7):
        self.calls += 1
        return ChatResult(ok=True, content="在的。", usage=Usage(1, 1), model="stub")

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


class FakeEvent:
    def __init__(self, x_root=0, y_root=0):
        self.x_root = x_root
        self.y_root = y_root


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


class PetHostTests(TkTestCase):
    def make_host(self):
        host = PetHost(
            tk.Toplevel(self.root),
            build_canvas=lambda parent: tk.Canvas(parent, width=100, height=120, bg=TRANSPARENT_KEY),
        )
        return host

    def test_host_is_frameless_and_uses_the_transparency_key(self):
        host = self.make_host()
        self.assertTrue(host.root.overrideredirect())
        self.assertEqual(str(host.canvas.cget("bg")), TRANSPARENT_KEY)

    def test_move_to_never_leaves_the_virtual_screen(self):
        host = self.make_host()
        left, top, right, bottom = host.bounds()
        self.assertEqual(host.move_to(-99999, -99999), (left, top))
        self.assertEqual(host.move_to(99999, 99999), (right, bottom))

    def test_card_sits_right_of_the_pet_until_the_edge(self):
        host = self.make_host()
        left, top, right, bottom = host.bounds()
        host.move_to(left, top)
        x, _y = host.card_position(CARD_WIDTH, CARD_HEIGHT)
        self.assertEqual(x, host.position()[0] + host.size()[0] + 12)
        host.move_to(right, bottom)
        x, _y = host.card_position(CARD_WIDTH, CARD_HEIGHT)
        self.assertLess(x, host.position()[0])

    def test_click_without_drag_opens_the_menu(self):
        host = self.make_host()
        seen = []
        host.on_click = seen.append
        host._press(FakeEvent(500, 400))
        host._release(FakeEvent(500, 400))
        self.assertEqual(len(seen), 1)

    def test_drag_moves_the_pet_and_does_not_open_the_menu(self):
        host = self.make_host()
        seen, moved = [], []
        host.on_click = seen.append
        host.on_moved = moved.append
        host._press(FakeEvent(500, 400))
        host._motion(FakeEvent(540, 430))
        host._release(FakeEvent(540, 430))
        self.assertEqual(seen, [])
        self.assertEqual(len(moved), 1)

    def test_hide_leaves_a_dot_you_can_click_back(self):
        host = self.make_host()
        self.assertFalse(host.hidden)
        host.set_hidden(True)
        self.assertTrue(host.hidden)
        self.assertEqual(host._dot.winfo_manager(), "pack")
        self.assertEqual(host.canvas.winfo_manager(), "")
        self.assertEqual(host.window_size(), (DOT_SIZE, DOT_SIZE))
        host.toggle_hidden()
        self.assertFalse(host.hidden)
        self.assertEqual(host.canvas.winfo_manager(), "pack")
        self.assertEqual(host.window_size(), host.size())


class DesktopPetHostTests(TkTestCase):
    def build(self, provider=None, memory=None):
        database = Database(Path(self.tmp.name) / "t.sqlite3")
        database.migrate()
        memory = memory or MemoryManager(MemoryRepository(database))
        agent = AgentCore(
            provider or StubProvider(),
            memory,
            ContextManager(token_budget=600),
            persona=DefaultPersonaProvider(),
        )
        pet = DesktopPet(agent, Config(load_env=False), memory=memory, root=tk.Toplevel(self.root))
        return pet, agent, memory

    def test_pet_body_is_the_transparent_host_canvas(self):
        pet, _, _ = self.build()
        self.assertIs(pet.pet, pet.host.canvas)
        self.assertEqual(str(pet.pet.cget("bg")), TRANSPARENT_KEY)

    def test_chat_card_starts_hidden(self):
        pet, _, _ = self.build()
        self.assertFalse(pet.chat_visible)
        self.assertEqual(pet.card.wm_state(), "withdrawn")

    def test_toggle_chat_shows_and_hides_the_card(self):
        pet, _, _ = self.build()
        self.assertTrue(pet.toggle_chat())
        self.assertTrue(pet.chat_visible)
        self.assertEqual(pet.card.wm_state(), "normal")
        self.assertFalse(pet.toggle_chat())
        self.assertFalse(pet.chat_visible)
        self.assertEqual(pet.card.wm_state(), "withdrawn")

    def test_card_geometry_is_the_chat_size(self):
        pet, _, _ = self.build()
        pet.show_chat()
        self.assertTrue(
            pet.card.geometry().startswith("%dx%d" % pet._card_size)
        )

    def test_pet_position_is_remembered(self):
        pet, _, memory = self.build()
        pet.host.move_to(220, 180)
        self.assertEqual(pet.host.position(), (220, 180))
        pet._on_pet_moved(pet.host.position())
        self.assertEqual(memory.get_preference(DesktopPet.POSITION_KEY), "220,180")

    def test_saved_position_is_restored_on_start(self):
        database = Database(Path(self.tmp.name) / "r.sqlite3")
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        memory.set_preference(DesktopPet.POSITION_KEY, "260,200")
        pet, _, _ = self.build(memory=memory)
        self.assertEqual(pet.host.position(), (260, 200))

    def test_saved_position_never_comes_back_off_screen(self):
        database = Database(Path(self.tmp.name) / "c.sqlite3")
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        memory.set_preference(DesktopPet.POSITION_KEY, "99999,99999")
        pet, _, _ = self.build(memory=memory)
        _left, _top, right, bottom = pet.host.bounds()
        self.assertEqual(pet.host.position(), (right, bottom))

    def test_menu_actions_are_real(self):
        pet, _, _ = self.build()
        for _label, action in DesktopPet.MENU_ITEMS:
            self.assertTrue(callable(getattr(pet, action)), action)

    def test_hide_pet_keeps_a_way_back(self):
        pet, _, _ = self.build()
        pet.hide_pet()
        self.assertTrue(pet.host.hidden)
        pet.host.set_hidden(False)
        self.assertFalse(pet.host.hidden)

    def test_idle_loop_never_calls_the_model(self):
        provider = StubProvider()
        pet, _, _ = self.build(provider=provider)
        for _ in range(20):
            pet._drain()
        self.assertEqual(provider.calls, 0)


if __name__ == "__main__":
    unittest.main()
