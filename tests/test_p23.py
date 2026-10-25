"""P23 验收：专注模式 —— 全屏时自动降低存在感，全是程序判断，不经过模型。"""

import os
import sys
import tkinter as tk
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from behavior.focus import FocusMode, WindowsWindowProvider
from behavior.manager import NullWindowProvider, WindowProvider
from events import bus as bus_module
from events.bus import EventBus
from persona.state import EmotionState
from security.config import Config
from ui.desktop import DesktopPet
from ui.pet_host import PetHost


class FakeWindow(WindowProvider):
    name = "fake"

    def __init__(self, busy=False, available=True):
        self.busy = busy
        self.available = available

    def is_available(self):
        return self.available

    def is_busy(self, now=None):
        return self.busy


class BrokenWindow(WindowProvider):
    name = "broken"

    def is_busy(self, now=None):
        raise RuntimeError("probe failed")


class StubAgent:
    def __init__(self):
        self.avatar = None
        self.emotion = EmotionState()
        self.persona = None
        self.mode_name = "local"


class StubBehavior:
    def __init__(self):
        self.idle_calls = 0

    def idle(self, now=None):
        self.idle_calls += 1
        return None


class InterfaceTests(unittest.TestCase):
    def test_window_provider_still_only_requires_is_busy(self):
        window = NullWindowProvider()
        self.assertFalse(window.is_busy())
        self.assertTrue(window.is_available())


class WindowsProviderTests(unittest.TestCase):
    def test_reads_a_boolean(self):
        provider = WindowsWindowProvider()
        self.assertEqual(provider.is_available(), os.name == "nt")
        self.assertIsInstance(provider.is_fullscreen(), bool)

    def test_busy_means_fullscreen(self):
        provider = WindowsWindowProvider()
        self.assertEqual(provider.is_busy(), provider.is_fullscreen())

    def test_is_quiet_on_other_platforms(self):
        if os.name == "nt":
            self.skipTest("只在非 Windows 上验证降级")
        provider = WindowsWindowProvider()
        self.assertFalse(provider.is_available())
        self.assertFalse(provider.is_fullscreen())


class FocusModeTests(unittest.TestCase):
    def test_no_provider_never_enters_focus(self):
        focus = FocusMode()
        self.assertEqual(focus.update(), "")
        self.assertFalse(focus.active)
        self.assertEqual(focus.enter_count, 0)

    def test_fullscreen_has_to_persist_before_entering(self):
        focus = FocusMode(provider=FakeWindow(True), enter_after=2)
        self.assertEqual(focus.update(), "")
        self.assertFalse(focus.active)
        self.assertEqual(focus.update(), bus_module.FULLSCREEN_ENTERED)
        self.assertTrue(focus.active)

    def test_entering_is_reported_once(self):
        focus = FocusMode(provider=FakeWindow(True), enter_after=1)
        focus.update()
        self.assertEqual(focus.update(), "")
        self.assertEqual(focus.enter_count, 1)

    def test_leaving_has_to_persist_too(self):
        window = FakeWindow(True)
        focus = FocusMode(provider=window, enter_after=1, exit_after=2)
        focus.update()
        window.busy = False
        self.assertEqual(focus.update(), "")
        self.assertTrue(focus.active)
        self.assertEqual(focus.update(), bus_module.FULLSCREEN_EXITED)
        self.assertFalse(focus.active)

    def test_only_once_around_never_flips(self):
        window = FakeWindow(True)
        focus = FocusMode(provider=window, enter_after=2, exit_after=2)
        focus.update()
        window.busy = False
        focus.update()
        window.busy = True
        focus.update()
        window.busy = False
        self.assertEqual(focus.update(), "")
        self.assertFalse(focus.active)
        self.assertEqual(focus.enter_count, 0)

    def test_full_cycle_counts_once_each(self):
        window = FakeWindow(False)
        focus = FocusMode(provider=window, enter_after=1, exit_after=1)
        window.busy = True
        self.assertEqual(focus.update(), bus_module.FULLSCREEN_ENTERED)
        window.busy = False
        self.assertEqual(focus.update(), bus_module.FULLSCREEN_EXITED)
        self.assertEqual((focus.enter_count, focus.exit_count), (1, 1))

    def test_events_go_on_the_bus(self):
        bus = EventBus()
        seen = []
        bus.subscribe(bus_module.FULLSCREEN_ENTERED, lambda event: seen.append("in"))
        bus.subscribe(bus_module.FULLSCREEN_EXITED, lambda event: seen.append("out"))
        window = FakeWindow(False)
        focus = FocusMode(provider=window, bus=bus, enter_after=1, exit_after=1)
        window.busy = True
        focus.update()
        window.busy = False
        focus.update()
        self.assertEqual(seen, ["in", "out"])

    def test_callbacks_fire(self):
        seen = []
        window = FakeWindow(True)
        focus = FocusMode(
            provider=window,
            enter_after=1,
            exit_after=1,
            on_enter=lambda: seen.append("in"),
            on_exit=lambda: seen.append("out"),
        )
        focus.update()
        window.busy = False
        focus.update()
        self.assertEqual(seen, ["in", "out"])

    def test_broken_probe_is_contained(self):
        focus = FocusMode(provider=BrokenWindow(), enter_after=1)
        self.assertEqual(focus.update(), "")
        self.assertFalse(focus.active)

    def test_broken_callback_is_contained(self):
        def explode():
            raise RuntimeError("boom")

        focus = FocusMode(provider=FakeWindow(True), enter_after=1, on_enter=explode)
        self.assertEqual(focus.update(), bus_module.FULLSCREEN_ENTERED)

    def test_unavailable_source_is_reported(self):
        focus = FocusMode(provider=FakeWindow(available=False))
        self.assertFalse(focus.available())

    def test_describe_reports_state(self):
        focus = FocusMode(provider=FakeWindow(True), enter_after=1)
        focus.update()
        info = focus.describe()
        self.assertEqual(info["provider"], "fake")
        self.assertTrue(info["active"])
        self.assertTrue(info["fullscreen"])
        self.assertEqual(info["enter_count"], 1)


class HostDimTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except Exception as exc:
            self.skipTest(f"tkinter 不可用：{exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy)
        try:
            self.root.wm_attributes("-alpha")
        except tk.TclError as exc:
            self.skipTest(f"这个平台不支持窗口透明度：{exc}")

    def _destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _host(self):
        return PetHost(self.root, lambda root: tk.Canvas(root, width=10, height=10))

    def test_dim_fades_the_pet_and_brings_it_back(self):
        host = self._host()
        self.assertTrue(host.set_dim(True))
        self.assertTrue(host.dimmed)
        self.assertAlmostEqual(float(self.root.wm_attributes("-alpha")), 0.35, places=2)
        self.assertFalse(host.set_dim(False))
        self.assertFalse(host.dimmed)
        self.assertAlmostEqual(float(self.root.wm_attributes("-alpha")), 1.0, places=2)

    def test_broken_alpha_change_is_contained(self):
        host = self._host()
        self.root.wm_attributes = lambda *args: (_ for _ in ()).throw(tk.TclError("no"))
        self.assertFalse(host.set_dim(True))


class DesktopFocusTests(unittest.TestCase):
    def setUp(self):
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

    def _pet(self, focus=None, behavior=None):
        config = Config(load_env=False)
        config.dev_mode = True
        pet = DesktopPet(
            StubAgent(),
            config,
            root=self.root,
            behavior=behavior,
            focus=focus,
        )
        if focus is not None:
            focus.on_enter = pet.enter_focus
            focus.on_exit = pet.exit_focus
        return pet

    def test_fullscreen_lowers_presence(self):
        window = FakeWindow(True)
        pet = self._pet(focus=FocusMode(provider=window, enter_after=1))
        pet._focus_tick()
        self.assertTrue(pet.focus.active)
        self.assertTrue(pet.host.dimmed)

    def test_leaving_fullscreen_restores_the_pet(self):
        window = FakeWindow(True)
        pet = self._pet(focus=FocusMode(provider=window, enter_after=1, exit_after=1))
        pet._focus_tick()
        window.busy = False
        pet._focus_tick()
        self.assertFalse(pet.focus.active)
        self.assertFalse(pet.host.dimmed)

    def test_open_chat_comes_back_after_focus(self):
        window = FakeWindow(True)
        pet = self._pet(focus=FocusMode(provider=window, enter_after=1, exit_after=1))
        pet.show_chat()
        pet._focus_tick()
        self.assertFalse(pet.chat_visible)
        window.busy = False
        pet._focus_tick()
        self.assertTrue(pet.chat_visible)

    def test_idle_motions_pause_while_focused(self):
        behavior = StubBehavior()
        window = FakeWindow(True)
        pet = self._pet(
            focus=FocusMode(provider=window, enter_after=1, exit_after=1),
            behavior=behavior,
        )
        pet._focus_tick()
        pet._idle_tick()
        self.assertEqual(behavior.idle_calls, 0)
        window.busy = False
        pet._focus_tick()
        pet._idle_tick()
        self.assertEqual(behavior.idle_calls, 1)

    def test_focus_shows_up_in_the_developer_panel(self):
        pet = self._pet(focus=FocusMode(provider=FakeWindow(False)))
        pet._set_dev({})
        self.assertIn("专注模式", pet.dev.get("1.0", "end"))

    def test_broken_window_probe_does_not_break_the_pet(self):
        pet = self._pet(focus=FocusMode(provider=BrokenWindow(), enter_after=1))
        pet._focus_tick()
        self.assertFalse(pet.focus.active)
        self.assertFalse(pet.host.dimmed)

    def test_no_window_source_keeps_the_pet_unchanged(self):
        pet = self._pet()
        pet._focus_tick()
        self.assertFalse(pet.host.dimmed)


class AgentDecouplingTests(unittest.TestCase):
    def test_agent_core_knows_nothing_about_fullscreen(self):
        text = (ROOT / "agent" / "core.py").read_text(encoding="utf-8").lower()
        for word in ("fullscreen", "is_fullscreen", "getforegroundwindow", "set_dim", "全屏"):
            self.assertNotIn(word, text)

    def test_focus_module_uses_only_the_standard_library(self):
        text = (ROOT / "behavior" / "focus.py").read_text(encoding="utf-8").lower()
        for needle in ("pywin32", "win32gui", "psutil", "pygetwindow", "screeninfo"):
            self.assertNotIn(needle, text)

    def test_swapping_the_window_source_needs_no_agent_change(self):
        for provider in (FakeWindow(True), NullWindowProvider(), BrokenWindow()):
            focus = FocusMode(provider=provider, enter_after=1, exit_after=1)
            focus.update()
            focus.describe()


if __name__ == "__main__":
    unittest.main()
