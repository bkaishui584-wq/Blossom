"""液态玻璃：主题预设、背景渲染、日夜开关与偏好持久化。

对比度按 WCAG 公式逐预设检查：换主题不能把字变得看不清。
背景和开关用临时目录里的图片，不碰真实用户文件。
"""

import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ui import glass, theme
from ui.daynight import DayNightSwitch

try:
    from PIL import Image
except Exception:
    Image = None


def luminance(hex_color):
    text = str(hex_color).lstrip("#")
    parts = [int(text[index:index + 2], 16) / 255.0 for index in (0, 2, 4)]

    def channel(value):
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = (channel(item) for item in parts)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(first, second):
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class ThemePresetTests(unittest.TestCase):
    def setUp(self):
        self.original = (theme.preset(), theme.active(), theme.accent())
        self.addCleanup(
            lambda: (
                theme.set_preset(self.original[0], mode=self.original[1]),
                theme.set_accent(self.original[2]),
            )
        )

    def test_every_preset_has_day_and_night_gradients(self):
        self.assertEqual(len(theme.PRESETS), 7)
        for key, item in theme.PRESETS.items():
            for mode in ("night", "day"):
                stops = item[mode]
                self.assertEqual(len(stops), 3, key)
                for color in stops:
                    self.assertRegex(color, r"^#[0-9a-f]{6}$", key)

    def test_switching_preset_changes_glow_and_accent(self):
        theme.set_preset("ocean", mode="dark")
        self.assertEqual(theme.preset(), "ocean")
        self.assertEqual(theme.accent(), "sky")
        self.assertEqual(theme.GLOW, "#2fd0ff")
        theme.set_preset("not-a-theme", mode="dark")
        self.assertEqual(theme.preset(), theme.DEFAULT_PRESET)

    def test_day_night_keeps_the_preset(self):
        theme.set_preset("sakura", mode="dark")
        theme.apply("light")
        self.assertEqual(theme.preset(), "sakura")
        self.assertEqual(theme.GLOW, "#ffb3d1")
        self.assertEqual(theme.accent(), "rose")

    def test_every_preset_passes_contrast_in_both_modes(self):
        checks = (
            ("TEXT", "BG"),
            ("TEXT", "PANEL"),
            ("TEXT", "PANEL_2"),
            ("MUTED", "PANEL"),
            ("MUTED", "BG"),
            ("PLACEHOLDER", "FIELD"),
            ("ON_ACCENT", "ACCENT"),
            ("TEXT", "BUBBLE_ME"),
            ("TEXT", "BUBBLE_SYS"),
        )
        for key in theme.PRESET_NAMES:
            for mode in ("dark", "light"):
                theme.set_preset(key, mode=mode)
                for fg, bg in checks:
                    got = contrast(getattr(theme, fg), getattr(theme, bg))
                    self.assertGreaterEqual(
                        got, 4.5, "%s/%s %s on %s = %.2f" % (key, mode, fg, bg, got)
                    )


@unittest.skipUnless(Image is not None, "没有 Pillow")
class GlassTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)

    def make_image(self, name="bg.png", size=(640, 360), color=(80, 120, 200)):
        path = self.base / name
        Image.new("RGB", size, color).save(path)
        return path

    def test_gradient_renders_at_the_requested_size(self):
        picture = glass.render((320, 200))
        self.assertIsNotNone(picture)
        self.assertEqual(picture.size, (320, 200))
        self.assertEqual(picture.mode, "RGB")

    def test_a_user_photo_is_used(self):
        path = self.make_image()
        picture = glass.render((320, 200), image_path=str(path))
        self.assertIsNotNone(picture)
        self.assertEqual(picture.size, (320, 200))

    def test_a_missing_photo_falls_back_to_the_gradient(self):
        picture = glass.render((320, 200), image_path=str(self.base / "nope.png"))
        self.assertIsNotNone(picture, "背景图坏了也必须能画，不能把窗口搞空")

    def test_inspect_rejects_bad_files(self):
        tiny = self.make_image("tiny.png", size=(64, 64))
        broken = self.base / "broken.png"
        broken.write_bytes(b"not an image")
        self.assertFalse(glass.inspect_image(tiny)["ok"])
        self.assertFalse(glass.inspect_image(broken)["ok"])
        self.assertFalse(glass.inspect_image(self.base / "missing.png")["ok"])
        self.assertTrue(glass.inspect_image(self.make_image("ok.png"))["ok"])

    def test_motion_does_not_change_the_size(self):
        first = glass.render((200, 120), phase=0.0)
        second = glass.render((200, 120), phase=1.8)
        self.assertEqual(first.size, second.size)


class DayNightSwitchTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.picked = []

    def test_click_switches_and_reports(self):
        switch = DayNightSwitch(self.root, mode="dark", command=self.picked.append)
        self.assertEqual(switch.mode(), "dark")
        switch._clicked()
        self.assertEqual(self.picked, ["light"])
        self.assertEqual(switch.mode(), "light")

    def test_set_mode_without_animation(self):
        switch = DayNightSwitch(self.root, mode="light")
        switch.set_mode("dark")
        self.assertEqual(switch.mode(), "dark")
        self.assertEqual(switch._value, 1.0)
        switch.set_mode("light")
        self.assertEqual(switch._value, 0.0)

    def test_animation_finishes_at_the_target(self):
        import time

        switch = DayNightSwitch(self.root, mode="dark")
        switch.set_mode("light", animate=True)
        for _ in range(40):
            self.root.update()
            time.sleep(0.02)
        self.assertAlmostEqual(switch._value, 0.0, places=2)


class PetWiringTests(unittest.TestCase):
    """宿主接线：背景、缩放、动效、日夜、预设都写进偏好。"""

    def setUp(self):
        try:
            self.root = tk.Tk()
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)

        from app.main import build_agent
        from database.db import Database
        from logkit.logger import get_logger
        from model.base import ChatDelta, ChatResult, ModelProvider, Usage
        from security.config import Config
        from ui.desktop import DesktopPet

        class Stub(ModelProvider):
            name = "stub"

            def is_available(self):
                return True

            def chat(self, messages, tools=None, temperature=0.7):
                return ChatResult(ok=True, content="在。", usage=Usage(), model="stub")

            def stream(self, messages, tools=None, temperature=0.7):
                yield ChatDelta(done=True)

        config = Config(load_env=False)
        config.data_root = self.base
        config.data_dir = self.base / "data"
        config.log_dir = self.base / "logs"
        config.db_path = self.base / "t.sqlite3"
        config.characters_dir = self.base / "characters"
        config.live2d_dir = self.base / "live2d"
        config.devices_dir = self.base / "devices"
        config.live2d_render = False
        config.ensure_dirs()
        database = Database(config.db_path)
        database.migrate()
        self.config = config
        agent, memory = build_agent(config, database, Stub(), get_logger())
        self.pet = DesktopPet(agent, config, memory=memory, root=self.root)
        self.pet.show_chat()
        self.root.update()

    def test_background_is_remembered_and_clearable(self):
        path = self.base / "wall.png"
        Image.new("RGB", (800, 450), (40, 60, 120)).save(path)
        result = self.pet.set_background(str(path))
        self.assertTrue(result["ok"], result)
        info = self.pet.background_info()
        self.assertTrue(info["exists"])
        self.assertEqual(info["path"], str(path))
        self.pet.clear_background()
        self.assertEqual(self.pet.background_info()["path"], "")

    def test_zoom_and_motion_are_persisted(self):
        self.assertEqual(self.pet.set_background_zoom(1.3), 1.3)
        self.assertEqual(self.pet.set_background_motion("low"), "low")
        self.assertEqual(self.pet.background_motion(), "low")
        self.assertEqual(self.pet.set_background_motion("nonsense"), "off")

    def test_background_position_blur_and_darkening_are_persisted(self):
        self.assertAlmostEqual(self.pet.set_background_param("x", 0.3), 0.3, places=2)
        self.assertAlmostEqual(self.pet.set_background_param("y", 0.7), 0.7, places=2)
        self.assertEqual(self.pet.set_background_param("blur", 18), 18)
        self.assertAlmostEqual(self.pet.set_background_param("dim", 0.2), 0.2, places=2)
        info = self.pet.background_info()
        self.assertAlmostEqual(info["x"], 0.3, places=2)
        self.assertAlmostEqual(info["y"], 0.7, places=2)
        self.assertEqual(info["blur"], 18)
        self.assertAlmostEqual(info["dim"], 0.2, places=2)

    def test_day_night_and_preset_switch_without_touching_data(self):
        before = self.pet.config.companion_model
        self.pet.set_day_night("light")
        self.assertEqual(theme.active(), "light")
        self.assertEqual(self.pet._preference(theme.THEME_PREFERENCE), "light")
        self.pet.set_theme_preset("ocean")
        self.assertEqual(theme.preset(), "ocean")
        self.assertEqual(self.pet._preference(theme.PRESET_PREFERENCE), "ocean")
        self.assertEqual(self.pet.config.companion_model, before)
        self.pet.set_day_night("dark")
        self.assertEqual(theme.active(), "dark")

    def test_rebuilding_leaves_no_stale_callbacks(self):
        """换主题会重建界面：旧回调必须拆干净，否则会冒出 bad window path name。"""
        import time

        errors = []
        self.root.report_callback_exception = lambda exc, value, tb: errors.append(value)
        self.pet.set_day_night("light")
        self.pet.set_theme_preset("ocean")
        self.pet.set_day_night("dark")
        for _ in range(12):
            self.root.update()
            time.sleep(0.03)
        self.assertEqual(errors, [])
        self.assertNotIn("出错", self.pet.chat.state.cget("text"))

    def test_nav_hover_expands_a_pinned_collapsed_sidebar(self):
        import time
        from unittest import mock

        chat = self.pet.chat
        chat.toggle_nav(False)
        self.assertFalse(chat.nav_pinned)
        chat._nav_enter()
        self.assertTrue(chat._nav_hover)
        with mock.patch.object(chat.nav, "winfo_pointerxy", return_value=(-100, -100)), \
             mock.patch.object(chat.nav, "winfo_containing", return_value=None):
            chat._nav_leave()
            for _ in range(20):
                self.root.update()
                time.sleep(0.02)
        self.assertFalse(chat._nav_hover)
        chat.toggle_nav(True)
        self.assertTrue(chat.nav_pinned)


if __name__ == "__main__":
    unittest.main()
