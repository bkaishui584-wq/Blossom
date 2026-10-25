"""P20 验收：语音识别接口（STT）—— 接口稳定、没有识别引擎也不出声、换引擎不改 Agent Core。"""

import sys
import tkinter as tk
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.core import AgentReply
from security.config import Config
from ui.desktop import DesktopPet
from voice.manager import TranscribePipeline
from voice.stt import NullSttProvider, SpeechToTextProvider


class FakeStt(SpeechToTextProvider):
    """模拟用户以后接入的识别引擎，本地或云端都一样。"""

    name = "fake-stt"

    def __init__(self, available=True, text="你好", error=False):
        self.available = available
        self.text = text
        self.error = error
        self.seen = []
        self.stopped = 0

    def is_available(self):
        return self.available

    def transcribe(self, audio, sample_rate=None, language=None):
        if self.error:
            raise RuntimeError("engine gone")
        self.seen.append((audio, sample_rate, language))
        return self.text

    def stop(self):
        self.stopped += 1


class BrokenStt(SpeechToTextProvider):
    name = "broken"

    def is_available(self):
        raise RuntimeError("no driver")

    def transcribe(self, audio, sample_rate=None, language=None):
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
        for name in ("is_available", "transcribe", "stop"):
            self.assertTrue(hasattr(SpeechToTextProvider, name))

    def test_null_provider_stays_quiet_without_errors(self):
        provider = NullSttProvider()
        self.assertFalse(provider.is_available())
        self.assertEqual(provider.transcribe(b"\x00\x01"), "")
        self.assertFalse(provider.stop())
        self.assertIn("没有接入语音识别", provider.describe()["note"])


class TranscribePipelineTests(unittest.TestCase):
    def test_no_engine_means_no_text_not_a_crash(self):
        pipeline = TranscribePipeline()
        self.assertFalse(pipeline.available())
        self.assertEqual(pipeline.transcribe(b"audio"), "")
        self.assertEqual(pipeline.transcribed, 0)
        self.assertEqual(pipeline.skipped, 1)

    def test_audio_becomes_text(self):
        stt = FakeStt(text="今天天气怎么样")
        pipeline = TranscribePipeline(provider=stt)
        self.assertTrue(pipeline.available())
        self.assertEqual(pipeline.transcribe(b"audio"), "今天天气怎么样")
        self.assertEqual(stt.seen, [(b"audio", None, None)])
        self.assertEqual(pipeline.transcribed, 1)
        self.assertEqual(pipeline.last_text, "今天天气怎么样")

    def test_language_and_rate_are_forwarded(self):
        stt = FakeStt()
        TranscribePipeline(provider=stt).transcribe(
            b"audio", sample_rate=16000, language="zh"
        )
        self.assertEqual(stt.seen, [(b"audio", 16000, "zh")])

    def test_long_text_is_capped(self):
        pipeline = TranscribePipeline(
            provider=FakeStt(text="一二三四五六七八九十十一十二"), max_chars=10
        )
        self.assertEqual(pipeline.transcribe(b"audio"), "一二三四五六七八九十")

    def test_whitespace_is_collapsed(self):
        pipeline = TranscribePipeline(provider=FakeStt(text="  你好   啊  \n 在吗 "))
        self.assertEqual(pipeline.transcribe(b"audio"), "你好 啊 在吗")

    def test_nothing_heard_is_skipped(self):
        pipeline = TranscribePipeline(provider=FakeStt(text="   "))
        self.assertEqual(pipeline.transcribe(b"audio"), "")
        self.assertEqual(pipeline.skipped, 1)
        self.assertEqual(pipeline.transcribed, 0)

    def test_unavailable_engine_is_not_called(self):
        stt = FakeStt(available=False)
        pipeline = TranscribePipeline(provider=stt)
        self.assertEqual(pipeline.transcribe(b"audio"), "")
        self.assertEqual(stt.seen, [])

    def test_broken_engine_is_contained(self):
        pipeline = TranscribePipeline(provider=BrokenStt())
        self.assertFalse(pipeline.available())
        self.assertEqual(pipeline.transcribe(b"audio"), "")
        pipeline.stop()

    def test_stop_is_forwarded(self):
        stt = FakeStt()
        TranscribePipeline(provider=stt).stop()
        self.assertEqual(stt.stopped, 1)

    def test_feed_hands_text_downstream(self):
        heard = []
        pipeline = TranscribePipeline(
            provider=FakeStt(text="帮我看看天气"), on_text=heard.append
        )
        self.assertEqual(pipeline.feed(b"audio"), "帮我看看天气")
        self.assertEqual(heard, ["帮我看看天气"])

    def test_feed_without_engine_does_not_disturb_downstream(self):
        heard = []
        pipeline = TranscribePipeline(on_text=heard.append)
        self.assertEqual(pipeline.feed(b"audio"), "")
        self.assertEqual(heard, [])

    def test_downstream_failure_is_contained(self):
        def explode(text):
            raise RuntimeError("boom")

        pipeline = TranscribePipeline(provider=FakeStt(text="你好"), on_text=explode)
        self.assertEqual(pipeline.feed(b"audio"), "你好")

    def test_describe_reports_state(self):
        pipeline = TranscribePipeline(provider=FakeStt(text="你好"))
        pipeline.transcribe(b"audio")
        info = pipeline.describe()
        self.assertEqual(info["provider"], "fake-stt")
        self.assertTrue(info["available"])
        self.assertEqual(info["transcribed"], 1)
        self.assertEqual(info["last_text"], "你好")


class AgentDecouplingTests(unittest.TestCase):
    def test_agent_core_knows_nothing_about_recognition(self):
        text = (ROOT / "agent" / "core.py").read_text(encoding="utf-8").lower()
        for word in ("stt", "transcribe", "语音识别"):
            self.assertNotIn(word, text)

    def test_stt_module_imports_no_audio_library(self):
        """stt.py 不能依赖任何音频/识别库：引擎只在真正调用时按需 import。"""
        text = (ROOT / "voice" / "stt.py").read_text(encoding="utf-8")
        banned = (
            "pyaudio",
            "sounddevice",
            "whisper",
            "vosk",
            "azure",
            "speech_recognition",
            "openai",
        )
        module_imports = [
            line.strip()
            for line in text.splitlines()
            if line.startswith("import ") or line.startswith("from ")
        ]
        for line in module_imports:
            for needle in banned:
                self.assertNotIn(needle, line.lower(), line)
        # 引擎名只出现在探测表与按需加载里，模块级 import 仍是空的。
        self.assertEqual(
            [line for line in module_imports if line.lower().startswith("from abc")],
            ["from abc import ABC, abstractmethod"],
        )
        self.assertIn("import vosk", text)

    def test_swapping_the_recognition_engine_needs_no_agent_change(self):
        for provider in (FakeStt(), NullSttProvider(), BrokenStt()):
            pipeline = TranscribePipeline(provider=provider)
            pipeline.transcribe(b"audio")
            pipeline.stop()


class DesktopRecognitionTests(unittest.TestCase):
    """界面把识别出的文字交给聊天通道；看不出来、坏了都不影响桌宠。"""

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

    def _pet(self, pipeline, dev=False):
        config = Config(load_env=False)
        config.dev_mode = dev
        pet = DesktopPet(StubAgent(), config, stt=pipeline, root=self.root)
        if pipeline is not None:
            pipeline.on_text = pet.submit_voice
        return pet

    def test_recognition_shows_up_in_developer_panel(self):
        pet = self._pet(TranscribePipeline(provider=FakeStt()), dev=True)
        pet._set_dev({})
        panel = pet.dev.get("1.0", "end")
        self.assertIn("语音识别", panel)
        self.assertIn("fake-stt", panel)

    def test_recognised_text_goes_through_the_chat_channel(self):
        pet = self._pet(TranscribePipeline(provider=FakeStt(text="今天天气怎么样")))
        pet.stt.feed(b"audio")
        self.assertIn("今天天气怎么样", pet.transcript.get("1.0", "end"))

    def test_no_engine_keeps_the_ui_quiet(self):
        pet = self._pet(TranscribePipeline())
        self.assertFalse(pet.stt.available())
        self.assertEqual(pet.stt.feed(b"audio"), "")

    def test_broken_engine_does_not_break_the_pet(self):
        pet = self._pet(TranscribePipeline(provider=BrokenStt()), dev=True)
        self.assertEqual(pet.stt.feed(b"audio"), "")
        pet._set_dev({})

    def test_recognition_is_stopped_on_close(self):
        stt = FakeStt()
        pet = self._pet(TranscribePipeline(provider=stt))
        pet.close()
        self.assertEqual(stt.stopped, 1)


if __name__ == "__main__":
    unittest.main()
