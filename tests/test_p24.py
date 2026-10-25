"""P24 验收：静默模式 —— 不发声、不主动、不提醒，但角色动作照常。"""

import sys
import tkinter as tk
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from behavior.manager import INTERACT, SILENT, BehaviorManager
from behavior.quiet import QuietMode
from persona.state import EmotionState
from security.config import Config
from ui.desktop import DesktopPet
from voice.manager import SpeechPipeline
from voice.tts import SpeechProvider


class FakePreferences:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get_preference(self, key):
        return self.values.get(key)

    def set_preference(self, key, value):
        self.values[key] = value


class FakeMemory(FakePreferences):
    def usage_totals(self):
        return None


class BrokenPreferences:
    def get_preference(self, key):
        raise RuntimeError("db gone")

    def set_preference(self, key, value):
        raise RuntimeError("db gone")


class BrokenQuiet:
    def allows_proactive(self):
        raise RuntimeError("boom")


class FakeSpeech(SpeechProvider):
    name = "fake-speech"

    def __init__(self):
        self.said = []
        self.stopped = 0

    def is_available(self):
        return True

    def speak(self, text, emotion=None):
        self.said.append(text)
        return True

    def stop(self):
        self.stopped += 1


class StubAgent:
    def __init__(self):
        self.avatar = None
        self.emotion = EmotionState()
        self.persona = None
        self.mode_name = "local"


class QuietModeTests(unittest.TestCase):
    def test_off_by_default(self):
        quiet = QuietMode()
        self.assertFalse(quiet.enabled())

    def test_manual_switch_and_toggle(self):
        quiet = QuietMode()
        self.assertTrue(quiet.set(True))
        self.assertTrue(quiet.enabled())
        self.assertFalse(quiet.toggle())
        self.assertFalse(quiet.enabled())

    def test_switch_is_remembered(self):
        prefs = FakePreferences()
        QuietMode(preferences=prefs).set(True)
        self.assertEqual(prefs.values["quiet_mode"], "1")
        self.assertTrue(QuietMode(preferences=prefs).enabled())

    def test_stored_value_is_read_on_start(self):
        self.assertTrue(QuietMode(preferences=FakePreferences({"quiet_mode": "1"})).enabled())
        self.assertFalse(QuietMode(preferences=FakePreferences({"quiet_mode": "0"})).enabled())

    def test_manual_switch_wins_over_the_stored_value(self):
        quiet = QuietMode(preferences=FakePreferences({"quiet_mode": "1"}))
        quiet.set(False)
        self.assertFalse(quiet.enabled())

    def test_gates_while_quiet(self):
        quiet = QuietMode(preferences=FakePreferences({"quiet_mode": "1"}))
        self.assertFalse(quiet.allows_speech())
        self.assertFalse(quiet.allows_proactive())
        self.assertTrue(quiet.allows_idle())

    def test_gates_while_awake(self):
        quiet = QuietMode()
        self.assertTrue(quiet.allows_speech())
        self.assertTrue(quiet.allows_proactive())
        self.assertTrue(quiet.allows_idle())

    def test_broken_storage_is_contained(self):
        quiet = QuietMode(preferences=BrokenPreferences())
        self.assertFalse(quiet.enabled())
        self.assertTrue(quiet.set(True))
        self.assertTrue(quiet.enabled())

    def test_describe_reports_the_source(self):
        self.assertEqual(QuietMode().describe()["source"], "preference")
        quiet = QuietMode(preferences=FakePreferences())
        quiet.set(True)
        info = quiet.describe()
        self.assertEqual(info["source"], "manual")
        self.assertEqual(info["key"], "quiet_mode")
        self.assertTrue(info["enabled"])


class BehaviorGateTests(unittest.TestCase):
    def _manager(self, quiet=None):
        return BehaviorManager(
            emotion=EmotionState(),
            hour_of=lambda ts: 12,
            quiet=quiet,
        )

    def test_quiet_stops_the_pet_from_speaking_first(self):
        quiet = QuietMode(preferences=FakePreferences({"quiet_mode": "1"}))
        self.assertEqual(self._manager(quiet).should_interact(), SILENT)

    def test_quiet_stops_even_urgent_reminders(self):
        quiet = QuietMode(preferences=FakePreferences({"quiet_mode": "1"}))
        manager = self._manager(quiet)
        self.assertEqual(manager.should_interact(urgent=True), SILENT)
        self.assertEqual(manager.should_interact(priority="high"), SILENT)

    def test_quiet_does_not_stop_idle_motions(self):
        quiet = QuietMode(preferences=FakePreferences({"quiet_mode": "1"}))
        self.assertIsNotNone(self._manager(quiet).idle())

    def test_without_quiet_nothing_changes(self):
        self.assertEqual(self._manager().should_interact(), INTERACT)

    def test_broken_quiet_does_not_mute_the_pet(self):
        self.assertEqual(self._manager(BrokenQuiet()).should_interact(), INTERACT)

    def test_describe_reports_quiet(self):
        quiet = QuietMode(preferences=FakePreferences({"quiet_mode": "1"}))
        self.assertTrue(self._manager(quiet).describe()["quiet"])
        self.assertFalse(self._manager().describe()["quiet"])


class DesktopQuietTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except Exception as exc:
            self.skipTest(f"tkinter 不可用：{exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy)

    def _destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _pet(self, quiet=None, speech=None, memory=None):
        config = Config(load_env=False)
        config.dev_mode = True
        return DesktopPet(
            StubAgent(),
            config,
            root=self.root,
            memory=memory,
            speech=speech,
            quiet=quiet,
        )

    def test_speech_is_skipped_while_quiet(self):
        voice = FakeSpeech()
        quiet = QuietMode(preferences=FakePreferences({"quiet_mode": "1"}))
        pet = self._pet(quiet=quiet, speech=SpeechPipeline(provider=voice))
        self.assertFalse(pet._say("你好"))
        self.assertEqual(voice.said, [])
        quiet.set(False)
        self.assertTrue(pet._say("你好"))
        self.assertEqual(voice.said, ["你好"])

    def test_status_line_says_quiet(self):
        quiet = QuietMode(preferences=FakePreferences({"quiet_mode": "1"}))
        pet = self._pet(quiet=quiet)
        pet._refresh_status()
        self.assertIn("静默中", pet.status.cget("text"))
        quiet.set(False)
        pet._refresh_status()
        self.assertNotIn("静默中", pet.status.cget("text"))

    def test_no_quiet_object_keeps_the_status_line_unchanged(self):
        pet = self._pet()
        pet._refresh_status()
        self.assertNotIn("静默中", pet.status.cget("text"))

    def test_quiet_shows_up_in_the_developer_panel(self):
        pet = self._pet(quiet=QuietMode())
        pet._set_dev({})
        self.assertIn("静默模式", pet.dev.get("1.0", "end"))

    def test_the_settings_switch_is_saved_and_applied(self):
        memory = FakeMemory()
        quiet = QuietMode(preferences=memory)
        voice = FakeSpeech()
        pet = self._pet(quiet=quiet, speech=SpeechPipeline(provider=voice), memory=memory)
        pet.quiet_var = tk.BooleanVar(value=True)
        pet.settings_hint = tk.Label(self.root, text="")
        self.assertTrue(pet._save_quiet())
        self.assertEqual(memory.values["quiet_mode"], "1")
        self.assertTrue(pet._quiet_enabled())
        self.assertEqual(voice.stopped, 1)

    def test_turning_quiet_off_lets_the_pet_speak_again(self):
        memory = FakeMemory({"quiet_mode": "1"})
        quiet = QuietMode(preferences=memory)
        voice = FakeSpeech()
        pet = self._pet(quiet=quiet, speech=SpeechPipeline(provider=voice), memory=memory)
        pet.quiet_var = tk.BooleanVar(value=False)
        pet.settings_hint = tk.Label(self.root, text="")
        pet._save_quiet()
        self.assertEqual(memory.values["quiet_mode"], "0")
        self.assertTrue(pet._say("我在"))
        self.assertEqual(voice.said, ["我在"])


class AgentDecouplingTests(unittest.TestCase):
    def test_agent_core_knows_nothing_about_quiet_mode(self):
        text = (ROOT / "agent" / "core.py").read_text(encoding="utf-8").lower()
        for word in ("quiet", "静默", "allows_speech", "allows_proactive"):
            self.assertNotIn(word, text)

    def test_quiet_module_needs_no_extra_dependency(self):
        text = (ROOT / "behavior" / "quiet.py").read_text(encoding="utf-8").lower()
        for needle in ("requests", "pyttsx3", "sounddevice", "win32", "pyaudio"):
            self.assertNotIn(needle, text)

    def test_swapping_the_quiet_store_needs_no_agent_change(self):
        for prefs in (FakePreferences({"quiet_mode": "1"}), BrokenPreferences(), None):
            quiet = QuietMode(preferences=prefs)
            quiet.set(True)
            quiet.describe()


if __name__ == "__main__":
    unittest.main()
