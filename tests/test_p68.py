"""DeskPet V6 阶段 C 验收：习惯学习、置信度与用户校正。

覆盖：一次提到只算候选、说够次数且一致才自动采纳、显式设置永远优先、
称呼与雷区必须用户点、冲突不互相覆盖、拒绝与删除之后不再生效、
学习开关关掉就不再记录也不进提示词、依据只存短句不存整段聊天、
学习记录只写偏好键且进不了模型上下文、面板上的接受/拒绝/删除/清空。
全程使用临时数据库，不碰真实用户数据。
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
from app.main import build_agent
from database.db import Database
from logkit.logger import get_logger
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatDelta, ChatResult, ModelProvider, Usage
from persona import habits as habits_module
from persona import learner as learner_module
from persona.interface import DefaultPersonaProvider, build_persona_note
from security.config import Config
from ui import persona_workbench
from ui.desktop import DesktopPet
from ui.persona_view import PersonaWindow

OBSERVED_HEAD = "以下是从对话里观察到的偏好（可能不准）："


class StubProvider(ModelProvider):
    name = "stub"

    def __init__(self, reply="好。"):
        self.reply = reply
        self.calls = []

    def is_available(self):
        return True

    def chat(self, messages, tools=None, temperature=0.7):
        self.calls.append(list(messages))
        return ChatResult(
            ok=True,
            content=self.reply,
            usage=Usage(prompt_tokens=5, completion_tokens=2),
            model="stub",
        )

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Database(Path(self.tmp.name) / "t.sqlite3")
        self.database.migrate()
        self.memory = MemoryManager(MemoryRepository(self.database))
        self.persona = DefaultPersonaProvider()
        self.learner = learner_module.HabitLearner(
            preferences=self.memory, persona=self.persona
        )

    def say(self, text, times=1):
        for _ in range(times):
            self.learner.observe(text)
        return self.learner.rows()

    def note(self):
        return build_persona_note(self.persona)

    def row(self, identifier):
        for item in self.learner.rows():
            if item.get("id") == identifier:
                return item
        return None

    def set_habits(self, values):
        habits_module.save(self.memory, values)
        return self.persona.set_habits(values)


class ProposalTests(unittest.TestCase):
    def test_unrelated_talk_proposes_nothing(self):
        self.assertEqual(learner_module.propose("今天天气不错，出去走走"), [])

    def test_empty_text_proposes_nothing(self):
        self.assertEqual(learner_module.propose(""), [])
        self.assertEqual(learner_module.propose(None), [])

    def test_a_length_request_is_picked_up(self):
        found = learner_module.propose("以后回答简短一点")
        self.assertIn(("length", "简短", "以后回答简短一点"), found)

    def test_avoid_captures_the_thing_not_the_sentence(self):
        found = dict((field, value) for field, value, _ in learner_module.propose("不要用表格"))
        self.assertEqual(found.get("avoid"), "用表格")

    def test_address_captures_the_name(self):
        found = dict((field, value) for field, value, _ in learner_module.propose("以后叫我阿墨"))
        self.assertEqual(found.get("address"), "阿墨")

    def test_only_known_fields_are_proposed(self):
        for field, _value, _word in learner_module.RULES:
            self.assertIn(field, habits_module.FIELDS)

    def test_confidence_grows_with_repeats(self):
        first = learner_module.confidence(1, directive=True)
        third = learner_module.confidence(3, directive=True)
        self.assertLess(first, third)
        self.assertLessEqual(third, 0.95)

    def test_confidence_drops_when_two_values_conflict(self):
        plain = learner_module.confidence(3, directive=True)
        clashing = learner_module.confidence(3, directive=True, conflict=True)
        self.assertLess(clashing, plain)

class LearningTests(Base):
    def test_one_mention_is_only_a_candidate(self):
        self.say("以后回答简短一点")
        row = self.row("length|简短")
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "candidate")
        self.assertEqual(row["seen"], 1)
        self.assertNotIn("回复长度", self.note())

    def test_one_mention_never_reaches_the_prompt(self):
        self.say("以后回答简短一点")
        self.assertNotIn(OBSERVED_HEAD, self.note())

    def test_two_mentions_are_not_enough(self):
        self.say("以后回答简短一点", times=2)
        self.assertEqual(self.row("length|简短")["status"], "candidate")

    def test_three_consistent_mentions_are_accepted(self):
        self.say("以后回答简短一点", times=3)
        row = self.row("length|简短")
        self.assertEqual(row["status"], "accepted")
        self.assertTrue(row["auto"])
        note = self.note()
        self.assertIn(OBSERVED_HEAD, note)
        self.assertIn("回复长度：简短", note)

    def test_a_plain_statement_needs_more_repeats(self):
        self.say("回答简短一点", times=3)
        self.assertEqual(self.row("length|简短")["status"], "candidate")
        self.say("回答简短一点")
        self.assertEqual(self.row("length|简短")["status"], "accepted")

    def test_the_users_own_setting_wins(self):
        self.say("以后回答简短一点", times=3)
        self.set_habits({"length": "详细"})
        note = self.note()
        self.assertIn("回复长度：详细", note)
        self.assertNotIn("回复长度：简短", note)
        self.assertNotIn(OBSERVED_HEAD, note)

    def test_sensitive_fields_wait_for_the_user(self):
        self.say("以后叫我阿墨", times=5)
        self.assertEqual(self.row("address|阿墨")["status"], "candidate")
        self.assertNotIn("阿墨", self.note())
        self.assertTrue(self.learner.accept("address|阿墨"))
        note = self.note()
        self.assertIn("称呼用户为「阿墨」", note)
        self.assertIn(OBSERVED_HEAD, note)

    def test_avoid_is_never_auto_accepted(self):
        self.say("不要用表格", times=5)
        self.assertEqual(self.row("avoid|用表格")["status"], "candidate")
        self.assertNotIn("用表格", self.note())

    def test_conflicting_values_do_not_overwrite_each_other(self):
        self.say("以后回答简短一点", times=3)
        self.say("以后回答详细一点", times=3)
        short = self.row("length|简短")
        longer = self.row("length|详细")
        self.assertIsNotNone(short)
        self.assertIsNotNone(longer)
        self.assertLess(longer["confidence"], learner_module.confidence(3, directive=True))
        self.assertIn("length", self.learner.conflicts())
        hit = [value for value in ("简短", "详细") if value in self.note()]
        self.assertEqual(len(hit), 1)

    def test_rejection_stops_it_for_good(self):
        self.say("以后回答简短一点", times=3)
        self.assertTrue(self.learner.reject("length|简短"))
        self.assertNotIn("回复长度：简短", self.note())
        before = len(self.learner.evidence("length|简短"))
        self.say("以后回答简短一点", times=4)
        self.assertEqual(self.row("length|简短")["status"], "rejected")
        self.assertEqual(len(self.learner.evidence("length|简短")), before)
        self.assertNotIn("回复长度：简短", self.note())

    def test_removing_a_record_drops_it(self):
        self.say("以后回答简短一点", times=3)
        self.assertTrue(self.learner.remove("length|简短"))
        self.assertIsNone(self.row("length|简短"))
        self.assertNotIn(OBSERVED_HEAD, self.note())
        self.assertFalse(self.learner.remove("length|简短"))

    def test_editing_makes_it_a_candidate_again(self):
        self.say("以后回答简短一点", times=3)
        self.assertTrue(self.learner.edit("length|简短", "适中"))
        self.assertIsNone(self.row("length|简短"))
        self.assertEqual(self.row("length|适中")["status"], "candidate")
        self.assertFalse(self.learner.edit("length|适中", "   "))
        self.assertTrue(self.learner.accept("length|适中"))
        self.assertIn("回复长度：适中", self.note())

    def test_unknown_identifier_changes_nothing(self):
        self.assertFalse(self.learner.accept("nope|nope"))
        self.assertFalse(self.learner.reject("nope|nope"))
        self.assertFalse(self.learner.remove("nope|nope"))
        self.assertEqual(self.learner.evidence("nope|nope"), [])

    def test_learning_off_records_nothing(self):
        self.learner.set_enabled(False)
        self.assertEqual(self.learner.observe("以后回答简短一点"), [])
        self.assertEqual(self.learner.rows(), [])

    def test_turning_learning_off_pulls_the_observed_lines_back(self):
        self.say("以后回答简短一点", times=3)
        self.assertIn(OBSERVED_HEAD, self.note())
        self.learner.set_enabled(False)
        self.assertNotIn(OBSERVED_HEAD, self.note())
        self.assertNotIn("回复长度：简短", self.note())
        self.learner.set_enabled(True)
        self.assertIn(OBSERVED_HEAD, self.note())

    def test_evidence_keeps_short_snippets_only(self):
        body = "以后回答简短一点" + "顺便说说今天的事" * 20
        self.say(body, times=8)
        evidence = self.learner.evidence("length|简短")
        self.assertLessEqual(len(evidence), learner_module.MAX_EVIDENCE)
        for item in evidence:
            self.assertLessEqual(len(item["text"]), learner_module.MAX_EVIDENCE_CHARS)
        self.assertLess(len(evidence[0]["text"]), len(body))

    def test_records_are_sorted_by_field_order(self):
        self.say("以后回答简短一点")
        self.say("以后叫我阿墨")
        fields = [row["field"] for row in self.learner.rows()]
        order = list(habits_module.FIELDS)
        self.assertEqual(fields, sorted(fields, key=order.index))

    def test_summary_counts_by_status(self):
        self.say("以后回答简短一点", times=3)
        self.say("以后叫我阿墨")
        self.say("不要用表格")
        self.learner.reject("avoid|用表格")
        info = self.learner.summary()
        self.assertEqual(info["accepted"], 1)
        self.assertEqual(info["rejected"], 1)
        self.assertEqual(info["candidate"], 1)
        self.assertEqual(info["applied"], 1)
        self.assertTrue(info["learning"])

    def test_clear_keeps_the_users_own_settings(self):
        self.set_habits({"tone": "专业"})
        self.say("以后回答简短一点", times=3)
        self.assertTrue(self.learner.clear())
        self.assertEqual(self.learner.rows(), [])
        self.assertEqual(self.persona.habits["tone"], "专业")
        self.assertIn("语言风格：专业", self.note())

    def test_the_learner_cannot_reach_anything_but_preferences(self):
        for name in ("execute", "run", "grant", "set_permission", "install", "connect"):
            self.assertFalse(hasattr(self.learner, name))
        self.say("以后回答简短一点", times=3)
        keys = [row["key"] for row in self.database.query("SELECT key FROM preferences")]
        self.assertTrue(keys)
        for key in keys:
            self.assertTrue(key.startswith("persona."), key)

    def test_learning_records_stay_out_of_the_model_block(self):
        self.say("以后回答简短一点", times=3)
        _recent, block, _summary = self.memory.recall("s1", "简短")
        self.assertNotIn(learner_module.KEY, block)
        self.assertNotIn("persona.habits", block)

    def test_broken_preferences_do_not_crash_the_learner(self):
        broken = learner_module.HabitLearner(preferences=None, persona=self.persona)
        self.assertFalse(broken.enabled())
        self.assertEqual(broken.rows(), [])
        self.assertEqual(broken.observe("以后回答简短一点"), [])

    def test_a_persona_that_cannot_take_observations_says_so(self):
        learner = learner_module.HabitLearner(preferences=self.memory, persona=object())
        self.assertFalse(learner.apply()["ok"])


class AgentWiringTests(Base):
    def test_a_real_turn_feeds_the_learner(self):
        self.agent = AgentCore(
            StubProvider(), self.memory, ContextManager(token_budget=800), persona=self.persona
        )
        self.agent.learning = self.learner
        self.agent.respond("s1", "以后回答简短一点")
        self.assertEqual(self.row("length|简短")["seen"], 1)

    def test_a_broken_learner_does_not_break_the_turn(self):
        class Boom:
            def observe(self, text):
                raise RuntimeError("坏了")

        agent = AgentCore(
            StubProvider(), self.memory, ContextManager(token_budget=800), persona=self.persona
        )
        agent.learning = Boom()
        reply = agent.respond("s2", "以后回答简短一点")
        self.assertTrue(reply.ok)

    def test_no_learner_is_fine(self):
        agent = AgentCore(
            StubProvider(), self.memory, ContextManager(token_budget=800), persona=self.persona
        )
        self.assertTrue(agent.respond("s3", "在吗").ok)

class StartupTests(Base):
    def config(self):
        base = Path(self.tmp.name)
        config = Config(load_env=False)
        config.data_dir = base
        config.log_dir = base / "logs"
        config.db_path = base / "boot.sqlite3"
        config.characters_dir = base / "characters"
        config.live2d_dir = base / "live2d"
        config.devices_dir = base / "devices"
        return config

    def boot(self, config):
        database = Database(config.db_path)
        database.migrate()
        return build_agent(config, database, StubProvider(), get_logger())

    def test_startup_attaches_the_learner_and_keeps_its_records(self):
        config = self.config()
        agent, memory = self.boot(config)
        self.assertIsNotNone(agent.learning)
        agent.learning.observe("以后回答简短一点")
        agent.learning.observe("以后回答简短一点")
        agent.learning.observe("以后回答简短一点")
        self.assertIn("回复长度：简短", build_persona_note(agent.persona))
        # 关掉再打开：观察到的偏好跟着提示词一起回来。
        again, _ = self.boot(config)
        self.assertIn("回复长度：简短", build_persona_note(again.persona))

    def test_startup_stays_quiet_when_learning_was_turned_off(self):
        config = self.config()
        agent, _memory = self.boot(config)
        agent.learning.observe("以后回答简短一点")
        agent.learning.observe("以后回答简短一点")
        agent.learning.observe("以后回答简短一点")
        agent.learning.set_enabled(False)
        again, _ = self.boot(config)
        self.assertNotIn("回复长度：简短", build_persona_note(again.persona))


class TkTests(unittest.TestCase):
    """真窗口：面板上的接受 / 拒绝 / 删除 / 清空。"""

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

    def build_pet(self):
        database = Database(self.config.db_path)
        database.migrate()
        agent, memory = build_agent(self.config, database, StubProvider(), get_logger())
        pet = DesktopPet(agent, self.config, memory=memory, root=self.root)
        return pet, memory

    def open_window(self, pet):
        window = pet.open_persona()
        self.addCleanup(window.win.destroy)
        return window

    def seed(self, pet, text, times=3):
        for _ in range(times):
            pet.agent.learning.observe(text)
        return pet.agent.learning.rows()

    def test_window_has_the_learning_tab(self):
        pet, _ = self.build_pet()
        window = self.open_window(pet)
        titles = [window.book.tab(index, "text") for index in range(window.book.index("end"))]
        self.assertIn("学习", titles)

    def test_the_panel_lists_candidates_with_their_evidence(self):
        pet, _ = self.build_pet()
        self.seed(pet, "以后回答简短一点", times=1)
        window = self.open_window(pet)
        self.assertEqual(len(window.learn_tree.get_children()), 1)
        self.assertIn("以后回答简短一点", window.learn_evidence.get("1.0", "end"))
        self.assertIn("候选", str(window.learn_status.cget("text")))

    def test_accepting_a_suggestion_from_the_panel_applies_it(self):
        pet, _ = self.build_pet()
        self.seed(pet, "以后叫我阿墨", times=3)
        window = self.open_window(pet)
        window.learn_tree.selection_set("address|阿墨")
        self.assertTrue(window.accept_candidate())
        self.assertIn("称呼用户为「阿墨」", build_persona_note(pet.agent.persona))

    def test_rejecting_a_suggestion_pulls_it_back(self):
        pet, _ = self.build_pet()
        self.seed(pet, "以后回答简短一点", times=3)
        window = self.open_window(pet)
        self.assertIn("回复长度：简短", build_persona_note(pet.agent.persona))
        window.learn_tree.selection_set("length|简短")
        self.assertTrue(window.reject_candidate())
        self.assertNotIn("回复长度：简短", build_persona_note(pet.agent.persona))

    def test_deleting_a_record_asks_first(self):
        pet, _ = self.build_pet()
        self.seed(pet, "以后回答简短一点", times=3)
        window = self.open_window(pet)
        window.learn_tree.selection_set("length|简短")
        with mock.patch.object(persona_workbench.messagebox, "askyesno", return_value=False):
            self.assertFalse(window.remove_candidate())
        self.assertEqual(len(window.learn_tree.get_children()), 1)
        with mock.patch.object(persona_workbench.messagebox, "askyesno", return_value=True):
            self.assertTrue(window.remove_candidate())
        self.assertEqual(len(window.learn_tree.get_children()), 0)

    def test_clearing_records_asks_first(self):
        pet, _ = self.build_pet()
        self.seed(pet, "以后回答简短一点", times=3)
        window = self.open_window(pet)
        with mock.patch.object(persona_workbench.messagebox, "askyesno", return_value=False):
            self.assertFalse(window.clear_candidates())
        self.assertEqual(len(window.learn_tree.get_children()), 1)
        with mock.patch.object(persona_workbench.messagebox, "askyesno", return_value=True):
            self.assertTrue(window.clear_candidates())
        self.assertEqual(len(window.learn_tree.get_children()), 0)

    def test_the_panel_switch_stops_recording(self):
        pet, _ = self.build_pet()
        window = self.open_window(pet)
        self.assertTrue(window.learning_var.get())
        window.learning_var.set(0)
        self.assertTrue(window.toggle_learning())
        self.assertFalse(pet.agent.learning.enabled())
        self.assertEqual(pet.agent.learning.observe("以后回答简短一点"), [])
        self.assertEqual(pet.agent.learning.rows(), [])

    def test_actions_without_a_selection_are_refused(self):
        pet, _ = self.build_pet()
        window = self.open_window(pet)
        self.assertFalse(window.accept_candidate())
        self.assertFalse(window.reject_candidate())
        self.assertFalse(window.remove_candidate())

    def test_host_methods_report_nothing_when_learning_is_missing(self):
        pet, _ = self.build_pet()
        pet.agent.learning = None
        self.assertEqual(pet.persona_habit_candidates(), [])
        self.assertFalse(pet.accept_persona_habit("length|简短"))
        self.assertEqual(pet.persona_habit_evidence("length|简短"), [])
        self.assertFalse(pet.persona_habit_summary()["learning"])

    def test_archived_feedback_leaves_the_prompt(self):
        pet, memory = self.build_pet()
        number = memory.remember("以后不要用这么正式的语气", kind="feedback", importance=2)
        self.assertIn("不要用这么正式的语气", build_persona_note(pet.agent.persona))
        memory.archive_memory(number)
        self.assertNotIn("不要用这么正式的语气", build_persona_note(pet.agent.persona))


if __name__ == "__main__":
    unittest.main()
