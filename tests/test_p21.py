"""P21 验收：唤醒词与麦克风隐私 —— 接口稳定、状态清楚、没有唤醒模型也不崩。"""

import sys
import tkinter as tk
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.core import AgentReply
from security.config import Config
from ui.desktop import DesktopPet
from voice.wake import (
    MIC_LABELS,
    MIC_LISTENING,
    MIC_OFF,
    MIC_STATES,
    PROCESSING,
    RECORDING,
    WAKE_DETECTION,
    WAKE_WORDS,
    MicPrivacy,
    NullWakeWordProvider,
    WakeWordDetector,
    WakeWordProvider,
    match_wake_word,
)


class FakeWake(WakeWordProvider):
    """模拟用户以后接入的唤醒词模型。"""

    name = "fake-wake"

    def __init__(self, available=True, heard=True):
        self.available = available
        self.heard = heard
        self.seen = []
        self.stopped = 0

    def is_available(self):
        return self.available

    def detect(self, audio, sample_rate=None):
        self.seen.append((audio, sample_rate))
        return self.heard

    def stop(self):
        self.stopped += 1


class BrokenWake(WakeWordProvider):
    name = "broken"

    def is_available(self):
        raise RuntimeError("no driver")

    def detect(self, audio, sample_rate=None):
        raise RuntimeError("boom")

    def stop(self):
        raise RuntimeError("boom")


class StubAgent:
    """只做界面接线测试：不碰数据库，不调用模型。"""

    def __init__(self):
        self.avatar = None
        self.persona = None
        self.mode_name = "local"

    def respond(self, session_id, text, **kwargs):
        return AgentReply(ok=True, content="嗯。")


class InterfaceTests(unittest.TestCase):
    def test_provider_has_the_three_methods(self):
        for name in ("is_available", "detect", "stop"):
            self.assertTrue(hasattr(WakeWordProvider, name))

    def test_null_provider_stays_quiet_without_errors(self):
        provider = NullWakeWordProvider()
        self.assertFalse(provider.is_available())
        self.assertFalse(provider.detect(b"\x00\x01"))
        self.assertFalse(provider.stop())
        self.assertIn("没有接入唤醒词检测", provider.describe()["note"])


class MatchTests(unittest.TestCase):
    def test_wake_word_at_the_start_is_matched(self):
        self.assertEqual(match_wake_word("芳菲，今天天气怎么样"), ("芳菲", "今天天气怎么样"))

    def test_wake_word_alone_leaves_nothing(self):
        self.assertEqual(match_wake_word("芳菲"), ("芳菲", ""))

    def test_wake_word_with_trailing_punctuation_only(self):
        self.assertEqual(match_wake_word("芳菲！！！"), ("芳菲", ""))

    def test_english_wake_word_ignores_case(self):
        self.assertEqual(match_wake_word("DeskPet 你好"), ("deskpet", "你好"))

    def test_no_wake_word_returns_nothing_and_the_original(self):
        self.assertEqual(match_wake_word("今天几号"), ("", "今天几号"))

    def test_wake_word_in_the_middle_does_not_count(self):
        word, rest = match_wake_word("那个芳菲在吗")
        self.assertEqual(word, "")
        self.assertEqual(rest, "那个芳菲在吗")

    def test_empty_text(self):
        self.assertEqual(match_wake_word(""), ("", ""))
        self.assertEqual(match_wake_word(None), ("", ""))

    def test_custom_words(self):
        self.assertEqual(match_wake_word("小美，在吗", words=("小美",)), ("小美", "在吗"))

    def test_default_words_are_not_empty(self):
        self.assertTrue(WAKE_WORDS)


class DetectorTests(unittest.TestCase):
    def test_text_matching_works_without_any_model(self):
        detector = WakeWordDetector(provider=NullWakeWordProvider())
        self.assertTrue(detector.available())
        model_only = WakeWordDetector(provider=NullWakeWordProvider(), words=())
        self.assertFalse(model_only.available())

    def test_feed_wakes_and_hands_the_rest_over(self):
        heard = []
        detector = WakeWordDetector(on_wake=heard.append)
        detector.arm()
        self.assertEqual(detector.feed("芳菲，几点了"), ("芳菲", "几点了"))
        self.assertEqual(heard, ["几点了"])
        self.assertEqual(detector.woken, 1)
        self.assertEqual(detector.last_word, "芳菲")
        self.assertFalse(detector.armed)

    def test_feed_without_wake_word_is_ignored(self):
        heard = []
        detector = WakeWordDetector(on_wake=heard.append)
        self.assertEqual(detector.feed("随便说点什么"), ("", ""))
        self.assertEqual(heard, [])
        self.assertEqual(detector.ignored, 1)

    def test_arm_and_disarm(self):
        detector = WakeWordDetector()
        self.assertFalse(detector.armed)
        detector.arm()
        self.assertTrue(detector.armed)
        detector.disarm()
        self.assertFalse(detector.armed)

    def test_detect_forwards_audio_to_the_model(self):
        wake = FakeWake()
        detector = WakeWordDetector(provider=wake)
        self.assertTrue(detector.detect(b"audio", sample_rate=16000))
        self.assertEqual(wake.seen, [(b"audio", 16000)])

    def test_detect_without_model_is_false(self):
        self.assertFalse(WakeWordDetector(provider=NullWakeWordProvider()).detect(b"audio"))

    def test_broken_model_is_contained(self):
        detector = WakeWordDetector(provider=BrokenWake())
        self.assertFalse(detector.detect(b"audio"))
        detector.arm()
        detector.stop()
        self.assertFalse(detector.armed)

    def test_stop_is_forwarded_and_disarms(self):
        wake = FakeWake()
        detector = WakeWordDetector(provider=wake)
        detector.arm()
        detector.stop()
        self.assertEqual(wake.stopped, 1)
        self.assertFalse(detector.armed)

    def test_broken_callback_is_contained(self):
        def explode(rest):
            raise RuntimeError("boom")

        detector = WakeWordDetector(on_wake=explode)
        self.assertEqual(detector.feed("芳菲，你好"), ("芳菲", "你好"))

    def test_describe_reports_state(self):
        detector = WakeWordDetector(provider=FakeWake(), words=("芳菲",))
        detector.arm()
        detector.feed("芳菲，你好")
        info = detector.describe()
        self.assertEqual(info["provider"], "fake-wake")
        self.assertEqual(info["words"], ["芳菲"])
        self.assertEqual(info["woken"], 1)
        self.assertFalse(info["armed"])


class MicPrivacyTests(unittest.TestCase):
    def test_starts_off(self):
        mic = MicPrivacy()
        self.assertEqual(mic.state, MIC_OFF)
        self.assertFalse(mic.listening())

    def test_all_five_states_have_human_labels(self):
        self.assertEqual(len(MIC_STATES), 5)
        for state in MIC_STATES:
            self.assertTrue(MIC_LABELS.get(state))

    def test_unknown_state_is_rejected(self):
        mic = MicPrivacy()
        self.assertFalse(mic.set("nonsense"))
        self.assertEqual(mic.state, MIC_OFF)

    def test_every_open_state_counts_as_listening(self):
        mic = MicPrivacy()
        for state in (MIC_LISTENING, WAKE_DETECTION, RECORDING, PROCESSING):
            mic.set(state)
            self.assertTrue(mic.listening())
        mic.off()
        self.assertFalse(mic.listening())

    def test_push_to_talk_only_records_while_held(self):
        mic = MicPrivacy()
        mic.push(True)
        self.assertEqual(mic.state, RECORDING)
        mic.push(False)
        self.assertEqual(mic.state, MIC_OFF)
        self.assertFalse(mic.listening())

    def test_changes_are_counted(self):
        mic = MicPrivacy()
        mic.start_listening()
        mic.start_listening()
        mic.off()
        self.assertEqual(mic.changes, 2)

    def test_describe_reports_state(self):
        mic = MicPrivacy()
        mic.await_wake()
        info = mic.describe()
        self.assertEqual(info["state"], WAKE_DETECTION)
        self.assertEqual(info["label"], MIC_LABELS[WAKE_DETECTION])
        self.assertTrue(info["listening"])


class AgentDecouplingTests(unittest.TestCase):
    def test_agent_core_knows_nothing_about_waking(self):
        text = (ROOT / "agent" / "core.py").read_text(encoding="utf-8").lower()
        for word in ("wake", "唤醒", "mic", "麦克风"):
            self.assertNotIn(word, text)

    def test_wake_module_imports_no_audio_library(self):
        text = (ROOT / "voice" / "wake.py").read_text(encoding="utf-8").lower()
        for needle in (
            "pyaudio",
            "sounddevice",
            "porcupine",
            "openwakeword",
            "snowboy",
            "azure",
            "openai",
        ):
            self.assertNotIn(needle, text)

    def test_swapping_the_wake_engine_needs_no_agent_change(self):
        for provider in (FakeWake(), NullWakeWordProvider(), BrokenWake()):
            detector = WakeWordDetector(provider=provider)
            detector.arm()
            detector.detect(b"audio")
            detector.feed("芳菲，你好")
            detector.stop()


class DesktopWakeTests(unittest.TestCase):
    """界面负责串联：等唤醒词、把内容交给聊天通道、把麦克风状态摆给用户看。"""

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

    def _pet(self, wake=None, mic=None):
        config = Config(load_env=False)
        config.dev_mode = True
        pet = DesktopPet(StubAgent(), config, root=self.root, wake=wake, mic=mic)
        if wake is not None:
            wake.on_wake = pet.submit_voice
        return pet

    def test_wake_word_then_content_goes_to_the_chat_channel(self):
        wake = WakeWordDetector()
        wake.arm()
        pet = self._pet(wake=wake)
        pet.receive_speech("芳菲，今天天气怎么样")
        self.assertIn("今天天气怎么样", pet.transcript.get("1.0", "end"))

    def test_speech_without_wake_word_is_dropped_while_armed(self):
        wake = WakeWordDetector()
        wake.arm()
        pet = self._pet(wake=wake)
        before = pet.transcript.get("1.0", "end")
        pet.receive_speech("随便说点什么")
        self.assertEqual(pet.transcript.get("1.0", "end"), before)
        self.assertEqual(wake.ignored, 1)

    def test_wake_word_alone_only_wakes_up(self):
        wake = WakeWordDetector()
        wake.arm()
        pet = self._pet(wake=wake)
        before = pet.transcript.get("1.0", "end")
        pet.receive_speech("芳菲")
        self.assertFalse(wake.armed)
        self.assertEqual(pet.transcript.get("1.0", "end"), before)

    def test_speech_is_unchanged_when_not_waiting_for_wake_word(self):
        pet = self._pet(wake=WakeWordDetector())
        pet.receive_speech("你好呀")
        self.assertIn("你好呀", pet.transcript.get("1.0", "end"))

    def test_mic_state_is_visible_in_the_status_line(self):
        mic = MicPrivacy()
        pet = self._pet(mic=mic)
        pet._refresh_status()
        self.assertIn("麦克风：关闭", pet.status.cget("text"))
        mic.start_listening()
        pet._refresh_status()
        self.assertIn("麦克风：正在监听", pet.status.cget("text"))

    def test_no_mic_keeps_the_status_line_unchanged(self):
        pet = self._pet()
        pet._refresh_status()
        self.assertNotIn("麦克风", pet.status.cget("text"))

    def test_mic_is_turned_off_on_close(self):
        mic = MicPrivacy(MIC_LISTENING)
        pet = self._pet(mic=mic)
        pet.close()
        self.assertFalse(mic.listening())

    def test_wake_and_mic_show_up_in_the_developer_panel(self):
        pet = self._pet(wake=WakeWordDetector(), mic=MicPrivacy())
        pet._set_dev({})
        panel = pet.dev.get("1.0", "end")
        self.assertIn("唤醒词", panel)
        self.assertIn("麦克风", panel)

    def test_broken_wake_model_does_not_break_the_pet(self):
        wake = WakeWordDetector(provider=BrokenWake())
        wake.arm()
        pet = self._pet(wake=wake)
        pet.receive_speech("芳菲，你好")
        self.assertIn("你好", pet.transcript.get("1.0", "end"))


if __name__ == "__main__":
    unittest.main()
