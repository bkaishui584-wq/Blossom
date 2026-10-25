"""阶段 P44 验收：聊天消息分类与设置中心九分区。

覆盖：普通回复 / 主动提醒 / 任务状态 / 系统通知 / 错误提示 五类消息互不混淆、
任务只在值得看的几步留一行、取消正在跑的任务会说一声、清空当前对话不动长期记忆、
设置按九个类别分区、置顶与位置这类设置真的生效且重启后记得住、开发者面板默认收起、
开发者信息不含密钥。
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
from agent.core import AgentCore, AgentReply
from agent.task import TaskState
from database.db import Database
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatDelta, ChatResult, ModelProvider, Usage
from security.config import Config
from ui import theme
from ui.desktop import CARD_HEIGHT, CARD_WIDTH, DesktopPet


class StubProvider(ModelProvider):
    name = "stub"

    def __init__(self, available=True, reply="在的。"):
        self.available = available
        self.reply = reply

    def is_available(self):
        return self.available

    def chat(self, messages, tools=None, temperature=0.7):
        return ChatResult(
            ok=True,
            content=self.reply,
            usage=Usage(prompt_tokens=5, completion_tokens=3),
            model="stub",
        )

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


def label_texts(widget):
    texts = []
    for child in widget.winfo_children():
        try:
            value = child.cget("text")
        except Exception:
            value = ""
        if value:
            texts.append(str(value))
        texts.extend(label_texts(child))
    return texts


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
        self.config.voices_dir = base / "voices"
        self.config.characters_dir = base / "characters"
        self.config.live2d_dir = base / "live2d"
        self.config.devices_dir = base / "devices"
        self.config.tray = False

    def _safe_destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def build_pet(self, **kwargs):
        database = Database(Path(self.tmp.name) / "t.sqlite3")
        database.migrate()
        memory = MemoryManager(MemoryRepository(database), **kwargs)
        agent = AgentCore(StubProvider(), memory, ContextManager(token_budget=800))
        pet = DesktopPet(agent, self.config, memory=memory, root=self.root)
        return pet, memory


class MessageKindTests(TkTestCase):
    """五类消息在视觉上必须分得开，行为上互不串味。"""

    def test_theme_covers_every_message_role(self):
        for tag in ("me", "pet", "err", "hint", "notice", "proactive", "task"):
            self.assertIn(tag, theme.MESSAGE_STYLES)

    def test_new_roles_are_not_the_normal_reply_bubble(self):
        pet_bg = theme.MESSAGE_STYLES["pet"][1]
        for tag in ("proactive", "task"):
            self.assertNotEqual(theme.MESSAGE_STYLES[tag][1], pet_bg)
            self.assertEqual(theme.MESSAGE_STYLES[tag][2], "left")

    def test_proactive_reply_uses_the_proactive_bubble(self):
        pet, _ = self.build_pet()
        reply = AgentReply(ok=True, content="该起来动一动啦。")
        pet._apply_proactive(reply)
        self.assertTrue(pet.transcript.tag_ranges("proactive"))
        self.assertIn("该起来动一动啦。", pet.chat.history_text())

    def test_task_notice_only_for_meaningful_steps(self):
        pet, _ = self.build_pet()
        self.assertIsNone(pet._note_task_state({"task_state": TaskState.NEW.value}))
        self.assertIsNone(pet._note_task_state({"task_state": TaskState.RUNNING.value}))
        self.assertIsNone(pet._note_task_state({}))
        self.assertIsNotNone(pet._note_task_state({"task_state": TaskState.COMPLETED.value}))
        self.assertTrue(pet.transcript.tag_ranges("task"))

    def test_pending_confirmation_tells_the_user_to_confirm(self):
        pet, _ = self.build_pet()
        pet._note_task_state({"task_state": TaskState.PENDING_CONFIRMATION.value})
        self.assertIn("弹窗", pet.chat.history_text())

    def test_cancel_marks_only_a_running_task(self):
        pet, _ = self.build_pet()
        pet._cancel()
        self.assertNotIn("已取消", pet.chat.history_text())
        pet.agent.task_state = TaskState.RUNNING.value
        pet._cancel()
        self.assertIn("已取消", pet.chat.history_text())

    def test_error_reply_uses_the_error_bubble(self):
        pet, _ = self.build_pet()
        pet._apply_reply(AgentReply(ok=False, error="模型连不上。"))
        self.assertTrue(pet.transcript.tag_ranges("err"))
        self.assertIn("模型连不上。", pet.chat.history_text())

    def test_clearing_the_chat_keeps_long_term_memory(self):
        pet, memory = self.build_pet()
        memory.remember("用户住在杭州", kind="long_term")
        pet._append_message("你", "你好", "me")
        with mock.patch("ui.desktop.messagebox.askyesno", return_value=True):
            self.assertTrue(pet._clear_chat())
        self.assertEqual(pet.chat.history_text().strip(), "")
        kinds = [row["kind"] for row in memory.browse()]
        self.assertIn("long_term", kinds)


class ChatWindowStateTests(TkTestCase):
    """聊天浮层的大小和开合状态要能跨重启恢复。"""

    def test_card_size_is_remembered_after_resizing(self):
        pet, _ = self.build_pet()
        pet._card_size = (400, 500)
        pet._resize = (0, 0, 344, 452)
        self.assertTrue(pet._resize_done())
        self.assertEqual(pet._preference("chat.size"), "400x500")
        self.assertFalse(pet._resize_done())

    def test_saved_size_is_clamped_to_the_minimum(self):
        pet, memory = self.build_pet()
        memory.set_preference("chat.size", "10x10")
        self.assertEqual(
            pet._saved_card_size(),
            (DesktopPet.MIN_CARD_WIDTH, DesktopPet.MIN_CARD_HEIGHT),
        )

    def test_broken_size_preference_falls_back(self):
        pet, memory = self.build_pet()
        memory.set_preference("chat.size", "乱七八糟")
        self.assertEqual(pet._saved_card_size(), pet._default_card_size())

    def test_the_old_small_default_is_treated_as_never_resized(self):
        pet, memory = self.build_pet()
        memory.set_preference("chat.size", "%dx%d" % (CARD_WIDTH, CARD_HEIGHT))
        self.assertEqual(pet._saved_card_size(), pet._default_card_size())

    def test_the_new_default_is_a_big_workbench_but_not_the_whole_screen(self):
        pet, _ = self.build_pet()
        width, height = pet._default_card_size()
        self.assertGreater(width, CARD_WIDTH)
        self.assertGreater(height, CARD_HEIGHT)
        self.assertLessEqual(width, 1600)
        self.assertLessEqual(height, 1040)
        self.assertLess(width, pet.root.winfo_screenwidth())

    def test_open_state_survives_a_restart(self):
        pet, memory = self.build_pet()
        self.assertFalse(pet._chat_was_open())
        pet.show_chat()
        self.assertEqual(pet._preference("chat.open"), "1")
        self.assertTrue(pet._chat_was_open())
        pet.hide_chat()
        self.assertEqual(pet._preference("chat.open"), "0")
        self.assertFalse(pet._chat_was_open())


class SettingsCategoryTests(TkTestCase):
    """设置中心按类别分开，不再是「通用」一个长页面。"""

    EXPECTED = (
        "基础",
        "个性化",
        "模型",
        "角色",
        "人格",
        "记忆",
        "技能",
        "语音",
        "主动行为",
        "权限",
        "开发者",
    )

    def test_settings_lists_nine_categories_in_order(self):
        pet, _ = self.build_pet()
        window = pet.open_settings()
        self.addCleanup(window.win.destroy)
        titles = [
            window.book.tab(index, "text")
            for index in range(window.book.index("end"))
        ]
        self.assertEqual(tuple(titles), self.EXPECTED)

    def test_proactive_tab_holds_the_quiet_controls(self):
        pet, _ = self.build_pet()
        window = pet.open_settings()
        self.addCleanup(window.win.destroy)
        self.assertTrue(hasattr(pet, "proactive_var"))
        self.assertTrue(hasattr(pet, "quiet_var"))
        self.assertTrue(hasattr(pet, "cooldown_entry"))
        self.assertTrue(hasattr(pet, "daily_entry"))

    def test_base_tab_keeps_the_shell_controls(self):
        pet, _ = self.build_pet()
        window = pet.open_settings()
        self.addCleanup(window.win.destroy)
        self.assertTrue(hasattr(pet, "startup_var"))
        self.assertTrue(hasattr(pet, "hotkey_entry"))
        self.assertTrue(hasattr(pet, "topmost_var"))


class PlacementTests(TkTestCase):
    def test_default_position_is_reachable_and_remembered(self):
        pet, _ = self.build_pet()
        pet.host.move_to(11, 22)
        pet.reset_position()
        self.assertEqual(tuple(pet.host.position()), tuple(pet.host.default_position()))
        saved = pet._preference(DesktopPet.POSITION_KEY)
        self.assertEqual(saved, "%d,%d" % tuple(pet.host.default_position()))

    def test_topmost_can_be_turned_off_and_stays_off(self):
        pet, _ = self.build_pet()
        window = pet.open_settings()
        self.addCleanup(window.win.destroy)
        self.assertTrue(pet.topmost_enabled())
        pet.topmost_var.set(False)
        pet._save_topmost()
        self.assertFalse(pet.host.topmost)
        self.assertFalse(pet.topmost_enabled())
        self.assertEqual(pet._preference("shell.topmost"), "0")
        pet._stay_on_top()
        self.assertFalse(pet.host.topmost)


class DeveloperPanelTests(TkTestCase):
    def developer_tab(self, window):
        """按标题找开发者页，不写死序号（前面加分区不该弄坏这个测试）。"""
        titles = [
            window.book.tab(index, "text") for index in range(window.book.index("end"))
        ]
        return window.book.nametowidget(window.book.tabs()[titles.index("开发者")])

    def test_panel_hides_details_until_dev_mode(self):
        pet, _ = self.build_pet()
        window = pet.open_settings()
        self.addCleanup(window.win.destroy)
        texts = label_texts(self.developer_tab(window))
        joined = "\n".join(texts)
        self.assertIn("开发者模式没有打开", joined)
        self.assertNotIn("运行模式", joined)

    def test_panel_shows_program_state_in_dev_mode(self):
        self.config.dev_mode = True
        pet, _ = self.build_pet()
        window = pet.open_settings()
        self.addCleanup(window.win.destroy)
        texts = label_texts(self.developer_tab(window))
        joined = "\n".join(texts)
        for key in ("运行模式", "任务状态", "记忆条数", "累计 Token", "角色后端", "设备"):
            self.assertIn(key, joined)
        self.assertNotIn("读取失败", joined)

    def test_developer_rows_never_leak_the_api_key(self):
        self.config.companion_api_key = "sk-should-not-appear"
        pet, _ = self.build_pet()
        rows = pet.developer_rows()
        blob = "\n".join("%s=%s" % row for row in rows)
        self.assertNotIn("sk-should-not-appear", blob)
        self.assertNotIn("sk-", blob)

    def test_developer_rows_survive_a_broken_memory(self):
        pet, _ = self.build_pet()

        class Broken:
            def stats(self):
                raise RuntimeError("库坏了")

        pet.memory = Broken()
        rows = dict(pet.developer_rows())
        self.assertIn("读取失败", rows["记忆条数"])
        self.assertIn("运行模式", rows)


if __name__ == "__main__":
    unittest.main()
