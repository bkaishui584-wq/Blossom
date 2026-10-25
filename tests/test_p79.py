"""第三轮 UI 验收：定稿图版式、点名要的四支点缀色、连续性上下文包与学习建议卡。

覆盖：深浅两套主题都带天蓝/浅青/粉红/白；空会话是 2×2 能力卡；左侧导航分组、
「新建对话」是顶上主按钮；顶栏与输入区的图标入口；连续性上下文包报的是任务库里
真实的目标与步骤；学习建议卡能看影响范围、能稍后、能撤销。
"""

import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.context import ContextManager
from agent.core import AgentCore
from agent.task import TaskRecord, TaskState, TaskStep, new_task_id
from agent.task_store import TaskStore
from database.db import Database
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatDelta, ChatResult, ModelProvider, Usage
from persona.interface import DefaultPersonaProvider, build_persona_note
from persona.learner import HabitLearner
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
        agent = AgentCore(
            StubProvider(), memory, ContextManager(token_budget=800),
            persona=DefaultPersonaProvider(),
        )
        agent.task_store = TaskStore(database)
        # 学习接线和 app/main.py 一样，学习器只写 persona. 开头的偏好键。
        agent.learning = HabitLearner(preferences=memory, persona=agent.persona)
        agent.learning.apply()
        pet = DesktopPet(agent, self.config, memory=memory, root=self.root)
        self.addCleanup(pet.close)
        return pet, memory, database

    def seed_task(self, store, goal, state, steps=None):
        record = TaskRecord(id=new_task_id(), goal=goal, state=state, session="desktop")
        store.save(record)
        if steps:
            store.replace_steps(record.id, [TaskStep(**row) for row in steps])
        return record


class TintTests(unittest.TestCase):
    """点名要的四支点缀色：天蓝、浅青、粉红、白。深浅两套都得有。"""

    def setUp(self):
        self.original = (theme.active(), theme.accent())
        self.addCleanup(lambda: theme.apply(self.original[0], accent_name=self.original[1]))

    def test_the_four_tints_are_in_both_palettes(self):
        for key in ("SKY", "CYAN", "PINK", "WHITE"):
            self.assertIn(key, theme.DARK, key)
            self.assertIn(key, theme.LIGHT, key)
            self.assertNotEqual(theme.DARK[key], "", key)
        self.assertEqual(theme.DARK["WHITE"].lower(), "#ffffff")

    def test_cyan_is_a_pickable_accent(self):
        self.assertIn("cyan", theme.ACCENT_NAMES)
        self.assertTrue(theme.ACCENT_LABELS.get("cyan"))
        theme.apply("dark", accent_name="cyan")
        self.assertEqual(theme.ACCENT.lower(), theme.ACCENTS["cyan"][0])

    def test_the_cards_use_exactly_the_four_tints(self):
        self.assertEqual(
            [row[3] for row in ChatView.EMPTY_CARDS],
            ["SKY", "CYAN", "PINK", "WHITE"],
        )

    def test_the_hero_line_is_bigger_than_body_text(self):
        self.assertGreater(theme.FONT_HERO[1], theme.FONT_BODY[1])


class EmptyStateTests(TkCase):
    """空会话按定稿图：正中一句问话，下面 2×2 四张能力卡。"""

    def test_the_hero_line_sits_above_the_cards(self):
        parent, view = self.make_view()
        self.assertTrue(view.is_empty_shown())
        self.assertEqual(str(view.empty_title.cget("text")), ChatView.EMPTY_TITLE)
        self.assertTrue(str(view.empty_text.cget("text")).strip())
        self.assertEqual(
            str(view.empty_cards_box.winfo_manager()), "pack"
        )
        order = view.empty.winfo_children()
        self.assertLess(order.index(view.empty_hero), order.index(view.empty_cards_box))

    def test_every_card_carries_its_own_tint(self):
        parent, view = self.make_view()
        for key, _title, _note, colour in ChatView.EMPTY_CARDS:
            icon = view.empty_card_icons[key][0]
            self.assertEqual(
                str(icon.cget("fg")).lower(), getattr(theme, colour).lower(), key
            )

    def test_switching_the_theme_repaints_the_cards(self):
        parent, view = self.make_view()
        theme.apply("light")
        try:
            view.apply_theme()
            self.settle(parent)
            for key, _title, _note, colour in ChatView.EMPTY_CARDS:
                icon = view.empty_card_icons[key][0]
                self.assertEqual(
                    str(icon.cget("fg")).lower(), theme.LIGHT[colour].lower(), key
                )
        finally:
            theme.apply("dark", accent_name=theme.DEFAULT_ACCENT)
            view.apply_theme()

    def test_a_short_window_puts_the_cards_away(self):
        parent, view = self.make_view(size=(900, 500))
        self.settle(parent)
        self.assertNotEqual(str(view.empty_cards_box.winfo_manager()), "pack")


class EmptyCanvasTests(TkCase):
    """空会话的消息区不留空框；新建对话回到同一张欢迎页。"""

    def test_an_empty_message_area_blends_into_the_background(self):
        parent, view = self.make_view()
        self.assertTrue(view.is_empty_shown())
        self.assertEqual(str(view.transcript.cget("bg")).lower(), theme.BG.lower())
        self.assertEqual(view._paint_transcript_bg().lower(), theme.BG.lower())

    def test_the_message_area_gets_its_own_colour_back(self):
        parent, view = self.make_view()
        view.append("pet", "在。")
        self.settle(parent)
        self.assertFalse(view.is_empty_shown())
        self.assertEqual(str(view.transcript.cget("bg")).lower(), theme.FIELD.lower())

    def test_clearing_the_chat_puts_the_welcome_screen_back(self):
        parent, view = self.make_view()
        view.append("pet", "在。")
        self.settle(parent)
        view.clear()
        self.settle(parent)
        self.assertTrue(view.is_empty_shown())
        self.assertEqual(str(view.transcript.cget("bg")).lower(), theme.BG.lower())

    def test_a_new_session_lands_on_the_welcome_screen(self):
        pet, _memory, _database = self.build_pet()
        pet.chat.append("pet", "在。")
        self.settle(self.root)
        pet.new_session()
        self.settle(self.root)
        self.assertTrue(pet.chat.is_empty_shown())
        self.assertEqual(
            str(pet.chat.empty_title.cget("text")), ChatView.EMPTY_TITLE
        )
        self.assertEqual(
            pet.chat.transcript.get("1.0", "end-1c").strip(), ""
        )


class NavLayoutTests(TkCase):
    """左侧导航按定稿图分组，「新建对话」是顶上的主按钮。"""

    def test_the_group_headings_are_there_in_order(self):
        parent, view = self.make_view()
        texts = [
            str(child.cget("text"))
            for child in view.nav.winfo_children()
            if isinstance(child, tk.Label)
        ]
        self.assertIn("工作空间", texts)
        self.assertIn("最近对话", texts)
        self.assertLess(texts.index("工作空间"), texts.index("最近对话"))

    def test_new_chat_is_the_first_row_and_stays_lit(self):
        parent, view = self.make_view()
        first = view.nav.pack_slaves()[0]
        self.assertIs(first, view.nav_rows["new"])
        self.assertEqual(str(view.nav_rows["new"].cget("bg")), theme.SIDEBAR_ACTIVE)
        view.set_nav_active("tasks")
        self.assertEqual(str(view.nav_rows["tasks"].cget("bg")), theme.SIDEBAR_ACTIVE)
        self.assertEqual(str(view.nav_rows["new"].cget("bg")), theme.SIDEBAR_ACTIVE)

    def test_the_fixed_entries_are_untouched(self):
        self.assertEqual(
            [key for key, _label in ChatView.NAV_ITEMS],
            ["home", "new", "sessions", "search", "calendar", "ideas", "goals",
             "work", "tasks", "knowledge", "skills", "persona", "capabilities",
             "devices", "awareness", "settings"],
        )


class TopBarTests(TkCase):
    """顶栏：品牌加一根斜线，右边一排图标，窗口控制一个不少。"""

    def test_brand_reads_blossom_slash_talk(self):
        parent, view = self.make_view()
        self.assertEqual(str(view.brand.cget("text")), "Blossom")
        self.assertEqual(str(view.brand_icon.cget("text")), "/")
        self.assertEqual(str(view.title.cget("text")), "对话")

    def test_the_bar_buttons_are_single_icons(self):
        parent, view = self.make_view()
        for widget in (view.search_button, view.panel_button, view.settings_button,
                       view.tasks_button, view.clear_button):
            self.assertEqual(len(str(widget.cget("text"))), 1, widget)
            self.assertTrue(widget.bind("<Enter>"), "缺少悬停说明")

    def test_window_controls_are_still_mapped(self):
        parent, view = self.make_view()
        for widget in (view.min_button, view.max_button, view.close_button,
                       view.model_button, view.mode_label):
            self.assertEqual(widget.winfo_ismapped(), 1, widget)


class ComposerTests(TkCase):
    """输入区：一排图标入口，右下角一个圆形发送键。"""

    def test_the_five_icon_entries_exist(self):
        parent, view = self.make_view()
        labels = [str(widget.cget("text")) for widget in view.bar_buttons()]
        self.assertEqual(len(labels), 5)
        for widget in view.bar_buttons():
            self.assertTrue(widget.bind("<Enter>"), "缺少悬停说明")

    def test_web_and_voice_say_they_are_not_wired(self):
        parent, view = self.make_view()
        self.assertEqual(str(view.bar_web.cget("state")), "disabled")
        self.assertEqual(str(view.bar_voice.cget("state")), "disabled")
        self.assertEqual(str(view.bar_attach.cget("state")), "normal")
        # 知识库是本地能力，不管有没有模型都能点。
        self.assertEqual(str(view.bar_knowledge.cget("state")), "normal")

    def test_the_send_key_is_an_arrow_and_survives_a_narrow_window(self):
        parent, view = self.make_view(size=(320, 400))
        self.settle(parent)
        self.assertEqual(str(view.send_button.cget("text")), "\u2191")
        self.assertEqual(view.send_button.winfo_ismapped(), 1)
        rendered = view.send_button.cget("text")
        self.assertEqual(len(rendered), 1)

    def test_the_hint_mentions_files_and_sending(self):
        parent, view = self.make_view()
        self.assertIn("拖入文件", view.input.placeholder)
        self.assertIn("发送", view.input.placeholder)

    def test_wiring_voice_and_web_lights_the_bar(self):
        parent, view = self.make_view()
        view.set_voice_available(True)
        view.set_web_search_available(True)
        self.assertEqual(str(view.bar_voice.cget("state")), "normal")
        self.assertEqual(str(view.bar_web.cget("state")), "normal")
        self.assertTrue(view.voice_available())
        self.assertTrue(view.web_search_available())


class CapsuleTests(TkCase):
    """连续性上下文包：目标、做到哪一步、下一步，全部来自任务库。"""

    def test_a_live_task_becomes_a_capsule(self):
        pet, _memory, _db = self.build_pet()
        self.seed_task(
            pet.agent.task_store, "整理上周记录", TaskState.RUNNING.value,
            steps=[
                {"index": 0, "title": "读一遍", "status": "done"},
                {"index": 1, "title": "归类", "status": "running"},
                {"index": 2, "title": "写摘要", "status": "pending"},
            ],
        )
        capsule = pet.continuity_capsule()
        self.assertEqual(capsule["goal"], "整理上周记录")
        self.assertEqual(capsule["done"], ["读一遍"])
        self.assertEqual(capsule["next_step"], "归类")
        self.assertIn("整理上周记录", pet.capsule_text(capsule))

    def test_no_live_task_means_no_capsule(self):
        pet, _memory, _db = self.build_pet()
        self.assertIsNone(pet.continuity_capsule())
        self.assertEqual(pet.capsule_text(None), "")

    def test_the_resume_card_points_at_the_next_step(self):
        pet, _memory, _db = self.build_pet()
        self.seed_task(
            pet.agent.task_store, "整理上周记录", TaskState.RUNNING.value,
            steps=[{"index": 0, "title": "归类", "status": "pending"}],
        )
        card = pet._feed_resume()
        self.assertEqual(card["title"], "上次停在这里")
        self.assertIn("归类", card["body"])

    def test_goals_show_every_step_as_a_milestone(self):
        pet, _memory, _db = self.build_pet()
        pet.show_chat()
        self.seed_task(
            pet.agent.task_store, "整理上周记录", TaskState.RUNNING.value,
            steps=[
                {"index": 0, "title": "读一遍", "status": "done"},
                {"index": 1, "title": "写摘要", "status": "pending"},
            ],
        )
        rows = pet.open_goals()
        self.assertEqual(len(rows), 1)
        text = "\n".join(pet._panels["goals"].text())
        self.assertIn("读一遍", text)
        self.assertIn("写摘要", text)

    def test_a_task_without_steps_says_so(self):
        pet, _memory, _db = self.build_pet()
        self.seed_task(pet.agent.task_store, "一件事", TaskState.RUNNING.value)
        self.assertEqual(pet.goal_milestones(pet.task_rows()[0]["id"]), [])
        pet.show_chat()
        pet.open_goals()
        self.assertIn("还没有列步骤", "\n".join(pet._panels["goals"].text()))


class PolicyCardTests(TkCase):
    """学习建议卡：看影响范围、点稍后、还能撤销。"""

    def test_snoozing_hides_a_card_until_later(self):
        database = Database(self.config.db_path)
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        clock = {"now": 1000.0}
        learner = HabitLearner(preferences=memory, clock=lambda: clock["now"])
        learner.observe("以后回答简短一点")
        identifier = learner.rows()[0]["id"]
        self.assertEqual(len(learner.visible_rows()), 1)
        self.assertTrue(learner.snooze(identifier, seconds=60))
        self.assertEqual(learner.visible_rows(), [])
        clock["now"] += 61
        self.assertEqual(len(learner.visible_rows()), 1)

    def test_undo_takes_back_an_accepted_habit(self):
        pet, _memory, _db = self.build_pet()
        for _ in range(3):
            pet.agent.learning.observe("以后回答简短一点")
        note = build_persona_note(pet.agent.persona)
        self.assertIn("回复长度：简短", note)
        identifier = pet.agent.learning.rows()[0]["id"]
        self.assertTrue(pet.undo_persona_habit(identifier))
        self.assertNotIn("回复长度：简短", build_persona_note(pet.agent.persona))
        self.assertEqual(pet.agent.learning.key(identifier, "status"), "candidate")

    def test_the_scope_line_comes_from_the_real_rules(self):
        pet, _memory, _db = self.build_pet()
        for _ in range(3):
            pet.agent.learning.observe("以后叫我阿墨")
        identifier = pet.agent.learning.rows()[0]["id"]
        self.assertIn("称呼", pet.persona_habit_scope(identifier))

    def test_the_panel_shows_the_scope_column_and_the_new_buttons(self):
        pet, _memory, _db = self.build_pet()
        for _ in range(3):
            pet.agent.learning.observe("以后叫我阿墨")
        window = pet.open_persona()
        self.addCleanup(window.win.destroy)
        self.assertEqual(
            list(window.learn_tree["columns"]),
            ["field", "value", "status", "scope", "seen", "confidence"],
        )
        row = window.learn_tree.item(window.learn_tree.get_children()[0], "values")
        self.assertIn("称呼", row[3])
        self.assertTrue(callable(window.snooze_candidate))
        self.assertTrue(callable(window.undo_candidate))

    def test_snoozing_from_the_panel_clears_the_row(self):
        pet, _memory, _db = self.build_pet()
        for _ in range(3):
            pet.agent.learning.observe("以后回答简短一点")
        window = pet.open_persona()
        self.addCleanup(window.win.destroy)
        self.assertEqual(len(window.learn_tree.get_children()), 1)
        window.learn_tree.selection_set(window.learn_tree.get_children()[0])
        self.assertTrue(window.snooze_candidate())
        self.assertEqual(len(window.learn_tree.get_children()), 0)


if __name__ == "__main__":
    unittest.main()
