"""阶段 P73 验收：V7 阶段 2 的强调色与减少动态效果。

覆盖：强调色预设齐全、切主题不丢强调色、深浅色各有对应版本、认不出的名字就地退回、
偏好能存能读、界面按钮跟着换色；减少动态效果默认关闭、勾选后存进偏好并通知渲染进程、
渲染进程没起来时先记住、起来后补发、渲染子进程认识这条命令。
"""

import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.context import ContextManager
from agent.core import AgentCore
from avatar import web2d, web2d_renderer
from database.db import Database
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatDelta, ChatResult, ModelProvider, Usage
from security.config import Config
from ui import theme
from ui.desktop import DesktopPet

RUNTIME = ROOT / "resources" / "live2d"


class StubProvider(ModelProvider):
    name = "stub"

    def is_available(self):
        return True

    def chat(self, messages, tools=None, temperature=0.7):
        return ChatResult(ok=True, content="在的。", usage=Usage(5, 3), model="stub")

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


class AccentTests(unittest.TestCase):
    """强调色只换自己的那一档颜色，其余照旧。"""

    def setUp(self):
        theme.apply("dark", accent_name=theme.DEFAULT_ACCENT)
        self.addCleanup(lambda: theme.apply("dark", accent_name=theme.DEFAULT_ACCENT))

    def test_every_preset_has_a_name_and_a_label(self):
        self.assertEqual(set(theme.ACCENT_NAMES), set(theme.ACCENTS))
        for name in theme.ACCENT_NAMES:
            self.assertTrue(theme.ACCENT_LABELS.get(name), name)

    def test_two_themes_have_two_versions_of_the_same_accent(self):
        for name, (dark, light) in theme.ACCENTS.items():
            self.assertNotEqual(dark, light, name)

    def test_picking_one_changes_the_colour_right_away(self):
        self.assertEqual(theme.ACCENT, theme.ACCENTS[theme.DEFAULT_ACCENT][0])
        theme.set_accent("mint")
        self.assertEqual(theme.ACCENT, theme.ACCENTS["mint"][0])

    def test_the_light_theme_uses_the_light_version(self):
        theme.apply("light", accent_name="rose")
        self.assertEqual(theme.ACCENT, theme.ACCENTS["rose"][1])

    def test_switching_the_theme_keeps_the_accent(self):
        theme.apply("dark", accent_name="amber")
        theme.apply("light")
        self.assertEqual(theme.accent(), "amber")
        self.assertEqual(theme.ACCENT, theme.ACCENTS["amber"][1])

    def test_a_name_nobody_knows_falls_back_quietly(self):
        self.assertEqual(theme.set_accent("not-a-colour"), theme.DEFAULT_ACCENT)
        self.assertEqual(theme.set_accent(None), theme.DEFAULT_ACCENT)
        self.assertEqual(theme.set_accent("  MINT  "), "mint")

    def test_the_style_tables_follow_the_accent(self):
        theme.set_accent("violet")
        self.assertEqual(theme.MESSAGE_STYLES["notice"][0], theme.ACCENT)
        self.assertEqual(theme.STATE_COLORS["busy"], theme.ACCENT)

    def test_saved_preferences_bring_back_the_theme_and_the_accent(self):
        class Prefs:
            def __init__(self, values):
                self.values = values

            def get_preference(self, key):
                return self.values.get(key)

        prefs = Prefs({theme.THEME_PREFERENCE: "light", theme.ACCENT_PREFERENCE: "amber"})
        self.assertEqual(theme.apply_saved(prefs), "light")
        self.assertEqual(theme.accent(), "amber")

    def test_nothing_saved_falls_back_to_the_defaults(self):
        self.assertEqual(theme.apply_saved(None), "light")
        self.assertEqual(theme.accent(), theme.DEFAULT_ACCENT)


class MotionCommandTests(unittest.TestCase):
    """「减少动态效果」一路走到页面控制器。"""

    def renderer(self):
        r = web2d_renderer.Renderer.__new__(web2d_renderer.Renderer)
        r.state = {"ready": True, "visible": True, "scale": 1.0}
        r.forwarded = []
        r.quit = mock.Mock()
        r._forward = lambda name, script: r.forwarded.append((name, script))
        return r

    def test_the_renderer_understands_the_command(self):
        r = self.renderer()
        self.assertTrue(r.handle({"cmd": "reduced-motion", "value": True}))
        name, script = r.forwarded[0]
        self.assertEqual(name, "motion")
        self.assertIn("Pet.setMotion(false)", script)

    def test_turning_it_back_off_is_the_other_way_round(self):
        r = self.renderer()
        r.handle({"cmd": "reduced-motion", "value": False})
        self.assertIn("Pet.setMotion(true)", r.forwarded[0][1])

    def test_the_host_sends_it_while_the_render_is_up(self):
        with mock.patch.object(web2d, "_webview_ready", return_value=True):
            host = web2d.WebLive2DHost(runtime=RUNTIME)
        sent = []
        host._send = lambda message: sent.append(message)
        host.running = lambda: True
        self.assertTrue(host.set_reduced_motion(True))
        self.assertEqual(sent[-1], {"cmd": "reduced-motion", "value": True})
        self.assertTrue(host.reduced_motion())

    def test_the_host_remembers_it_before_the_render_starts(self):
        with mock.patch.object(web2d, "_webview_ready", return_value=True):
            host = web2d.WebLive2DHost(runtime=RUNTIME)
        self.assertFalse(host.reduced_motion())
        self.assertTrue(host.set_reduced_motion(True))
        self.assertTrue(host.reduced_motion())


class MotionSettingTests(unittest.TestCase):
    """界面上的那个勾：默认不勾，勾上要存住，并且告诉渲染进程。"""

    def setUp(self):
        try:
            self.root = tk.Tk()
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        self.root.withdraw()
        self.addCleanup(self._safe_destroy)
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

    def _safe_destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def build_pet(self):
        database = Database(Path(self.tmp.name) / "t.sqlite3")
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        agent = AgentCore(StubProvider(), memory, ContextManager(token_budget=800))
        return DesktopPet(agent, self.config, memory=memory, root=self.root)

    def test_the_default_is_the_full_show(self):
        pet = self.build_pet()
        self.assertFalse(pet.motion_reduced())

    def test_turning_it_on_is_remembered(self):
        pet = self.build_pet()
        pet.set_motion(True)
        self.assertTrue(pet.motion_reduced())
        self.assertEqual(pet._preference(theme.MOTION_PREFERENCE), "reduced")
        pet.set_motion(False)
        self.assertFalse(pet.motion_reduced())

    def test_the_checkbox_drives_the_preference(self):
        pet = self.build_pet()
        pet.motion_var = tk.BooleanVar(value=True)
        pet._save_motion()
        self.assertTrue(pet.motion_reduced())

    def test_the_host_is_told_about_it(self):
        pet = self.build_pet()
        host = mock.Mock()
        host.reduced_motion.return_value = False
        pet.live2d = host
        pet.set_motion(True)
        host.set_reduced_motion.assert_called_with(True)
        del pet.live2d

    def test_no_render_process_is_not_an_error(self):
        pet = self.build_pet()
        self.assertIsNone(pet.live2d)
        self.assertTrue(pet.set_motion(True))

    def test_the_accent_is_saved_and_applied(self):
        pet = self.build_pet()
        pet.set_accent("mint")
        self.assertEqual(theme.accent(), "mint")
        self.assertEqual(pet._preference(theme.ACCENT_PREFERENCE), "mint")
        self.assertEqual(pet._accent_row(), "薄荷（当前 mint）")


if __name__ == "__main__":
    unittest.main()
