"""第二轮 UI 验收：状态行瘦身、场景入口、推荐操作、任务模式与「＋」分组面板。

覆盖：状态行不再常驻模型与 token、详情入口给的是真数据、空会话四个场景入口各自有行为、
推荐操作按真实能力生成且点一下就填进输入框、任务模式开关可切换且记得住、
输入工作台的「＋」分组面板默认收起、没接的能力仍然灰着并写明原因。
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
from ui.desktop import DesktopPet


class StubProvider(ModelProvider):
    name = "stub"

    def is_available(self):
        return True

    def chat(self, messages, tools=None, temperature=0.7):
        return ChatResult(ok=True, content="在的。", usage=Usage(5, 3), model="stub")

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


class TkCase(unittest.TestCase):
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
        self.events = []

    def _safe_destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def settle(self, widget):
        for _ in range(4):
            widget.update_idletasks()
            widget.update()

    def make_view(self, size=(1200, 800)):
        parent = tk.Toplevel(self.root)
        parent.geometry("%dx%d" % size)
        self.addCleanup(lambda: parent.winfo_exists() and parent.destroy())
        view = ChatView(parent, on_event=lambda name, payload=None: self.events.append((name, payload)))
        self.settle(parent)
        return parent, view

    def build_pet(self):
        database = Database(self.config.db_path)
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        agent = AgentCore(StubProvider(), memory, ContextManager(token_budget=800))
        pet = DesktopPet(agent, self.config, memory=memory, root=self.root)
        self.addCleanup(pet.close)
        return pet, memory, database


class StatusRowTests(TkCase):
    """状态行只写一行状态，模型和 token 收进详情。"""

    def test_the_row_keeps_the_noisy_parts_out(self):
        pet, _memory, _db = self.build_pet()
        pet._refresh_status()
        row = str(pet.status.cget("text"))
        self.assertIn("情绪", row)
        self.assertNotIn("tokens", row)
        self.assertNotIn("聊天模型", row)
        self.assertNotIn("任务能力", row)

    def test_the_full_line_is_still_available_for_the_detail(self):
        pet, _memory, _db = self.build_pet()
        detail = pet._status_full_text()
        self.assertIn("tokens", detail)
        self.assertIn("未配置", detail)

    def test_the_detail_button_reports_back(self):
        _parent, view = self.make_view()
        view.status_detail.invoke()
        self.assertIn(("status_detail", None), self.events)

    def test_the_host_builds_the_detail_from_real_rows(self):
        pet, _memory, _db = self.build_pet()
        with mock.patch.object(DesktopPet, "_show_detail_window", return_value=None) as opener:
            body = pet._chat_status_detail()
        self.assertTrue(opener.called)
        self.assertIn("运行模式", body)
        self.assertIn("记忆条数", body)


class SceneEntryTests(TkCase):
    """空会话四张能力卡片：都只是把话填进输入框，没有替用户发送。"""

    def test_the_entries_are_capability_cards(self):
        _parent, view = self.make_view()
        self.assertEqual(list(view.empty_cards), ["code", "research", "files", "plan"])

    def test_a_card_fills_the_box_without_sending(self):
        pet, _memory, _db = self.build_pet()
        pet.show_chat()
        text = pet._chat_card("research")
        self.assertTrue(text)
        self.assertEqual(pet.chat.read_input().strip(), text)
        # 只填进输入框，没有替用户发出去。
        self.assertNotIn(text, pet.chat.history_text())

    def test_every_card_has_a_real_starter_line(self):
        pet, _memory, _db = self.build_pet()
        pet.show_chat()
        for key, _title, _note, _colour in ChatView.EMPTY_CARDS:
            pet.chat.clear_input()
            text = pet._chat_card(key)
            self.assertTrue(text, key)
            self.assertEqual(pet.chat.read_input().strip(), text, key)

    def test_task_entry_switches_to_task_mode(self):
        pet, _memory, _db = self.build_pet()
        self.assertTrue(pet._chat_card("task"))
        self.assertTrue(pet.chat.task_mode())

    def test_partner_entry_opens_the_persona_page(self):
        pet, _memory, _db = self.build_pet()
        with mock.patch.object(DesktopPet, "open_persona", return_value="persona") as opened:
            self.assertEqual(pet._chat_card("persona"), "persona")
        self.assertTrue(opened.called)


class RecommendationTests(TkCase):
    """推荐操作来自真实能力，点一下只是把话填进输入框。"""

    def test_tips_render_and_fill_the_input(self):
        pet, _memory, _db = self.build_pet()
        tips = pet.chat_tips()
        labels = [row[0] for row in tips]
        self.assertIn("现在几点", labels)
        self.assertEqual(pet.chat.set_tips(tips), len(tips))
        self.assertEqual(pet.chat.tip_texts(), labels)
        pet.chat.tip_chips[0].invoke()
        self.assertEqual(pet.chat.read_input().strip(), tips[0][1])

    def test_no_tips_means_no_block(self):
        _parent, view = self.make_view()
        self.assertEqual(view.set_tips([]), 0)
        self.assertEqual(view.tip_texts(), [])
        self.assertEqual(str(view.empty_notes.winfo_manager()), "")

    def test_a_pending_task_leads_the_suggestions(self):
        pet, _memory, _db = self.build_pet()
        rows = [{"id": "t1", "goal": "整理上周记录", "state": "running", "state_label": "进行中"}]
        with mock.patch.object(DesktopPet, "task_rows", return_value=rows):
            tips = pet.chat_tips()
        self.assertEqual(tips[0][0], "接着做上回的")
        self.assertIn("整理上周记录", tips[0][1])


class TaskModeTests(TkCase):
    """任务模式：看得见、记得住，不动执行规则。"""

    def test_toggling_switches_and_remembers(self):
        pet, memory, _db = self.build_pet()
        pet._chat_ready = True
        self.assertTrue(pet._chat_mode_toggle(True))
        self.assertTrue(pet.chat.task_mode())
        self.assertEqual(memory.get_preference("chat.task_view"), "1")
        self.assertFalse(pet._chat_mode_toggle(False))
        self.assertFalse(pet.chat.task_mode())
        self.assertEqual(memory.get_preference("chat.task_view"), "0")

    def test_the_panel_speaks_from_the_task_store(self):
        pet, _memory, _db = self.build_pet()
        self.assertIn("还没有任务记录", pet._task_panel_text())

    def test_naming_a_task_shows_it_in_the_panel(self):
        pet, _memory, _db = self.build_pet()
        rows = [{"id": "t1", "goal": "整理会议记录", "state": "running", "state_label": "进行中"}]
        with mock.patch.object(DesktopPet, "task_rows", return_value=rows):
            text = pet._task_panel_text()
        self.assertIn("整理会议记录", text)
        self.assertIn("进行中", text)


class ComposerToolsTests(TkCase):
    """「＋」分组面板：默认收起，每个入口都有真实状态。"""

    def test_tools_start_collapsed_and_open_on_demand(self):
        _parent, view = self.make_view()
        self.assertFalse(view.tools_open())
        self.assertTrue(view.toggle_tools())
        self.assertTrue(view.tools_open())
        self.assertFalse(view.toggle_tools())
        self.assertFalse(view.tools_open())

    def test_every_entry_exists_and_reports_back(self):
        _parent, view = self.make_view()
        view.image_button.invoke()
        self.assertIn(("attach_image", None), self.events)
        # 知识库是本地能力，默认就能点；语音和网页搜索没接上，按规矩灰着。
        view._knowledge()
        self.assertIn(("knowledge", None), self.events)
        self.assertEqual(str(view.knowledge_button.cget("state")), "normal")
        self.assertEqual(str(view.voice_button.cget("state")), "disabled")
        self.assertEqual(str(view.web_button.cget("state")), "disabled")

    def test_the_model_entry_is_a_way_into_the_model_menu(self):
        _parent, view = self.make_view()
        view.composer_model.invoke()
        self.assertIn(("model_menu", None), self.events)

    def test_the_drop_hint_tells_the_truth(self):
        _parent, view = self.make_view()
        self.assertIn("设置", str(view.drop_hint.cget("text")))
        self.assertIn("附件", str(view.drop_hint.cget("text")))

    def test_the_bar_is_no_longer_a_row_of_small_buttons(self):
        _parent, view = self.make_view()
        self.assertFalse(view.tool_box.winfo_ismapped())
        self.assertEqual(str(view.more_button.cget("text")), "＋")


class HomeFeedTests(TkCase):
    """首页信息流：每条都来自真实存储，取不到就不摆。"""

    def test_the_feed_only_shows_what_is_really_there(self):
        pet, _memory, _db = self.build_pet()
        titles = [card["title"] for card in pet.home_feed()]
        self.assertIn("今日简报", titles)
        self.assertNotIn("任务进展", titles)
        self.assertNotIn("上次停在这里", titles)

    def test_progress_counts_real_states(self):
        pet, _memory, _db = self.build_pet()
        rows = [
            {"id": "a", "goal": "写周报", "state": "running", "state_label": "进行中"},
            {"id": "b", "goal": "查天气", "state": "completed", "state_label": "已完成"},
        ]
        with mock.patch.object(DesktopPet, "task_rows", return_value=rows):
            card = pet._feed_progress()
        self.assertIn("进行中 1 件", card["body"])
        self.assertIn("已完成 1 件", card["body"])
        self.assertIn("写周报", card["body"])

    def test_pending_card_only_lists_stuck_tasks(self):
        pet, _memory, _db = self.build_pet()
        rows = [{"id": "a", "goal": "写周报", "state": "waiting_user", "state_label": "等待用户"}]
        with mock.patch.object(DesktopPet, "task_rows", return_value=rows):
            self.assertIn("写周报", pet._feed_pending()["body"])
        done = [{"id": "a", "goal": "写周报", "state": "completed", "state_label": "已完成"}]
        with mock.patch.object(DesktopPet, "task_rows", return_value=done):
            self.assertIsNone(pet._feed_pending())

    def test_the_card_renders_and_its_open_button_routes(self):
        _parent, view = self.make_view()
        view.set_feed([
            {"title": "任务进展", "body": "一共 2 件", "actions": [("打开任务中心", "open:tasks")]}
        ])
        self.assertEqual(view.feed_titles(), ["任务进展"])
        chip = view.feed_cards[0].winfo_children()[-1].winfo_children()[0]
        chip.invoke()
        self.assertIn(("open", "tasks"), self.events)

    def test_a_prefill_button_lands_in_the_input(self):
        _parent, view = self.make_view()
        view.set_feed([
            {"title": "今日简报", "body": "x", "actions": [("今天做什么", "帮我安排今天的计划：")]}
        ])
        chip = view.feed_cards[0].winfo_children()[-1].winfo_children()[0]
        chip.invoke()
        self.assertEqual(view.read_input().strip(), "帮我安排今天的计划：")

    def test_a_very_short_window_keeps_the_feed_out_of_the_way(self):
        parent, view = self.make_view(size=(420, 400))
        view.set_feed([{"title": "今日简报", "body": "x", "actions": []}])
        self.settle(parent)
        self.assertEqual(str(view.feed_box.winfo_manager()), "")

    def test_home_refreshes_the_feed(self):
        pet, _memory, _db = self.build_pet()
        self.assertTrue(pet.open_home())
        self.assertTrue(pet.chat.feed_titles())

    def test_ideas_and_goals_say_what_is_missing(self):
        pet, _memory, _db = self.build_pet()
        # V8：灵感建议与目标都进了主工作台，各自是一页，没有数据就摆空状态。
        self.assertEqual(pet.open_ideas(), [])
        ideas = pet._panels.get("ideas")
        self.assertIsNotNone(ideas)
        self.assertIn("现在没有可以提的建议", ideas.text())
        self.assertEqual(pet.open_goals(), [])
        goals = pet._panels.get("goals")
        self.assertIsNotNone(goals)
        self.assertIn("还没有在追的目标", goals.text())

    def test_the_window_target_follows_the_spec(self):
        pet, _memory, _db = self.build_pet()
        width, height = pet._default_card_size()
        self.assertGreaterEqual(width, 1280)
        self.assertGreaterEqual(height, 820)


if __name__ == "__main__":
    unittest.main()
