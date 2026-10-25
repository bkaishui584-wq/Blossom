"""阶段 C 验收：人格、关系与行为联动。

覆盖：性格强度持久化与热生效、人格反馈撤销与样本导出、关系重置、
主动提醒的用户阀门（冷却 / 每日上限）、内部设置不进上下文、
人格面板界面，以及启动时套回保存过的性格。
"""

import json
import os
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
from behavior.manager import BehaviorManager
from database.db import Database
from events.bus import EventBus
from logkit.logger import configure
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatDelta, ChatResult, ModelProvider, Usage
from persona import traits as traits_module
from persona.interface import DefaultPersonaProvider, build_persona_note
from persona.package import PackagePersonaProvider
from persona.training import LocalTrainingCollector
from scheduler.manager import CompanionRule, Scheduler
from security.config import Config
from ui import persona_view
from ui.desktop import DesktopPet
from ui.persona_view import PersonaWindow, mode_lines, trait_names


def make_memory(tmp, **kwargs):
    database = Database(Path(tmp) / "t.sqlite3")
    database.migrate()
    return MemoryManager(MemoryRepository(database), **kwargs), database


class StubProvider(ModelProvider):
    name = "stub"

    def __init__(self, reply="在的。"):
        self.reply = reply
        self.calls = 0

    def is_available(self):
        return True

    def chat(self, messages, tools=None, temperature=0.7):
        self.calls += 1
        return ChatResult(
            ok=True, content=self.reply, usage=Usage(prompt_tokens=5, completion_tokens=3),
            model="stub",
        )

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


class FakeRandom:
    """固定随机数，行为层的待机节奏才可复现。"""

    def randint(self, low, high):
        return low

    def choice(self, items):
        return list(items)[0]


# ---------------------------------------------------------------- 性格强度
class TraitHelpersTests(unittest.TestCase):
    def test_normalize_keeps_known_and_bounds(self):
        got = traits_module.normalize({"温柔": 130, "理性": -5, "不存在": 80})
        self.assertEqual(got, {"温柔": 100, "理性": 0})

    def test_normalize_ignores_garbage(self):
        self.assertEqual(traits_module.normalize({"温柔": "高"}), {})
        self.assertEqual(traits_module.normalize(None), {})

    def test_parse_accepts_both_colons(self):
        self.assertEqual(
            traits_module.parse("理性:70，温柔：30"), {"理性": 70, "温柔": 30}
        )

    def test_default_intensity_is_fifty(self):
        self.assertEqual(traits_module.DEFAULT_INTENSITY, 50)

    def test_low_traits_are_not_written_into_prompt(self):
        self.assertEqual(traits_module.describe({"温柔": 10}), "")

    def test_trait_names_cover_the_panel_list(self):
        names = trait_names()
        for name in persona_view.PRIMARY_TRAITS:
            self.assertIn(name, names)
        self.assertEqual(len(names), len(set(names)))


class TraitStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.memory, _ = make_memory(self.tmp.name)

    def test_save_then_load_roundtrip(self):
        self.assertTrue(traits_module.save(self.memory, {"幽默": 85, "理性": 60}))
        self.assertEqual(traits_module.load(self.memory), {"幽默": 85, "理性": 60})

    def test_load_without_memory_is_empty(self):
        self.assertEqual(traits_module.load(None), {})

    def test_save_without_memory_reports_failure(self):
        self.assertFalse(traits_module.save(None, {"幽默": 85}))

    def test_load_ignores_broken_json(self):
        self.memory.set_preference(traits_module.PREFERENCE_KEY, "{不是 json")
        self.assertEqual(traits_module.load(self.memory), {})

    def test_load_ignores_non_dict_json(self):
        self.memory.set_preference(traits_module.PREFERENCE_KEY, "[1, 2]")
        self.assertEqual(traits_module.load(self.memory), {})

    def test_load_filters_unknown_traits(self):
        self.memory.set_preference(
            traits_module.PREFERENCE_KEY, json.dumps({"神秘": 90, "安静": 40})
        )
        self.assertEqual(traits_module.load(self.memory), {"安静": 40})


class TraitEffectTests(unittest.TestCase):
    def test_changing_traits_changes_the_prompt(self):
        provider = DefaultPersonaProvider()
        self.assertNotIn("幽默", build_persona_note(provider))
        provider.set_traits({"幽默": 90})
        note = build_persona_note(provider)
        self.assertIn("幽默", note)
        self.assertIn("不可被性格", note)

    def test_package_provider_delegates_set_traits(self):
        provider = PackagePersonaProvider(default=DefaultPersonaProvider())
        provider.set_traits({"理性": 80})
        self.assertEqual(provider.get_profile()["traits"], {"理性": 80})

    def test_safety_boundary_stays_last(self):
        provider = DefaultPersonaProvider(traits={"温柔": 90})
        note = build_persona_note(provider)
        self.assertTrue(note.rstrip().endswith("不假装已完成未执行的动作。"))

    def test_idle_speed_follows_traits(self):
        lively = BehaviorManager(
            persona=DefaultPersonaProvider(traits={"活泼": 90}), random_source=FakeRandom()
        )
        quiet = BehaviorManager(
            persona=DefaultPersonaProvider(traits={"安静": 90}), random_source=FakeRandom()
        )
        plain = BehaviorManager(persona=DefaultPersonaProvider(), random_source=FakeRandom())
        self.assertEqual(lively._persona_factor(), 0.6)
        self.assertEqual(quiet._persona_factor(), 1.8)
        self.assertEqual(plain._persona_factor(), 1.0)


class StartupApplyTests(unittest.TestCase):
    """保存过的性格要在下次启动时套回去，不需要改 Agent Core。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.memory = MemoryManager(
            MemoryRepository(Database(Path(self.tmp.name) / "t.sqlite3"))
        )
        self.memory.repository.db.migrate()

    def apply_like_startup(self, persona):
        saved = traits_module.load(self.memory)
        if saved:
            persona.set_traits(saved)
        return persona

    def test_saved_traits_win_over_config(self):
        self.memory.set_preference(traits_module.PREFERENCE_KEY, '{"幽默": 95}')
        persona = self.apply_like_startup(
            DefaultPersonaProvider(traits={"理性": 70})
        )
        profile = persona.get_profile()
        self.assertEqual(profile["traits"], {"幽默": 95})
        self.assertIn("幽默", profile["traits_desc"])

    def test_nothing_saved_keeps_config_traits(self):
        persona = self.apply_like_startup(DefaultPersonaProvider(traits={"理性": 70}))
        self.assertEqual(persona.get_profile()["traits"], {"理性": 70})


# ---------------------------------------------------------------- 反馈
class FeedbackUndoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.memory, self.database = make_memory(self.tmp.name)

    def test_archive_hides_feedback_but_keeps_the_row(self):
        number = self.memory.remember("以后不要这么啰嗦", kind="feedback", importance=2)
        self.assertEqual(len(self.memory.feedback_memories()), 1)
        self.assertTrue(self.memory.archive_memory(number))
        self.assertEqual(self.memory.feedback_memories(), [])
        row = self.memory.repository.get_memory(number)
        self.assertIsNotNone(row)
        self.assertEqual(row["archived"], 1)
        self.assertIn("啰嗦", row["content"])

    def test_archive_can_be_undone(self):
        number = self.memory.remember("以后不要这么啰嗦", kind="feedback")
        self.memory.archive_memory(number)
        self.assertTrue(self.memory.archive_memory(number, archived=False))
        self.assertEqual(len(self.memory.feedback_memories()), 1)

    def test_archived_feedback_leaves_the_context(self):
        number = self.memory.remember("以后不要这么啰嗦", kind="feedback")
        _recent, before, _summary = self.memory.recall("s1", "啰嗦")
        self.assertIn("啰嗦", before)
        self.memory.archive_memory(number)
        _recent, after, _summary = self.memory.recall("s1", "啰嗦")
        self.assertNotIn("啰嗦", after)

    def test_persona_note_drops_undone_feedback(self):
        number = self.memory.remember("以后不要这么啰嗦", kind="feedback", importance=2)
        provider = DefaultPersonaProvider(feedback=lambda: self.memory.feedback_memories())
        self.assertIn("啰嗦", build_persona_note(provider))
        self.memory.archive_memory(number)
        self.assertNotIn("啰嗦", build_persona_note(provider))

    def test_archive_failure_is_not_fatal(self):
        broken = MemoryManager(_BrokenRepository())
        self.assertFalse(broken.archive_memory(1))


class _BrokenRepository(MemoryRepository):
    """归档接口坏掉的库，用来确认失败不会拖垮桌宠。"""

    def __init__(self):
        pass

    def set_archived(self, *args, **kwargs):
        raise RuntimeError("库坏了")


class TrainingExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        database = Database(Path(self.tmp.name) / "t.sqlite3")
        database.migrate()
        self.collector = LocalTrainingCollector(database)

    def test_collect_and_export(self):
        self.assertIsNotNone(self.collector.collect("feedback", "以后别这么正式"))
        payload = self.collector.export()
        self.assertEqual(payload["version"], 1)
        self.assertEqual(len(payload["samples"]), 1)
        self.assertEqual(payload["samples"][0]["content"], "以后别这么正式")

    def test_unknown_kind_is_refused(self):
        self.assertIsNone(self.collector.collect("随便", "内容"))

    def test_empty_payload_is_refused(self):
        self.assertIsNone(self.collector.collect("feedback", "   "))

    def test_status_counts_and_never_claims_training(self):
        self.collector.collect("feedback", "一")
        self.collector.collect("correction", "二")
        status = self.collector.status()
        self.assertEqual(status["total"], 2)
        self.assertFalse(status["training"])


# ---------------------------------------------------------------- 关系
class RelationshipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.memory, self.database = make_memory(self.tmp.name)

    def test_reset_needs_explicit_confirmation(self):
        self.memory.relationship.touch()
        self.assertFalse(self.memory.relationship.reset())
        self.assertEqual(self.memory.relationship.interactions(), 1)

    def test_reset_clears_counters(self):
        self.memory.relationship.touch()
        self.memory.relationship.touch()
        self.assertEqual(self.memory.relationship.interactions(), 2)
        self.assertTrue(self.memory.relationship.reset(confirm=True))
        describe = self.memory.relationship.describe()
        self.assertEqual(describe["interactions"], 0)
        self.assertEqual(describe["days"], 0)
        self.assertEqual(describe["stage"], "刚认识")

    def test_reset_keeps_memories(self):
        number = self.memory.remember("用户喜欢美式咖啡", kind="relationship")
        self.memory.relationship.touch()
        self.memory.relationship.reset(confirm=True)
        self.assertIsNotNone(self.memory.repository.get_memory(number))

    def test_reset_without_database_is_refused(self):
        from memory.relationship import RelationshipState

        self.assertFalse(RelationshipState(database=None).reset(confirm=True))


# ---------------------------------------------------------------- 主动提醒阀门
class SchedulerValveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.memory, _ = make_memory(self.tmp.name)
        self.bus = EventBus()
        self.rule = CompanionRule(
            name="work_long",
            situation="提醒休息",
            cooldown_seconds=0,
            max_per_day=5,
            condition=lambda ctx: True,
        )

    def build(self, **kwargs):
        return Scheduler(self.bus, rules=[self.rule], preferences=self.memory, **kwargs)

    def test_cooldown_preference_blocks(self):
        scheduler = self.build()
        scheduler._mark_fired(self.rule, 1000.0)
        self.assertTrue(scheduler.evaluate(self.rule, 1010.0).allowed)
        self.memory.set_preference("proactive.cooldown_seconds", "600")
        outcome = scheduler.evaluate(self.rule, 1010.0)
        self.assertFalse(outcome.allowed)
        self.assertIn("冷却", outcome.reason)

    def test_user_valve_only_tightens(self):
        strict = CompanionRule(
            name="strict", situation="提醒", cooldown_seconds=3600, condition=lambda ctx: True
        )
        scheduler = Scheduler(
            self.bus, rules=[strict], preferences=self.memory
        )
        scheduler._mark_fired(strict, 1000.0)
        self.memory.set_preference("proactive.cooldown_seconds", "5")
        self.assertFalse(scheduler.evaluate(strict, 1010.0).allowed)

    def test_config_floor_applies_without_preference(self):
        scheduler = self.build(cooldown_floor=300)
        scheduler._mark_fired(self.rule, 1000.0)
        self.assertFalse(scheduler.evaluate(self.rule, 1010.0).allowed)

    def test_daily_limit_preference_caps(self):
        scheduler = self.build()
        self.memory.set_preference("proactive.max_per_day", "1")
        now = 1000.0
        scheduler._fired_today[scheduler._day_key(self.rule, now)] = 1
        outcome = scheduler.evaluate(self.rule, now)
        self.assertFalse(outcome.allowed)
        self.assertIn("上限", outcome.reason)

    def test_daily_limit_never_raises_the_rule(self):
        scheduler = self.build()
        self.memory.set_preference("proactive.max_per_day", "99")
        self.assertEqual(scheduler._daily_limit(self.rule), 5)

    def test_garbage_preference_falls_back(self):
        scheduler = self.build(cooldown_floor=120)
        self.memory.set_preference("proactive.cooldown_seconds", "很久")
        self.assertEqual(scheduler._cooldown(self.rule), 120)

    def test_proactive_off_still_wins(self):
        scheduler = self.build()
        self.memory.set_preference("proactive_enabled", "0")
        outcome = scheduler.evaluate(self.rule, 1000.0)
        self.assertFalse(outcome.allowed)
        self.assertIn("关闭", outcome.reason)


# ---------------------------------------------------------------- 配置与上下文
class ConfigValveTests(unittest.TestCase):
    def test_defaults_are_zero(self):
        config = Config(load_env=False)
        self.assertEqual(config.proactive_cooldown, 0)
        self.assertEqual(config.proactive_max_per_day, 0)

    def test_values_are_bounded(self):
        with mock.patch.dict(
            os.environ,
            {"DESKPET_PROACTIVE_COOLDOWN": "999999", "DESKPET_PROACTIVE_MAX_PER_DAY": "-5"},
        ):
            config = Config(load_env=False)
        self.assertEqual(config.proactive_cooldown, 86400)
        self.assertEqual(config.proactive_max_per_day, 0)

    def test_redacted_lists_the_valves(self):
        data = Config(load_env=False).redacted()
        self.assertIn("proactive_cooldown", data)
        self.assertIn("proactive_max_per_day", data)
        self.assertIn("proactive", data)


class PreferencePrivacyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.memory, _ = make_memory(self.tmp.name)

    def test_internal_settings_never_reach_the_model(self):
        self.memory.set_preference("persona.traits", '{"幽默": 90}')
        self.memory.set_preference("proactive.cooldown_seconds", "600")
        self.memory.set_preference("proactive_enabled", "1")
        _recent, block, _summary = self.memory.recall("s1", "你好")
        for hidden in ("幽默", "600", "proactive"):
            self.assertNotIn(hidden, block)

    def test_user_preference_is_still_visible(self):
        self.memory.set_preference("喜欢的饮料", "美式")
        _recent, block, _summary = self.memory.recall("s1", "你好")
        self.assertIn("美式", block)


# ---------------------------------------------------------------- 面板界面
class ModeLineTests(unittest.TestCase):
    def test_missing_behavior_says_so(self):
        self.assertIn("没有接入", mode_lines(None)[0])

    def test_lines_describe_modes(self):
        class FakeBehavior:
            def describe(self):
                return {
                    "quiet": True,
                    "quiet_hours": [23, 7],
                    "proactive_allowed": False,
                    "sleeping": False,
                    "user_away": True,
                    "window_busy": True,
                }

        text = "\n".join(mode_lines(FakeBehavior()))
        self.assertIn("静默模式：开", text)
        self.assertIn("23-7", text)
        self.assertIn("已关闭", text)
        self.assertIn("用户不在电脑前", text)
        self.assertIn("专注模式", text)


class TkTestCase(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except Exception as exc:
            self.skipTest(f"tkinter 不可用：{exc}")
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

    def _safe_destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def build_pet(self, **kwargs):
        database = Database(Path(self.tmp.name) / "t.sqlite3")
        database.migrate()
        memory = MemoryManager(MemoryRepository(database), **kwargs)
        agent = AgentCore(
            StubProvider(),
            memory,
            ContextManager(token_budget=800),
            persona=DefaultPersonaProvider(),
            training=LocalTrainingCollector(database),
        )
        pet = DesktopPet(
            agent,
            self.config,
            memory=memory,
            behavior=BehaviorManager(persona=agent.persona),
            root=self.root,
        )
        return pet, memory


class PersonaWindowTests(TkTestCase):
    def test_menu_entry_is_wired(self):
        pet, _ = self.build_pet()
        self.assertIn(("人格", "open_persona"), DesktopPet.MENU_ITEMS)
        self.assertTrue(callable(pet.open_persona))

    def test_window_lists_feedback(self):
        pet, memory = self.build_pet()
        memory.remember("以后不要这么啰嗦", kind="feedback", importance=2)
        window = pet.open_persona()
        self.addCleanup(window.win.destroy)
        self.assertIsInstance(window, PersonaWindow)
        self.assertEqual(len(window.tree.get_children()), 1)

    def test_window_is_not_stacked_twice(self):
        pet, _ = self.build_pet()
        first = pet.open_persona()
        self.addCleanup(first.win.destroy)
        self.assertIs(first, pet.open_persona())

    def test_saving_traits_applies_and_persists(self):
        pet, memory = self.build_pet()
        window = pet.open_persona()
        self.addCleanup(window.win.destroy)
        window.vars["幽默"].set(85)
        self.assertTrue(window.save_traits())
        self.assertEqual(
            traits_module.load(memory), {name: var.get() for name, var in window.vars.items()}
        )
        profile = pet.agent.persona.get_profile()
        self.assertEqual(profile["traits"]["幽默"], 85)
        self.assertIn("幽默", build_persona_note(pet.agent.persona))

    def test_reset_traits_returns_to_config(self):
        pet, memory = self.build_pet()
        window = pet.open_persona()
        self.addCleanup(window.win.destroy)
        window.vars["幽默"].set(90)
        window.save_traits()
        window.reset_traits()
        # 没配置过默认性格，就回到面板默认强度 50。
        self.assertEqual(window.vars["幽默"].get(), traits_module.DEFAULT_INTENSITY)
        self.assertEqual(
            window.current_traits()["幽默"], traits_module.DEFAULT_INTENSITY
        )

    def test_saving_without_memory_still_applies(self):
        pet, _ = self.build_pet()
        window = pet.open_persona()
        self.addCleanup(window.win.destroy)
        pet.memory = None
        window.memory = None
        window.vars["温柔"].set(75)
        self.assertTrue(window.save_traits())
        self.assertEqual(pet.agent.persona.get_profile()["traits"]["温柔"], 75)
        self.assertIn("没存下来", str(window.hint.cget("text")))

    def test_undo_feedback_needs_confirmation(self):
        pet, memory = self.build_pet()
        number = memory.remember("以后不要这么啰嗦", kind="feedback", importance=2)
        window = pet.open_persona()
        self.addCleanup(window.win.destroy)
        window.tree.selection_set(str(number))
        with mock.patch.object(persona_view.messagebox, "askyesno", return_value=False):
            self.assertFalse(window.undo_feedback())
        self.assertEqual(memory.repository.get_memory(number)["archived"], 0)
        with mock.patch.object(persona_view.messagebox, "askyesno", return_value=True):
            self.assertTrue(window.undo_feedback())
        self.assertEqual(memory.repository.get_memory(number)["archived"], 1)
        self.assertEqual(len(window.tree.get_children()), 0)

    def test_undo_without_selection_is_refused(self):
        pet, _ = self.build_pet()
        window = pet.open_persona()
        self.addCleanup(window.win.destroy)
        self.assertFalse(window.undo_feedback())

    def test_export_samples_writes_a_file(self):
        pet, memory = self.build_pet()
        memory.remember("以后别这么正式", kind="feedback")
        pet.agent.training.collect("feedback", "以后别这么正式")
        window = pet.open_persona()
        self.addCleanup(window.win.destroy)
        with mock.patch.object(PersonaWindow, "_export_dir", return_value=Path(self.tmp.name)):
            self.assertTrue(window.export_samples())
        written = Path(self.tmp.name) / "persona-samples.json"
        payload = json.loads(written.read_text(encoding="utf-8"))
        self.assertEqual(payload["samples"][0]["content"], "以后别这么正式")

    def test_export_without_samples_is_refused(self):
        pet, _ = self.build_pet()
        window = pet.open_persona()
        self.addCleanup(window.win.destroy)
        with mock.patch.object(PersonaWindow, "_export_dir", return_value=Path(self.tmp.name)):
            self.assertFalse(window.export_samples())

    def test_reset_relationship_needs_confirmation(self):
        pet, memory = self.build_pet()
        memory.relationship.touch()
        memory.relationship.touch()
        window = pet.open_persona()
        self.addCleanup(window.win.destroy)
        with mock.patch.object(persona_view.messagebox, "askyesno", return_value=False):
            self.assertFalse(window.reset_relationship())
        self.assertEqual(memory.relationship.interactions(), 2)
        with mock.patch.object(persona_view.messagebox, "askyesno", return_value=True):
            self.assertTrue(window.reset_relationship())
        self.assertEqual(memory.relationship.interactions(), 0)

    def test_status_sections_are_separate(self):
        pet, memory = self.build_pet()
        memory.relationship.touch()
        window = pet.open_persona()
        self.addCleanup(window.win.destroy)
        window.refresh_status()
        self.assertIn("人格设定", str(window.persona_label.cget("text")))
        self.assertIn("即时情绪", str(window.emotion_label.cget("text")))
        self.assertIn("关系状态", str(window.relation_label.cget("text")))
        self.assertIn("静默模式", str(window.mode_label.cget("text")))

    def test_window_degrades_when_memory_is_missing(self):
        pet, _ = self.build_pet()
        pet.memory = None
        window = pet.open_persona()
        self.addCleanup(window.win.destroy)
        self.assertEqual(window.refresh(), [])
        self.assertIn("记忆不可用", str(window.relation_label.cget("text")))

    def test_traits_loaded_from_preferences_on_open(self):
        pet, memory = self.build_pet()
        traits_module.save(memory, {"好奇": 80})
        window = pet.open_persona()
        self.addCleanup(window.win.destroy)
        self.assertEqual(window.vars["好奇"].get(), 80)


class SettingsWiringTests(TkTestCase):
    def test_settings_exposes_the_persona_tab(self):
        pet, _ = self.build_pet()
        window = pet.open_settings()
        self.addCleanup(window.win.destroy)
        titles = [window.book.tab(index, "text") for index in range(window.book.index("end"))]
        self.assertIn("人格", titles)
        self.assertIn("基础", titles)

    def test_settings_persona_tab_has_a_way_in(self):
        pet, _ = self.build_pet()
        window = pet.open_settings()
        self.addCleanup(window.win.destroy)
        self.assertTrue(callable(pet.open_persona))
        self.assertTrue(pet.persona_summary())

    def test_persona_summary_separates_the_parts(self):
        pet, memory = self.build_pet()
        memory.relationship.touch()
        names = [name for name, _ in pet.persona_summary()]
        for expected in ("人格", "即时情绪", "关系", "行为模式"):
            self.assertIn(expected, names)

    def test_saving_proactive_number_stores_it(self):
        pet, memory = self.build_pet()
        window = pet.open_settings()
        self.addCleanup(window.win.destroy)
        pet.cooldown_entry.delete(0, tk.END)
        pet.cooldown_entry.insert(0, "900")
        self.assertEqual(
            pet._save_proactive_number(
                "proactive.cooldown_seconds", pet.cooldown_entry, 0, 86400
            ),
            900,
        )
        self.assertEqual(memory.get_preference("proactive.cooldown_seconds"), "900")

    def test_saving_proactive_number_rejects_garbage(self):
        pet, _ = self.build_pet()
        window = pet.open_settings()
        self.addCleanup(window.win.destroy)
        pet.daily_entry.delete(0, tk.END)
        pet.daily_entry.insert(0, "很多次")
        self.assertIsNone(
            pet._save_proactive_number("proactive.max_per_day", pet.daily_entry, 0, 100)
        )

    def test_settings_rows_show_the_valves(self):
        pet, _ = self.build_pet()
        rows = dict(pet.settings_rows())
        self.assertIn("主动冷却", rows)
        self.assertIn("每日上限", rows)


# ---------------------------------------------------------------- 启动装配
class StartupWiringTests(unittest.TestCase):
    """走真实的 build_agent，确认保存过的性格在启动时被套回去。"""

    def setUp(self):
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
        self.logger = configure(self.config.log_dir, "INFO", False)
        self.addCleanup(self._close_logs)

    def _close_logs(self):
        """先把日志文件关掉，临时目录才删得干净。"""
        import logging

        logger = logging.getLogger("deskpet")
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)

    def build(self):
        from app.main import build_agent

        database = Database(self.config.db_path)
        database.migrate()
        return build_agent(
            self.config, database, StubProvider(), self.logger, bus=EventBus()
        )

    def test_build_agent_without_any_traits(self):
        agent, memory = self.build()
        self.assertIsNotNone(agent.persona)
        self.assertFalse(traits_module.load(memory))

    def test_saved_traits_are_applied_on_startup(self):
        database = Database(self.config.db_path)
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        traits_module.save(memory, {"活泼": 90, "好奇": 70})
        agent, _ = self.build()
        profile = agent.persona.get_profile()
        self.assertEqual(profile["traits"], {"活泼": 90, "好奇": 70})
        self.assertEqual(agent.behavior._persona_factor(), 0.6)

    def test_scheduler_gets_the_config_valves(self):
        from app.main import build_scheduler

        database = Database(self.config.db_path)
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        bus = EventBus()
        scheduler = build_scheduler(self.config, memory, self.logger, bus)
        self.assertEqual(scheduler.cooldown_floor, self.config.proactive_cooldown)
        self.assertEqual(scheduler.daily_limit, self.config.proactive_max_per_day)


if __name__ == "__main__":
    unittest.main()
