"""P2 屏幕感知与视觉接口：只读屏幕数字，视觉只保留接口。"""

import inspect
import os
import sys
import tkinter as tk
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from behavior.screen import (
    NullScreenProvider,
    ScreenProvider,
    ScreenWatcher,
    WindowsScreenProvider,
)
from behavior.vision import NullVisionProvider, VisionProvider
from events import bus as bus_module
from events.bus import EventBus
from persona.state import EmotionState
from security.config import Config
from ui.desktop import DesktopPet, launch

LAYOUT = {
    "count": 1,
    "primary": {"width": 1920, "height": 1080},
    "virtual": {"left": 0, "top": 0, "width": 1920, "height": 1080},
}


class StubAgent:
    def __init__(self):
        self.avatar = None
        self.emotion = EmotionState()
        self.persona = None
        self.mode_name = "local"


class ScriptedScreen(ScreenProvider):
    name = "scripted"

    def __init__(self, layout=None, available=True, boom=False):
        self.layout_value = dict(layout or {})
        self.available = available
        self.boom = boom

    def is_available(self):
        return self.available

    def layout(self):
        if self.boom:
            raise RuntimeError("screen exploded")
        return dict(self.layout_value)


def watcher_with(layout=None, **kwargs):
    bus = EventBus()
    seen = []
    bus.subscribe(bus_module.SCREEN_CHANGED, seen.append)
    provider = ScriptedScreen(layout, **kwargs)
    return provider, ScreenWatcher(provider=provider, bus=bus), seen


class ProviderTests(unittest.TestCase):
    def test_null_provider_reads_nothing(self):
        provider = NullScreenProvider()
        self.assertFalse(provider.is_available())
        self.assertEqual(provider.layout(), {})

    def test_windows_provider_reads_numbers(self):
        if os.name != "nt":
            self.skipTest("只在 Windows 上读数")
        provider = WindowsScreenProvider()
        self.assertTrue(provider.is_available())
        layout = provider.layout()
        self.assertGreaterEqual(layout["count"], 1)
        self.assertGreater(layout["primary"]["width"], 0)
        self.assertGreater(layout["virtual"]["width"], 0)

    def test_windows_provider_is_unavailable_off_windows(self):
        if os.name == "nt":
            self.skipTest("只在非 Windows 上判断")
        self.assertFalse(WindowsScreenProvider().is_available())


class WatcherTests(unittest.TestCase):
    def test_first_read_announces_the_layout(self):
        _, watcher, seen = watcher_with(LAYOUT)
        self.assertEqual(watcher.update(), bus_module.SCREEN_CHANGED)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].payload["count"], 1)

    def test_same_layout_stays_silent(self):
        _, watcher, seen = watcher_with(LAYOUT)
        watcher.update()
        self.assertEqual(watcher.update(), "")
        self.assertEqual(len(seen), 1)
        self.assertEqual(watcher.changes, 1)

    def test_resolution_change_is_announced(self):
        provider, watcher, seen = watcher_with(LAYOUT)
        watcher.update()
        provider.layout_value = {
            "count": 1,
            "primary": {"width": 1280, "height": 720},
            "virtual": {"left": 0, "top": 0, "width": 1280, "height": 720},
        }
        self.assertEqual(watcher.update(), bus_module.SCREEN_CHANGED)
        self.assertEqual(len(seen), 2)
        self.assertEqual(watcher.changes, 2)

    def test_unreadable_layout_keeps_previous_state(self):
        provider, watcher, seen = watcher_with(LAYOUT)
        watcher.update()
        provider.layout_value = {}
        self.assertEqual(watcher.update(), "")
        self.assertEqual(len(seen), 1)
        self.assertEqual(watcher.layout["count"], 1)

    def test_broken_provider_does_not_crash(self):
        _, watcher, seen = watcher_with(boom=True)
        self.assertEqual(watcher.update(), "")
        self.assertEqual(seen, [])

    def test_describe_carries_layout(self):
        _, watcher, _ = watcher_with(LAYOUT)
        watcher.update()
        info = watcher.describe()
        self.assertEqual(info["count"], 1)
        self.assertEqual(info["primary"]["width"], 1920)
        self.assertEqual(info["changes"], 1)

    def test_unavailable_provider_is_reported(self):
        watcher = ScreenWatcher(provider=ScriptedScreen(available=False))
        self.assertFalse(watcher.available())


class VisionTests(unittest.TestCase):
    def test_null_vision_says_it_cannot_see(self):
        provider = NullVisionProvider()
        self.assertFalse(provider.is_available())
        result = provider.observe()
        self.assertFalse(result["ok"])
        self.assertIsNone(result["data"])
        self.assertTrue(result["error"])

    def test_vision_interface_is_swappable(self):
        class FakeVision(VisionProvider):
            name = "fake"

            def is_available(self):
                return True

            def observe(self, source="screen"):
                return {"ok": True, "data": {"source": source}, "error": ""}

        result = FakeVision().observe("camera")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["source"], "camera")


class NoCaptureTests(unittest.TestCase):
    def test_screen_layer_never_reads_pixels(self):
        text = (ROOT / "behavior" / "screen.py").read_text(encoding="utf-8").lower()
        for word in ("imagegrab", "screenshot", "pillow", "cv2", "mss", "pyautogui"):
            self.assertNotIn(word, text)

    def test_vision_layer_has_no_capture_yet(self):
        text = (ROOT / "behavior" / "vision.py").read_text(encoding="utf-8").lower()
        for word in ("imagegrab", "screenshot", "pillow", "cv2", "camera.open"):
            self.assertNotIn(word, text)

    def test_agent_core_knows_nothing_about_screens_or_vision(self):
        text = (ROOT / "agent" / "core.py").read_text(encoding="utf-8").lower()
        for word in ("screenwatcher", "screenshot", "vision", "getsystemmetrics"):
            self.assertNotIn(word, text)


class DesktopTests(unittest.TestCase):
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

    def _pet(self, screen=None, vision=None):
        config = Config(load_env=False)
        config.dev_mode = True
        return DesktopPet(
            StubAgent(), config, root=self.root, screen=screen, vision=vision
        )

    def test_layout_change_moves_the_pet_back(self):
        provider = ScriptedScreen(LAYOUT)
        pet = self._pet(screen=ScreenWatcher(provider=provider))
        calls = []
        pet._apply_saved_position = lambda: calls.append(1)
        pet._screen_tick()
        self.assertEqual(len(calls), 1)
        pet._screen_tick()
        self.assertEqual(len(calls), 1)

    def test_broken_screen_does_not_break_the_pet(self):
        pet = self._pet(screen=ScreenWatcher(provider=ScriptedScreen(boom=True)))
        calls = []
        pet._apply_saved_position = lambda: calls.append(1)
        pet._screen_tick()
        self.assertEqual(calls, [])

    def test_no_screen_source_is_fine(self):
        pet = self._pet()
        pet._screen_tick()
        self.assertIsNone(pet.screen)

    def test_panel_shows_screen_and_vision(self):
        pet = self._pet(
            screen=ScreenWatcher(provider=ScriptedScreen(LAYOUT)),
            vision=NullVisionProvider(),
        )
        pet._set_dev({})
        text = pet.dev.get("1.0", "end")
        self.assertIn("屏幕感知", text)
        self.assertIn("视觉", text)

    def test_launch_accepts_screen_and_vision(self):
        params = inspect.signature(launch).parameters
        self.assertIn("screen", params)
        self.assertIn("vision", params)


if __name__ == "__main__":
    unittest.main()
