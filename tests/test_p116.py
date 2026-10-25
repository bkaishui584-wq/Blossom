"""P2：人格情境策略、主动行为门槛与提醒措辞。"""

import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.capabilities import CapabilityRouter
from behavior.manager import INTERACT, SILENT, WAIT, BehaviorManager
from persona import habits as habits_kit
from persona import policy as policy_module
from persona.interface import SAFETY_BOUNDARY, DefaultPersonaProvider, build_persona_note
from persona.policy import PersonaPolicy
from scheduler.reminders import TodoReminder


class FakePreferences:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get_preference(self, key):
        return self.values.get(key)

    def set_preference(self, key, value):
        self.values[key] = value


class FakeScheduler:
    def __init__(self):
        self.calls = []

    def schedule_once(self, seconds, **payload):
        self.calls.append((seconds, payload))
        return True


class FakeTodos:
    def __init__(self, row):
        self.row = row

    def scheduled(self):
        return [self.row]


class PersonaPolicyTests(unittest.TestCase):
    def test_mode_is_persisted_and_bad_values_fall_back(self):
        prefs = FakePreferences()
        policy = PersonaPolicy(prefs)
        self.assertEqual(policy.mode(), "companion")
        self.assertTrue(policy.set_mode("work"))
        self.assertEqual(PersonaPolicy(prefs).mode(), "work")
        self.assertTrue(policy.set_mode("nonsense"))
        self.assertEqual(policy.mode(), "companion")

    def test_context_modes_block_only_proactive_speech(self):
        policy = PersonaPolicy(FakePreferences())
        policy.set_mode("dnd")
        self.assertEqual(policy.decision()["verdict"], "silent")
        self.assertEqual(policy.decision(priority="high")["verdict"], "silent")

        policy.set_mode("work")
        self.assertEqual(policy.decision()["verdict"], "wait")
        self.assertEqual(policy.decision(priority="high")["verdict"], "interact")

        policy.set_mode("rest")
        self.assertEqual(policy.decision(priority="low")["verdict"], "silent")
        self.assertEqual(policy.decision()["verdict"], "wait")
        self.assertEqual(policy.decision(urgent=True)["verdict"], "interact")

    def test_explicit_habit_has_priority_over_default_proactive(self):
        prefs = FakePreferences()
        habits_kit.save(prefs, {"proactive": "不主动"})
        policy = PersonaPolicy(prefs)
        self.assertEqual(policy.decision()["verdict"], "silent")
        self.assertEqual(policy.decision(priority="high")["verdict"], "interact")

    def test_cooldown_factor_follows_explicit_habits(self):
        prefs = FakePreferences()
        habits_kit.save(prefs, {"proactive": "经常主动"})
        self.assertEqual(PersonaPolicy(prefs).cooldown_factor(), 0.5)
        habits_kit.save(prefs, {"proactive": "偶尔主动"})
        self.assertEqual(PersonaPolicy(prefs).cooldown_factor(), 2.0)
        habits_kit.save(prefs, {"remind": "多一点"})
        self.assertEqual(PersonaPolicy(prefs).cooldown_factor(), 0.75)

    def test_reminder_text_uses_explicit_address(self):
        prefs = FakePreferences()
        habits_kit.save(prefs, {"address": "阿墨"})
        policy = PersonaPolicy(prefs)
        self.assertEqual(
            policy.reminder_text("待办到时间了：喝水"),
            "阿墨，待办到时间了：喝水",
        )

    def test_context_line_enters_the_prompt_before_the_safety_boundary(self):
        policy = PersonaPolicy(FakePreferences())
        policy.set_mode("work")
        note = build_persona_note(
            DefaultPersonaProvider(name="小蝶"), extra=policy.prompt_lines()
        )
        self.assertIn("当前是工作情境", note)
        self.assertTrue(note.endswith(SAFETY_BOUNDARY))


class PersonaPolicyWiringTests(unittest.TestCase):
    def test_behavior_manager_uses_the_context_mode(self):
        prefs = FakePreferences()
        policy = PersonaPolicy(prefs)
        manager = BehaviorManager(
            preferences=prefs,
            policy=policy,
            clock=lambda: 1000,
            hour_of=lambda now: 12,
        )
        self.assertEqual(manager.should_interact(), INTERACT)

        policy.set_mode("work")
        self.assertEqual(manager.should_interact(), WAIT)
        self.assertEqual(manager.should_interact(priority="high"), INTERACT)

        policy.set_mode("dnd")
        self.assertEqual(manager.should_interact(), SILENT)

    def test_local_capability_switches_context_without_a_model(self):
        prefs = FakePreferences()
        policy = PersonaPolicy(prefs)
        router = CapabilityRouter(memory=prefs, policy=policy)

        answer = router.resolve("进入工作模式")
        self.assertIsNotNone(answer)
        self.assertEqual(answer.capability, "context")
        self.assertEqual(policy.mode(), "work")
        self.assertIn("工作", answer.text)

        answer = router.resolve("现在是什么情境")
        self.assertIsNotNone(answer)
        self.assertIn("工作", answer.text)

    def test_todo_reminder_uses_persona_text_without_changing_defaults(self):
        now = datetime(2026, 10, 21, 9, 0)
        row = {
            "id": 1,
            "title": "喝水",
            "due_at": "2026-10-21 10:00",
        }
        scheduler = FakeScheduler()
        prefs = FakePreferences()
        habits_kit.save(prefs, {"address": "阿墨"})
        reminder = TodoReminder(
            FakeTodos(row),
            scheduler=scheduler,
            clock=lambda: now,
            policy=PersonaPolicy(prefs),
        )
        self.assertEqual(reminder.arm(), 1)
        self.assertIn("阿墨", scheduler.calls[0][1]["local_text"])
        self.assertIn("喝水", scheduler.calls[0][1]["local_text"])

        plain_scheduler = FakeScheduler()
        plain = TodoReminder(
            FakeTodos(row), scheduler=plain_scheduler, clock=lambda: now
        )
        self.assertEqual(plain.arm(), 1)
        self.assertTrue(
            plain_scheduler.calls[0][1]["local_text"].startswith("待办到时间了：")
        )

    def test_persona_page_exposes_the_context_tab(self):
        source = (ROOT / "ui" / "persona_view.py").read_text(encoding="utf-8")
        self.assertIn('("情境", self._context_tab)', source)
        workbench = (ROOT / "ui" / "persona_workbench.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("def _context_tab", workbench)
        self.assertNotIn("自动学习还没接入", workbench)


if __name__ == "__main__":
    unittest.main()
