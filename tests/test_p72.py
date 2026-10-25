"""阶段 P72 验收：V7 阶段 1 的工作台尺寸与消息区排版。

覆盖：默认窗口按屏幕比例放大、旧的小默认值按「从没调过」处理、用户量过的尺寸不动、
工作台宽度下输入框留三行、宽窗口下消息两侧留白、气泡只包住文字、自己说的话另占一栏靠右、
窄窗口退回两侧对齐、留白配色跟着主题走、代码块不多铺空行、代码块前后的空行不进气泡。
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
from database.db import Database
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatDelta, ChatResult, ModelProvider, Usage
from security.config import Config
from ui import theme
from ui.chat_view import ChatView
from ui.desktop import CARD_HEIGHT, CARD_WIDTH, DesktopPet


class StubProvider(ModelProvider):
    name = "stub"

    def is_available(self):
        return True

    def chat(self, messages, tools=None, temperature=0.7):
        return ChatResult(ok=True, content="在的。", usage=Usage(5, 3), model="stub")

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


class TkTestCase(unittest.TestCase):
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


class WorkbenchSizeTests(TkTestCase):
    """窗口本身别局促，但也不能铺满屏幕。"""

    def test_the_first_window_is_much_bigger_than_the_old_default(self):
        pet = self.build_pet()
        width, height = pet._default_card_size()
        self.assertGreater(width, CARD_WIDTH)
        self.assertGreater(height, CARD_HEIGHT)

    def test_the_first_window_leaves_room_around_it(self):
        pet = self.build_pet()
        width, height = pet._default_card_size()
        self.assertLessEqual(width, 1600)
        self.assertLessEqual(height, 1040)
        self.assertGreaterEqual(width, int(self.root.winfo_screenwidth() * 0.5))

    def test_the_old_small_default_counts_as_never_resized(self):
        pet = self.build_pet()
        pet._save_preference("chat.size", "%dx%d" % (CARD_WIDTH, CARD_HEIGHT))
        self.assertEqual(pet._saved_card_size(), pet._default_card_size())

    def test_a_size_the_user_picked_is_kept(self):
        pet = self.build_pet()
        pet._save_preference("chat.size", "1180x760")
        self.assertEqual(pet._saved_card_size(), (1180, 760))

    def test_a_size_below_the_spec_minimum_is_lifted(self):
        pet = self.build_pet()
        pet._save_preference("chat.size", "900x700")
        self.assertEqual(
            pet._saved_card_size(), (DesktopPet.MIN_CARD_WIDTH, 700)
        )

    def test_a_screen_that_cannot_be_measured_falls_back_to_the_old_size(self):
        pet = self.build_pet()
        with mock.patch.object(self.root, "winfo_screenwidth", return_value=1):
            self.assertEqual(pet._default_card_size(), (CARD_WIDTH, CARD_HEIGHT))


class MessageLayoutTests(TkTestCase):
    """消息区：留白、气泡、代码块各就各位。"""

    def test_the_bubble_does_not_possess_the_whole_window(self):
        pet = self.build_pet()
        chat = pet.chat
        self.assertLess(ChatView.ME_BUBBLE_WIDTH, ChatView.MAX_TEXT_WIDTH)

    def test_a_wide_window_insets_the_bubbles(self):
        pet = self.build_pet()
        chat = pet.chat
        gutter = chat._apply_text_width(1200)
        self.assertGreater(gutter, 0)
        transcript = chat.transcript
        pet_left = int(transcript.tag_cget("pet", "lmargin1"))
        me_left = int(transcript.tag_cget("me", "lmargin1"))
        self.assertEqual(pet_left, theme.BUBBLE_PAD + gutter)
        self.assertGreater(me_left, pet_left)

    def test_the_bubble_hugs_the_text_not_the_window(self):
        pet = self.build_pet()
        chat = pet.chat
        chat._apply_text_width(1200)
        transcript = chat.transcript
        me_left = int(transcript.tag_cget("me", "lmargin1"))
        me_right = int(transcript.tag_cget("me", "rmargin"))
        self.assertEqual(1200 - me_left - me_right, ChatView.ME_BUBBLE_WIDTH)
        pet_left = int(transcript.tag_cget("pet", "lmargin1"))
        pet_right = int(transcript.tag_cget("pet", "rmargin"))
        self.assertLessEqual(1200 - pet_left - pet_right, ChatView.MAX_TEXT_WIDTH)

    def test_a_narrow_window_keeps_both_sides_aligned(self):
        pet = self.build_pet()
        chat = pet.chat
        chat._apply_text_width(380)
        transcript = chat.transcript
        self.assertEqual(int(transcript.tag_cget("me", "lmargin1")), theme.BUBBLE_PAD)
        self.assertEqual(int(transcript.tag_cget("pet", "lmargin1")), theme.BUBBLE_PAD)

    def test_the_margin_colour_follows_the_theme(self):
        pet = self.build_pet()
        chat = pet.chat
        transcript = chat.transcript
        # 一条消息都还没有的时候，留白跟着主背景走，首屏不留空框。
        self.assertTrue(chat.is_empty_shown())
        for tag in list(theme.MESSAGE_STYLES) + ["code"]:
            self.assertEqual(
                str(transcript.tag_cget(tag, "lmargincolor")).lower(),
                theme.BG.lower(),
                tag,
            )
            self.assertEqual(
                str(transcript.tag_cget(tag, "rmargincolor")).lower(),
                theme.BG.lower(),
                tag,
            )
        # 消息上屏之后回到输入区底色，气泡两侧不留接缝。
        chat.append("pet", "在。")
        self.assertFalse(chat.is_empty_shown())
        for tag in list(theme.MESSAGE_STYLES) + ["code"]:
            self.assertEqual(
                str(transcript.tag_cget(tag, "lmargincolor")).lower(),
                theme.FIELD.lower(),
                tag,
            )

    def test_the_configure_hook_sets_the_workbench_rules(self):
        pet = self.build_pet()
        chat = pet.chat
        with mock.patch.object(chat.parent, "winfo_width", return_value=1600):
            chat._on_configure()
        self.assertEqual(int(str(chat.input.cget("height"))), 3)
        self.assertGreater(int(chat.transcript.tag_cget("me", "lmargin1")), theme.BUBBLE_PAD)
        with mock.patch.object(chat.parent, "winfo_width", return_value=420):
            chat._on_configure()
        self.assertEqual(int(chat.transcript.tag_cget("me", "lmargin1")), theme.BUBBLE_PAD)
        self.assertEqual(int(str(chat.input.cget("height"))), 1)

    def test_the_code_block_does_not_tag_its_newline(self):
        pet = self.build_pet()
        pet.chat.append_code("print(1)", "python")
        transcript = pet.chat.transcript
        ranges = list(transcript.tag_ranges("code"))
        self.assertEqual(len(ranges), 2)
        self.assertEqual(transcript.get(ranges[0], ranges[1]), "print(1)")

    def test_blank_lines_around_a_code_block_stay_out_of_the_bubble(self):
        pet = self.build_pet()
        pet.chat.append_markdown("pet", "开头\n\n```python\nprint(1)\n```\n\n结尾")
        transcript = pet.chat.transcript
        ranges = list(transcript.tag_ranges("pet"))
        self.assertTrue(ranges)
        for index in range(0, len(ranges), 2):
            segment = transcript.get(ranges[index], ranges[index + 1])
            self.assertFalse(segment.startswith("\n"), repr(segment))
            self.assertFalse(segment.endswith("\n"), repr(segment))


if __name__ == "__main__":
    unittest.main()
