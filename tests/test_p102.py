"""阶段 D 验收：主工作台的质量底线。

这一份盯三件事：
  1. 深色和浅色两套配色的对比度都达标（正文、次要文字、占位提示、按钮字、状态色）。
  2. 两套配色的键完全对齐，切主题不会少一个颜色；浅色也不是把深色反过来。
  3. 右侧辅助面板能显示"正在等你授权什么"，问完把原来的内容还回去；
     窄窗口下面板自己收起来，不挤中央区。

界面部分用临时目录和临时数据库，不碰真实用户数据。
"""

import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.context import ContextManager  # noqa: E402
from agent.core import AgentCore  # noqa: E402
from behavior.manager import BehaviorManager  # noqa: E402
from database.db import Database  # noqa: E402
from memory.manager import MemoryManager  # noqa: E402
from memory.repository import MemoryRepository  # noqa: E402
from model.base import ChatDelta, ChatResult, ModelProvider, Usage  # noqa: E402
from persona.interface import DefaultPersonaProvider  # noqa: E402
from persona.training import LocalTrainingCollector  # noqa: E402
from security.config import Config  # noqa: E402
from ui import theme  # noqa: E402
from ui.desktop import DesktopPet  # noqa: E402

# 每一对：(名字, 前景键, 背景键, 最低对比度)。
# 正文和控件文字按 4.5（WCAG AA 常规文字），纯装饰性的点缀色按 3.0。
PAIRS = (
    ("正文/主背景", "TEXT", "BG", 4.5),
    ("正文/卡片", "TEXT", "PANEL", 4.5),
    ("正文/次级面板", "TEXT", "PANEL_2", 4.5),
    ("次要文字/主背景", "MUTED", "BG", 4.5),
    ("次要文字/卡片", "MUTED", "PANEL", 4.5),
    ("占位提示/输入框", "PLACEHOLDER", "FIELD", 4.5),
    ("按钮字/强调色", "ON_ACCENT", "ACCENT", 4.5),
    ("强调色/主背景", "ACCENT", "BG", 3.0),
    ("淡紫/主背景", "LILAC", "BG", 3.0),
    ("错误色/卡片", "ERROR", "PANEL", 4.5),
    ("成功色/卡片", "OK", "PANEL", 4.5),
    ("警告色/卡片", "WARN", "PANEL", 4.5),
    ("正文/我的气泡", "TEXT", "BUBBLE_ME", 4.5),
    ("正文/桌宠气泡", "TEXT", "BUBBLE_PET", 4.5),
    ("正文/系统气泡", "TEXT", "BUBBLE_SYS", 4.5),
    ("正文/代码块", "TEXT", "CODE_BG", 4.5),
    ("正文/侧栏", "TEXT", "SIDEBAR_BG", 4.5),
    ("正文/顶栏", "TEXT", "TOPBAR_BG", 4.5),
    ("天蓝/主背景", "SKY", "BG", 3.0),
    ("浅青/主背景", "CYAN", "BG", 3.0),
    ("粉红/主背景", "PINK", "BG", 3.0),
    ("白/主背景", "WHITE", "BG", 3.0),
    ("选区文字/选区底", "TEXT", "SELECT_BG", 4.5),
)


def _channel(value):
    value = value / 255.0
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def luminance(hexcolor):
    text = str(hexcolor).lstrip("#")
    if len(text) != 6:
        raise ValueError("颜色要写成 #rrggbb：%r" % (hexcolor,))
    red, green, blue = (int(text[index:index + 2], 16) for index in (0, 2, 4))
    return 0.2126 * _channel(red) + 0.7152 * _channel(green) + 0.0722 * _channel(blue)


def contrast(first, second):
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class ContrastTests(unittest.TestCase):
    """两套主题的对比度是硬指标，不是观感问题。"""

    def test_every_pair_meets_its_bar_in_both_themes(self):
        for name, palette in (("深色", theme.DARK), ("浅色", theme.LIGHT)):
            for label, foreground, background, bar in PAIRS:
                with self.subTest(theme=name, pair=label):
                    got = contrast(palette[foreground], palette[background])
                    self.assertGreaterEqual(
                        round(got, 2),
                        bar,
                        "%s 的「%s」对比度只有 %.2f，要求 %.1f"
                        % (name, label, got, bar),
                    )

    def test_both_palettes_define_the_same_keys(self):
        self.assertEqual(set(theme.DARK), set(theme.LIGHT), "少一个键就会切主题时报错")

    def test_light_is_not_a_flipped_dark_theme(self):
        """浅色底要亮、字要深；深色反过来。不是把两边调个个儿。"""
        self.assertGreater(luminance(theme.LIGHT["BG"]), luminance(theme.LIGHT["TEXT"]))
        self.assertLess(luminance(theme.DARK["BG"]), luminance(theme.DARK["TEXT"]))
        self.assertNotEqual(theme.LIGHT["BORDER"], theme.DARK["BORDER"])
        self.assertNotEqual(theme.LIGHT["ACCENT"], theme.DARK["ACCENT"])

    def test_switching_actually_repaints(self):
        try:
            self.assertEqual(theme.apply("light"), "light")
            self.assertEqual(theme.TEXT, theme.LIGHT["TEXT"])
            self.assertEqual(theme.apply("dark"), "dark")
            self.assertEqual(theme.TEXT, theme.DARK["TEXT"])
        finally:
            theme.apply("dark")


class StubProvider(ModelProvider):
    name = "stub"

    def is_available(self):
        return True

    def chat(self, messages, tools=None, temperature=0.7):
        return ChatResult(ok=True, content="在的。", usage=Usage(), model="stub")

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


class PanelTests(unittest.TestCase):
    """右侧面板：待授权的请求看得见，问完还原本来的内容；窄窗口自动收起。"""

    def setUp(self):
        try:
            self.root = tk.Tk()
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        self.root.withdraw()
        self.addCleanup(self._destroy)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        config = Config(load_env=False)
        config.data_dir = base
        config.log_dir = base / "logs"
        config.db_path = base / "t.sqlite3"
        config.characters_dir = base / "characters"
        config.live2d_dir = base / "live2d"
        config.devices_dir = base / "devices"
        database = Database(base / "t.sqlite3")
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        agent = AgentCore(
            StubProvider(),
            memory,
            ContextManager(token_budget=800),
            persona=DefaultPersonaProvider(),
            training=LocalTrainingCollector(database),
        )
        self.pet = DesktopPet(
            agent,
            config,
            memory=memory,
            behavior=BehaviorManager(persona=agent.persona),
            root=self.root,
        )
        self.addCleanup(self._close)

    def _destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _close(self):
        try:
            self.pet.close_panel()
        except Exception:
            pass

    def test_a_pending_authorization_shows_up_in_the_panel(self):
        self.pet.chat.set_panel_text("正在跑的步骤：读文件")
        self.pet._show_pending_authorization("write_file", "HIGH", "要把结果写进报告.md")
        shown = self.pet.chat.panel_body()
        self.assertIn("等待你的授权", shown)
        self.assertIn("write_file", shown)
        self.assertIn("HIGH", shown)
        self.assertIn("结果写进报告.md", shown)

    def test_the_panel_goes_back_to_what_it_showed_before(self):
        self.pet.chat.set_panel_text("正在跑的步骤：读文件")
        self.pet._show_pending_authorization("write_file", "HIGH", "写文件")
        self.pet._clear_pending_authorization()
        self.assertIn("正在跑的步骤：读文件", self.pet.chat.panel_body())
        self.assertNotIn("等待你的授权", self.pet.chat.panel_body())

    def test_a_narrow_window_keeps_the_panel_out_of_the_way(self):
        chat = self.pet.chat
        chat.set_panel(True)
        with mock.patch.object(type(chat.parent), "winfo_width", lambda self: 900):
            chat._apply_panel()
            self.assertEqual(str(chat.panel.winfo_manager()), "", "窄窗口下不该占一列")
        with mock.patch.object(type(chat.parent), "winfo_width", lambda self: 1400):
            chat._apply_panel()
            self.assertEqual(str(chat.panel.winfo_manager()), "pack", "宽窗口才摆出来")

    def test_the_panel_toggle_is_a_real_switch(self):
        chat = self.pet.chat
        before = chat.panel_is_shown()
        self.assertEqual(chat.toggle_panel(), not before)
        self.assertEqual(chat.toggle_panel(), before)


if __name__ == "__main__":
    unittest.main()
