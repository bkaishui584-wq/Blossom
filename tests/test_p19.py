"""P19 验收：语音输出接口（TTS）—— 接口稳定、没有语音包也不出声、换语音包不改 Agent Core。"""

import sys
import tkinter as tk
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.core import AgentReply
from persona.state import EmotionState
from security.config import Config
from ui.desktop import DesktopPet
from voice.manager import SpeechPipeline
from voice.tts import NullSpeechProvider, SpeechProvider


class FakeVoice(SpeechProvider):
    """模拟用户以后接入的语音包。"""

    name = "fake-voice"

    def __init__(self, available=True, started=True):
        self.available = available
        self.started = started
        self.said = []
        self.stopped = 0

    def is_available(self):
        return self.available

    def speak(self, text, emotion=None):
        self.said.append((text, emotion))
        return self.started

    def stop(self):
        self.stopped += 1


class BrokenVoice(SpeechProvider):
    name = "broken"

    def is_available(self):
        raise RuntimeError("no driver")

    def speak(self, text, emotion=None):
        raise RuntimeError("boom")

    def stop(self):
        raise RuntimeError("boom")


class StubAgent:
    """只做界面接线测试：不碰数据库，不调用模型。"""

    def __init__(self):
        self.avatar = None
        self.emotion = EmotionState()
        self.persona = None
        self.mode_name = "local"


class InterfaceTests(unittest.TestCase):
    def test_provider_has_the_three_methods(self):
        for name in ("is_available", "speak", "stop"):
            self.assertTrue(hasattr(SpeechProvider, name))

    def test_null_provider_stays_silent_without_errors(self):
        provider = NullSpeechProvider()
        self.assertFalse(provider.is_available())
        self.assertFalse(provider.speak("你好"))
        self.assertFalse(provider.stop())
        self.assertIn("没有接入语音合成", provider.describe()["note"])


class SpeechPipelineTests(unittest.TestCase):
    def test_no_voice_package_means_silence_not_a_crash(self):
        pipeline = SpeechPipeline()
        self.assertFalse(pipeline.available())
        self.assertFalse(pipeline.say("你好"))
        self.assertEqual(pipeline.spoken, 0)
        self.assertEqual(pipeline.skipped, 1)

    def test_available_provider_receives_text_and_emotion(self):
        voice = FakeVoice()
        pipeline = SpeechPipeline(provider=voice)
        self.assertTrue(pipeline.say("你好呀", emotion="happy"))
        self.assertEqual(voice.said, [("你好呀", "happy")])
        self.assertEqual(pipeline.spoken, 1)
        self.assertEqual(pipeline.last_text, "你好呀")

    def test_long_text_is_capped(self):
        voice = FakeVoice()
        pipeline = SpeechPipeline(provider=voice, max_chars=10)
        pipeline.say("一二三四五六七八九十十一十二")
        self.assertEqual(voice.said[0][0], "一二三四五六七八九十")

    def test_whitespace_is_collapsed(self):
        voice = FakeVoice()
        pipeline = SpeechPipeline(provider=voice)
        pipeline.say("  你好   呀  \n 在吗 ")
        self.assertEqual(voice.said[0][0], "你好 呀 在吗")

    def test_empty_text_is_not_spoken(self):
        voice = FakeVoice()
        pipeline = SpeechPipeline(provider=voice)
        self.assertFalse(pipeline.say("   "))
        self.assertFalse(pipeline.say(None))
        self.assertEqual(voice.said, [])
        self.assertEqual(pipeline.skipped, 2)

    def test_provider_that_refuses_is_counted_as_skipped(self):
        voice = FakeVoice(started=False)
        pipeline = SpeechPipeline(provider=voice)
        self.assertFalse(pipeline.say("你好"))
        self.assertEqual(pipeline.spoken, 0)
        self.assertEqual(pipeline.skipped, 1)

    def test_broken_provider_is_contained(self):
        pipeline = SpeechPipeline(provider=BrokenVoice())
        self.assertFalse(pipeline.available())
        self.assertFalse(pipeline.say("你好"))
        pipeline.stop()

    def test_stop_is_forwarded(self):
        voice = FakeVoice()
        pipeline = SpeechPipeline(provider=voice)
        pipeline.stop()
        self.assertEqual(voice.stopped, 1)

    def test_describe_reports_state(self):
        pipeline = SpeechPipeline(provider=FakeVoice())
        pipeline.say("你好")
        info = pipeline.describe()
        self.assertEqual(info["provider"], "fake-voice")
        self.assertTrue(info["available"])
        self.assertEqual(info["spoken"], 1)
        self.assertEqual(info["last_text"], "你好")


class AgentDecouplingTests(unittest.TestCase):
    def test_agent_core_knows_nothing_about_speech(self):
        text = (ROOT / "agent" / "core.py").read_text(encoding="utf-8").lower()
        for word in ("tts", "speech", "speak", "语音合成"):
            self.assertNotIn(word, text)

    def test_tts_module_imports_no_audio_library(self):
        text = (ROOT / "voice" / "tts.py").read_text(encoding="utf-8").lower()
        for needle in ("pyttsx3", "sounddevice", "pyaudio", "edge_tts", "azure", "gtts"):
            self.assertNotIn(needle, text)

    def test_swapping_the_voice_package_needs_no_agent_change(self):
        for provider in (FakeVoice(), NullSpeechProvider(), BrokenVoice()):
            pipeline = SpeechPipeline(provider=provider)
            pipeline.say("你好")
            pipeline.stop()


class DesktopSpeechTests(unittest.TestCase):
    """界面把要说的话交给语音包；说什么、什么情绪都由程序决定。"""

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

    def _pet(self, pipeline):
        pet = DesktopPet(
            StubAgent(), Config(load_env=False), speech=pipeline, root=self.root
        )
        return pet

    def test_reply_is_handed_to_the_voice_package(self):
        voice = FakeVoice()
        pet = self._pet(SpeechPipeline(provider=voice))
        pet.avatar.set_state({"emotion": "happy"})
        pet._apply_reply(AgentReply(ok=True, content="我在呢。"))
        self.assertEqual(voice.said, [("我在呢。", "happy")])

    def test_error_reply_is_not_spoken(self):
        voice = FakeVoice()
        pet = self._pet(SpeechPipeline(provider=voice))
        pet._apply_reply(AgentReply(ok=False, error="模型调用异常"))
        self.assertEqual(voice.said, [])

    def test_proactive_reply_is_spoken(self):
        voice = FakeVoice()
        pet = self._pet(SpeechPipeline(provider=voice))
        pet._apply_proactive(AgentReply(ok=True, content="起来走两步吧。"))
        self.assertEqual([item[0] for item in voice.said], ["起来走两步吧。"])

    def test_no_voice_package_keeps_the_ui_quiet(self):
        pet = self._pet(SpeechPipeline())
        pet._apply_reply(AgentReply(ok=True, content="我在呢。"))
        self.assertFalse(pet.speech.available())

    def test_speaking_failure_does_not_break_the_reply(self):
        pet = self._pet(SpeechPipeline(provider=BrokenVoice()))
        pet._apply_reply(AgentReply(ok=True, content="我在呢。"))
        self.assertIn("我在呢。", pet.transcript.get("1.0", "end"))


if __name__ == "__main__":
    unittest.main()
