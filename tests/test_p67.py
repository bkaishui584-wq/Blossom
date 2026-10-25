"""DeskPet V6 阶段 B / C 验收：自定义人格文本、版本、习惯与人格测试台。

覆盖：原文照存不被改写、版本留存与回滚、草稿与生效分离、显式习惯优先且能清空、
测试台没有模型时如实报错不编造、超长人格挤不掉安全边界、面板各分页能读写、
面板存下的文本真的进提示词、重启后恢复。全程使用临时数据库，不碰真实用户数据。
"""

import os
import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.main import build_agent
from database.db import Database
from logkit.logger import get_logger
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatDelta, ChatResult, ModelProvider, Usage
from persona import habits as habits_module
from persona import cards as cards_module
from persona import lab as lab_module
from persona import profile as profile_module
from persona.interface import (
    SAFETY_BOUNDARY,
    DefaultPersonaProvider,
    build_persona_note,
)
from persona.package import CharacterPackage, PackagePersonaProvider
from persona.traits import DEFAULT_INTENSITY
from security.config import Config
from ui import persona_workbench
from ui.desktop import DesktopPet
from ui.persona_view import PersonaWindow

LONG_TEXT = (
    "你叫阿墨。说话短，先给结论。用户累的时候提醒他歇一会儿，"
    "不同意就直说，但说完照用户的决定做。不要用表格。"
)


class StubProvider(ModelProvider):
    name = "stub"

    def __init__(self, reply="我在。", available=True, fail=False):
        self.reply = reply
        self.available = available
        self.fail = fail
        self.calls = []

    def is_available(self):
        return self.available

    def chat(self, messages, tools=None, temperature=0.7):
        self.calls.append(list(messages))
        if self.fail:
            return ChatResult(ok=False, error="模型挂了", usage=Usage(), model="stub")
        return ChatResult(
            ok=True,
            content=self.reply,
            usage=Usage(prompt_tokens=11, completion_tokens=4),
            model="stub",
        )

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


class TempMemory(unittest.TestCase):
    """一个临时 SQLite，一组临时偏好。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Database(Path(self.tmp.name) / "t.sqlite3")
        self.database.migrate()
        self.memory = MemoryManager(MemoryRepository(self.database))

    def persona(self, **kwargs):
        persona = DefaultPersonaProvider(**kwargs)
        profile_module.apply_to(persona, self.memory)
        return persona


class PersonaTextTests(TempMemory):
    def test_saved_text_reaches_the_prompt_word_for_word(self):
        self.assertTrue(profile_module.save_text(self.memory, LONG_TEXT)["ok"])
        note = build_persona_note(self.persona())
        self.assertIn(LONG_TEXT, note)

    def test_text_is_stored_as_written(self):
        body = "  你叫阿墨。\n先给结论，再给理由。  "
        profile_module.save_text(self.memory, body)
        self.assertEqual(profile_module.text(self.memory), body.strip())
        self.assertEqual(self.persona().note, body.strip())

    def test_empty_text_is_refused(self):
        result = profile_module.save_text(self.memory, "   \n  ")
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])

    def test_same_text_does_not_stack_versions(self):
        profile_module.save_text(self.memory, LONG_TEXT)
        second = profile_module.save_text(self.memory, LONG_TEXT)
        self.assertTrue(second["ok"])
        self.assertFalse(second["changed"])
        self.assertEqual(len(profile_module.versions(self.memory)), 1)

    def test_versions_keep_the_originals_and_rollback_keeps_history(self):
        profile_module.save_text(self.memory, "第一版人格。")
        profile_module.save_text(self.memory, "第二版人格。")
        rows = profile_module.versions(self.memory)
        self.assertEqual([row["text"] for row in rows], ["第二版人格。", "第一版人格。"])
        result = profile_module.revert_to(self.memory, 1)
        self.assertTrue(result["ok"])
        self.assertEqual(profile_module.text(self.memory), "第一版人格。")
        # 回滚不删东西：旧的两个版本都还在，回滚本身也留了一条。
        texts = [row["text"] for row in profile_module.versions(self.memory)]
        self.assertIn("第二版人格。", texts)
        self.assertIn("第一版人格。", texts)
        self.assertEqual(len(texts), 3)

    def test_rollback_to_a_missing_version_is_refused(self):
        profile_module.save_text(self.memory, LONG_TEXT)
        self.assertFalse(profile_module.revert_to(self.memory, 99)["ok"])

    def test_draft_is_separate_from_the_live_text(self):
        profile_module.save_text(self.memory, LONG_TEXT)
        profile_module.save_draft(self.memory, "还没想好的一版。")
        self.assertTrue(profile_module.is_dirty(self.memory))
        self.assertEqual(profile_module.draft(self.memory), "还没想好的一版。")
        self.assertEqual(profile_module.text(self.memory), LONG_TEXT)
        applied = profile_module.apply_to(self.persona(), self.memory)
        self.assertTrue(applied["ok"])

    def test_draft_falls_back_to_the_live_text(self):
        profile_module.save_text(self.memory, LONG_TEXT)
        profile_module.save_draft(self.memory, "")
        self.assertFalse(profile_module.is_dirty(self.memory))
        self.assertEqual(profile_module.draft(self.memory), LONG_TEXT)
    def test_long_text_cannot_push_out_the_safety_boundary(self):
        profile_module.set_budget(self.memory, 300)
        profile_module.save_text(self.memory, "这句话很重要。" * 400)
        note = build_persona_note(self.persona())
        self.assertTrue(note.endswith(SAFETY_BOUNDARY))
        # 发出去的第一段就是用户文本，被截到预算以内；库里存的还是全文。
        sent = note.split("\n")[0]
        self.assertLessEqual(len(sent), 300)
        self.assertGreater(len(profile_module.text(self.memory)), len(sent))

    def test_a_short_text_is_not_clipped(self):
        profile_module.set_budget(self.memory, 300)
        profile_module.save_text(self.memory, LONG_TEXT)
        self.assertIn(LONG_TEXT, build_persona_note(self.persona()))

    def test_budget_is_clamped_to_a_sane_range(self):
        self.assertEqual(profile_module.set_budget(self.memory, 10), 300)
        self.assertEqual(profile_module.set_budget(self.memory, 99999), 3000)
        self.assertEqual(profile_module.budget(self.memory), 3000)

    def test_two_personas_give_two_different_prompts(self):
        first = DefaultPersonaProvider(name="小桌", note="你叫阿墨，说话短。")
        second = DefaultPersonaProvider(name="小桌", note="你叫小盐，说话慢条斯理。")
        self.assertNotEqual(build_persona_note(first), build_persona_note(second))

    def test_a_persona_without_a_note_is_unchanged(self):
        plain = build_persona_note(DefaultPersonaProvider(name="小桌"))
        explicit = build_persona_note(DefaultPersonaProvider(name="小桌", note=""))
        self.assertEqual(plain, explicit)
        self.assertIn("你的名字是小桌", plain)

    def test_the_note_goes_through_a_character_package_too(self):
        package = CharacterPackage.from_dict(
            {"id": "amo", "name": "阿墨", "persona": {"style": "说话短"}}, path=""
        )
        wrapped = PackagePersonaProvider(
            default=DefaultPersonaProvider(name="小桌"), packages=[package], selected="amo"
        )
        profile_module.save_text(self.memory, LONG_TEXT)
        applied = profile_module.apply_to(wrapped, self.memory)
        self.assertTrue(applied["ok"])
        note = build_persona_note(wrapped)
        self.assertIn(LONG_TEXT, note)
        self.assertTrue(note.endswith(SAFETY_BOUNDARY))

    def test_a_persona_that_cannot_take_text_says_so(self):
        result = profile_module.apply_to(object(), self.memory)
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])

    def test_delete_all_clears_only_this_persona_text(self):
        profile_module.save_text(self.memory, LONG_TEXT)
        self.memory.set_preference("keep.me", "1")
        self.assertTrue(profile_module.delete_all(self.memory))
        self.assertEqual(profile_module.text(self.memory), "")
        self.assertEqual(profile_module.versions(self.memory), [])
        self.assertEqual(self.memory.get_preference("keep.me"), "1")

    def test_saved_text_survives_a_real_restart(self):
        base = Path(self.tmp.name)
        config = Config(load_env=False)
        config.data_dir = base
        config.log_dir = base / "logs"
        config.db_path = base / "restart.sqlite3"
        config.characters_dir = base / "characters"
        config.live2d_dir = base / "live2d"
        config.devices_dir = base / "devices"
        first_db = Database(config.db_path)
        first_db.migrate()
        first, memory = build_agent(config, first_db, None, get_logger())
        profile_module.save_text(memory, LONG_TEXT)
        habits_module.save(memory, {"address": "阿墨"})
        # 关掉再打开：只走启动路径，从库里读回来。
        second_db = Database(config.db_path)
        second_db.migrate()
        second, _ = build_agent(config, second_db, None, get_logger())
        note = build_persona_note(second.persona)
        self.assertIn(LONG_TEXT, note)
        self.assertIn("称呼用户为「阿墨」", note)
        self.assertTrue(note.endswith(SAFETY_BOUNDARY))


class HabitTests(TempMemory):
    def test_fields_and_line_templates_line_up(self):
        self.assertEqual(len(habits_module.FIELDS), len(set(habits_module.FIELDS)))
        for name in habits_module.FIELDS:
            self.assertIn(name, habits_module.LABELS)
            self.assertIn(name, habits_module.HINTS)
            self.assertIn(name, habits_module.KINDS)
            self.assertIn(name, habits_module.CHOICES)
            self.assertIn(name, habits_module._LINE_TEMPLATES)

    def test_unknown_values_are_dropped(self):
        values = habits_module.normalize({"length": "超长", "address": "阿墨", "nope": "x"})
        self.assertEqual(values["length"], "")
        self.assertEqual(values["address"], "阿墨")
        self.assertNotIn("nope", values)

    def test_lines_skip_empty_fields(self):
        lines = habits_module.lines({"address": "阿墨", "length": ""})
        self.assertEqual(lines, ["称呼用户为「阿墨」。"])

    def test_summary_says_so_when_nothing_is_set(self):
        self.assertIn("还没有设置", habits_module.summary({}))

    def test_learning_defaults_to_on_and_can_be_turned_off(self):
        self.assertTrue(habits_module.learning_enabled(self.memory))
        self.assertTrue(habits_module.set_learning_enabled(self.memory, False))
        self.assertFalse(habits_module.learning_enabled(self.memory))

    def test_saving_without_memory_reports_failure(self):
        result = habits_module.save(None, {"address": "阿墨"})
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])


class LabTests(TempMemory):
    def test_scenarios_have_names_inputs_and_focus(self):
        ids = [item["id"] for item in lab_module.scenarios()]
        self.assertEqual(ids, list(lab_module.SCENARIO_IDS))
        for item in lab_module.scenarios():
            self.assertTrue(item["name"])
            self.assertTrue(item["input"])
            self.assertTrue(item["focus"])

    def test_unknown_scenario_is_refused(self):
        self.assertIsNone(lab_module.scenario("nope"))

    def test_without_a_model_it_says_so_and_does_not_fake_an_answer(self):
        result = lab_module.run(None, "人格", "在吗？")
        self.assertFalse(result["ok"])
        self.assertEqual(result["content"], "")
        self.assertTrue(result["error"])

    def test_an_unavailable_model_is_never_called(self):
        provider = StubProvider(available=False)
        result = lab_module.run(provider, "人格", "在吗？")
        self.assertFalse(result["ok"])
        self.assertEqual(provider.calls, [])

    def test_a_broken_model_reports_the_real_error(self):
        result = lab_module.run(StubProvider(fail=True), "人格", "在吗？")
        self.assertFalse(result["ok"])
        self.assertIn("模型挂了", result["error"])
        self.assertEqual(result["content"], "")

    def test_the_prompt_carries_the_note_and_the_boundary(self):
        note = build_persona_note(DefaultPersonaProvider(note=LONG_TEXT))
        messages = lab_module.messages_for(note, "在吗？")
        self.assertEqual(messages[-1]["role"], "user")
        self.assertIn(LONG_TEXT, messages[1]["content"])
        self.assertIn(SAFETY_BOUNDARY, messages[1]["content"])

    def test_a_run_reports_the_tokens(self):
        result = lab_module.run(StubProvider("在的。"), "人格", "在吗？")
        self.assertTrue(result["ok"])
        self.assertEqual(result["content"], "在的。")
        self.assertEqual((result["prompt_tokens"], result["completion_tokens"]), (11, 4))

    def test_compare_runs_both_sides_with_their_own_note(self):
        provider = StubProvider("嗯。")
        result = lab_module.compare(provider, "左人格", "右人格", "在吗？")
        self.assertTrue(result["left"]["ok"])
        self.assertTrue(result["right"]["ok"])
        self.assertEqual(len(provider.calls), 2)
        self.assertIn("左人格", provider.calls[0][1]["content"])
        self.assertIn("右人格", provider.calls[1][1]["content"])
class TkTestCase(unittest.TestCase):
    """真窗口测试。桌面宠物用启动路径建出来，和用户打开时一样。"""

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

    def _safe_destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def build_pet(self, provider=None):
        database = Database(self.config.db_path)
        database.migrate()
        agent, memory = build_agent(
            self.config, database, provider or StubProvider(), get_logger()
        )
        pet = DesktopPet(agent, self.config, memory=memory, root=self.root)
        return pet, memory

    def open_window(self, pet):
        window = pet.open_persona()
        self.addCleanup(window.win.destroy)
        return window

    def type_text(self, window, body):
        window.text_area.delete("1.0", "end")
        window.text_area.insert("1.0", body)


class WorkbenchWindowTests(TkTestCase):
    def test_window_has_the_workbench_tabs(self):
        pet, _ = self.build_pet()
        window = self.open_window(pet)
        titles = [
            window.book.tab(index, "text") for index in range(window.book.index("end"))
        ]
        for expected in ("人格文本", "版本", "习惯", "测试台", "性格与状态"):
            self.assertIn(expected, titles)

    def test_panel_save_applies_the_text(self):
        pet, memory = self.build_pet()
        window = self.open_window(pet)
        self.type_text(window, LONG_TEXT)
        self.assertTrue(window.save_text())
        self.assertEqual(profile_module.text(memory), LONG_TEXT)
        self.assertIn(LONG_TEXT, build_persona_note(pet.agent.persona))

    def test_preview_shows_the_editor_text_and_keeps_the_boundary_last(self):
        pet, _ = self.build_pet()
        window = self.open_window(pet)
        self.type_text(window, LONG_TEXT)
        preview = window.refresh_preview()
        self.assertIn(LONG_TEXT, preview)
        self.assertTrue(preview.endswith(SAFETY_BOUNDARY))
        self.assertIn(LONG_TEXT, window.preview_area.get("1.0", "end"))

    def test_empty_text_is_refused_from_the_panel(self):
        pet, memory = self.build_pet()
        window = self.open_window(pet)
        self.type_text(window, "   ")
        self.assertFalse(window.save_text())
        self.assertEqual(profile_module.text(memory), "")

    def test_draft_survives_reopening_and_does_not_take_effect(self):
        pet, memory = self.build_pet()
        window = self.open_window(pet)
        self.type_text(window, "还没定的一版。")
        self.assertTrue(window.save_draft())
        # 收起再打开：页面是缓存的，编辑器里那版还在，前面写的没丢。
        pet.close_panel()
        self.assertIs(pet.open_persona(), window)
        self.assertEqual(window._editor_text(), "还没定的一版。")
        # 把缓存整块丢掉重开一遍，草稿得从库里读回来，不能只靠内存里那点状态。
        pet.forget_panels()
        again = self.open_window(pet)
        self.assertIsNot(again, window)
        self.assertEqual(again._editor_text(), "还没定的一版。")
        self.assertEqual(profile_module.text(memory), "")
        self.assertNotIn("还没定的一版。", build_persona_note(pet.agent.persona))

    def test_template_fills_the_editor_and_needs_saving(self):
        pet, memory = self.build_pet()
        window = self.open_window(pet)
        window.template_var.set(profile_module.TEMPLATE_NAMES[0])
        self.assertTrue(window.use_template())
        self.assertTrue(window._editor_text().strip())
        self.assertEqual(profile_module.text(memory), "")
        self.assertTrue(window.save_text())
        self.assertTrue(profile_module.text(memory).strip())

    def test_importing_a_card_switches_the_name_text_and_greeting(self):
        pet, memory = self.build_pet()
        window = self.open_window(pet)
        preview = cards_module.normalize(cards_module.BUILTIN_CARDS[0])
        self.assertTrue(window.apply_card(preview))
        self.assertEqual(profile_module.name(memory), preview["name"])
        self.assertIn(preview["name"], window._editor_text())
        self.assertEqual(window.name_var.get(), preview["name"])
        # 空会话的问候跟着新名字走，不能还挂着旧名字。
        self.assertIn(preview["name"], pet._greeting_text())

    def test_an_imported_card_is_kept_in_the_library(self):
        pet, memory = self.build_pet()
        window = self.open_window(pet)
        preview = cards_module.normalize(cards_module.BUILTIN_CARDS[1])
        self.assertTrue(window.apply_card(preview))
        rows = pet.persona_card_library()
        self.assertEqual([row["name"] for row in rows], [preview["name"]])
        self.assertIn("共 1 张", window.library_hint.cget("text"))
        # 卡库在数据目录下，和 skills、voices 同级；测试夹具已经把 data_dir 指到临时目录。
        self.assertTrue(str(rows[0]["path"]).startswith(str(Path(self.config.data_dir))))
        # 从库里读回来，内容与应用时一致。
        card = pet.load_persona_card(rows[0]["path"])
        self.assertTrue(card["ok"], card)
        self.assertEqual(card["text"], preview["text"])

    def test_the_name_field_changes_the_greeting_without_touching_the_text(self):
        pet, memory = self.build_pet()
        window = self.open_window(pet)
        self.type_text(window, "原来的人格。")
        self.assertTrue(window.save_text())
        window.name_var.set("白露")
        self.assertTrue(window.save_name())
        self.assertEqual(profile_module.name(memory), "白露")
        self.assertEqual(profile_module.text(memory), "原来的人格。")
        self.assertIn("白露", pet._greeting_text())

    def test_clearing_the_name_field_returns_to_the_default_name(self):
        pet, memory = self.build_pet()
        window = self.open_window(pet)
        window.name_var.set("白露")
        self.assertTrue(window.save_name())
        window.name_var.set("")
        self.assertTrue(window.save_name())
        self.assertEqual(profile_module.name(memory), "")
        self.assertEqual(
            pet.agent.persona.get_profile()["name"], pet.config.persona_name
        )
        self.assertIn(pet.config.persona_name, pet._greeting_text())

    def test_versions_compare_and_revert_need_confirmation(self):
        pet, memory = self.build_pet()
        window = self.open_window(pet)
        self.type_text(window, "第一版。")
        window.save_text()
        self.type_text(window, "第二版。")
        window.save_text()
        self.assertEqual(len(window.version_tree.get_children()), 2)
        window.version_tree.selection_set("1")
        self.assertTrue(window.compare_version())
        self.assertIn("第二版。", window.compare_current.get("1.0", "end"))
        self.assertIn("第一版。", window.compare_other.get("1.0", "end"))
        with mock.patch.object(persona_workbench.messagebox, "askyesno", return_value=False):
            self.assertFalse(window.revert_version())
        self.assertEqual(profile_module.text(memory), "第二版。")
        with mock.patch.object(persona_workbench.messagebox, "askyesno", return_value=True):
            self.assertTrue(window.revert_version())
        self.assertEqual(profile_module.text(memory), "第一版。")
        self.assertIn("第一版。", build_persona_note(pet.agent.persona))

    def test_habits_load_save_and_clear(self):
        pet, memory = self.build_pet()
        window = self.open_window(pet)
        window.habit_vars["address"].set("阿墨")
        window.habit_vars["length"].set("简短")
        window.habit_vars["avoid"].set("不要用表格")
        self.assertTrue(window.save_habits())
        self.assertEqual(habits_module.load(memory)["length"], "简短")
        note = build_persona_note(pet.agent.persona)
        self.assertIn("称呼用户为「阿墨」", note)
        self.assertIn("不要用表格", note)
        with mock.patch.object(persona_workbench.messagebox, "askyesno", return_value=False):
            self.assertFalse(window.reset_habits())
        self.assertEqual(habits_module.load(memory)["address"], "阿墨")
        with mock.patch.object(persona_workbench.messagebox, "askyesno", return_value=True):
            self.assertTrue(window.reset_habits())
        self.assertEqual(window.habit_vars["address"].get(), "")
        self.assertNotIn("称呼用户为「阿墨」", build_persona_note(pet.agent.persona))

    def test_learning_toggle_is_persisted(self):
        pet, memory = self.build_pet()
        window = self.open_window(pet)
        self.assertTrue(window.learning_var.get())
        window.learning_var.set(0)
        self.assertTrue(window.toggle_learning())
        self.assertFalse(habits_module.learning_enabled(memory))

    def test_budget_change_is_applied(self):
        pet, _ = self.build_pet()
        window = self.open_window(pet)
        window.budget_var.set("1200")
        self.assertEqual(window.save_budget(), 1200)
        self.assertEqual(profile_module.budget(pet.memory), 1200)

    def test_trait_tab_still_works(self):
        pet, _ = self.build_pet()
        window = self.open_window(pet)
        window.vars["幽默"].set(85)
        self.assertTrue(window.save_traits())
        self.assertIn("幽默", build_persona_note(pet.agent.persona))

    def test_lab_says_it_cannot_run_without_a_model(self):
        pet, _ = self.build_pet(provider=StubProvider(available=False))
        window = self.open_window(pet)
        self.assertFalse(window.run_lab())
        self.assertTrue(str(window.lab_status.cget("text")).strip())
        self.assertEqual(window.lab_left.get("1.0", "end").strip(), "")

    def test_lab_runs_and_leaves_no_trace(self):
        pet, memory = self.build_pet(provider=StubProvider("在的。"))
        window = self.open_window(pet)
        before = len(memory.browse(limit=500))
        self.assertTrue(window.run_lab())
        self.assertIn("在的。", window.lab_left.get("1.0", "end"))
        self.assertEqual(len(memory.browse(limit=500)), before)
        self.assertEqual(profile_module.text(memory), "")
        self.assertIn("11+4", str(window.lab_status.cget("text")))

    def test_lab_compare_needs_a_version(self):
        pet, _ = self.build_pet(provider=StubProvider("在的。"))
        window = self.open_window(pet)
        self.assertFalse(window.run_lab_compare())

    def test_lab_feedback_is_recorded(self):
        pet, memory = self.build_pet(provider=StubProvider("在的。"))
        window = self.open_window(pet)
        window.run_lab()
        self.assertTrue(window.lab_feedback("不符合"))
        rows = memory.browse(kind="feedback", limit=10)
        self.assertTrue(any("不符合" in row["content"] for row in rows))

    def test_host_test_run_refuses_an_unknown_scenario(self):
        pet, _ = self.build_pet()
        result = pet.persona_test_run("nope")
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])

    def test_host_habits_round_trip(self):
        pet, _ = self.build_pet()
        self.assertTrue(pet.save_persona_habits({"tone": "轻松幽默"}))
        self.assertEqual(pet.persona_habits()["tone"], "轻松幽默")
        self.assertIn("语言风格：轻松幽默", build_persona_note(pet.agent.persona))


if __name__ == "__main__":
    unittest.main()
