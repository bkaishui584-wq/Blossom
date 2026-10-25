"""P5 语音输出落地：用系统自带语音在本进程里发声，默认关闭，不通网、不加依赖、不开子进程。"""

import ctypes
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import voice.speaker as speaker
from database.db import Database
from security.config import Config
from voice.manager import SpeechPipeline
from voice.speaker import SapiVoice, WindowsSpeechProvider, build_provider
from voice.tts import NullSpeechProvider, SpeechProvider


class FakeLogger:
    def __init__(self):
        self.warnings = []

    def warning(self, message, *args):
        self.warnings.append(message % args if args else message)


class FakeEngine:
    """假的系统语音组件，记录每一次调用，绝不真的发声。"""

    def __init__(self, boom=False):
        self.boom = boom
        self.calls = []
        self.rate = None
        self.volume = None
        self.running = False
        self.closed = 0

    def set_rate(self, value):
        self.calls.append(("rate", value))
        self.rate = value

    def set_volume(self, value):
        self.calls.append(("volume", value))
        self.volume = value

    def speak(self, text, async_=True):
        if self.boom:
            raise OSError("发声炸了")
        self.calls.append(("speak", text))
        self.running = True

    def stop(self):
        self.calls.append(("stop",))
        self.running = False

    def speaking(self):
        return self.running

    def close(self):
        self.closed += 1


class FakeOS:
    name = "posix"


def factory_for(engine):
    def factory():
        return engine

    return factory


class TempDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)


class AvailabilityTests(TempDir):
    def test_not_windows_is_unavailable_with_reason(self):
        engine = FakeEngine()
        provider = WindowsSpeechProvider(factory=factory_for(engine))
        with mock.patch.object(speaker, "os", FakeOS):
            self.assertFalse(provider.is_available())
        self.assertEqual(provider.reason, "当前系统不是 Windows")
        self.assertEqual(provider.describe()["engine"], "当前系统不是 Windows")
        self.assertEqual(engine.calls, [])

    def test_missing_component_is_unavailable(self):
        def boom():
            raise OSError("找不到系统语音组件")

        logger = FakeLogger()
        provider = WindowsSpeechProvider(factory=boom, logger=logger)
        self.assertFalse(provider.is_available())
        self.assertIn("系统语音组件不可用", provider.reason)

    def test_factory_returning_nothing_is_unavailable(self):
        provider = WindowsSpeechProvider(factory=lambda: None)
        self.assertFalse(provider.is_available())
        self.assertEqual(provider.reason, "系统语音组件不可用")

    def test_open_passes_rate_and_volume_and_is_cached(self):
        engine = FakeEngine()
        calls = []

        def factory():
            calls.append(1)
            return engine

        provider = WindowsSpeechProvider(rate=3, volume=40, factory=factory)
        self.assertTrue(provider.is_available())
        self.assertTrue(provider.is_available())
        self.assertEqual(len(calls), 1)
        self.assertEqual(engine.volume, 40)
        self.assertEqual(engine.rate, 3)
        self.assertEqual(provider.reason, "")

    def test_settings_failure_does_not_break_availability(self):
        class HalfBroken(FakeEngine):
            def set_rate(self, value):
                raise OSError("设置失败")

        logger = FakeLogger()
        provider = WindowsSpeechProvider(
            factory=factory_for(HalfBroken()), logger=logger
        )
        self.assertTrue(provider.is_available())
        self.assertTrue(logger.warnings)

    def test_variant_must_be_the_real_size(self):
        # 真实的 VARIANT 是 24 字节（含 16 字节 DECIMAL）。少算 8 字节时，
        # 第二个参数会读到越界内存，系统会回「参数类型不对」。
        self.assertEqual(ctypes.sizeof(speaker.VARIANT), 24)
        self.assertEqual(speaker.VARIANT.value.offset, 8)
        self.assertEqual(ctypes.sizeof(speaker.DISPPARAMS), 24)


class SpeakTests(TempDir):
    def provider(self, **kwargs):
        engine = FakeEngine(**kwargs)
        return WindowsSpeechProvider(factory=factory_for(engine)), engine

    def test_speak_stops_the_previous_sentence_first(self):
        provider, engine = self.provider()
        self.assertTrue(provider.speak("你好"))
        self.assertEqual(engine.calls, [("volume", 100), ("rate", 0), ("stop",), ("speak", "你好")])
        self.assertEqual(provider.spoken, 1)
        self.assertEqual(provider.last_text, "你好")

    def test_text_reaches_the_engine_untouched(self):
        provider, engine = self.provider()
        payload = "'; Remove the file; echo '"
        provider.speak(payload)
        self.assertEqual(engine.calls[-1], ("speak", payload))

    def test_speak_squeezes_whitespace(self):
        provider, engine = self.provider()
        provider.speak("  你好\n\n世界  ")
        self.assertEqual(engine.calls[-1], ("speak", "你好 世界"))

    def test_empty_text_is_skipped(self):
        provider, engine = self.provider()
        self.assertFalse(provider.speak("   "))
        self.assertFalse(provider.speak(None))
        self.assertEqual(provider.spoken, 0)
        self.assertNotIn("speak", [call[0] for call in engine.calls])

    def test_speak_unavailable_says_nothing(self):
        engine = FakeEngine()
        provider = WindowsSpeechProvider(factory=factory_for(engine))
        with mock.patch.object(speaker, "os", FakeOS):
            self.assertFalse(provider.speak("你好"))
        self.assertEqual(engine.calls, [])
        self.assertFalse(provider.speaking())

    def test_speak_failure_is_swallowed(self):
        logger = FakeLogger()
        engine = FakeEngine(boom=True)
        provider = WindowsSpeechProvider(factory=factory_for(engine), logger=logger)
        self.assertFalse(provider.speak("你好"))
        self.assertEqual(provider.spoken, 0)
        self.assertTrue(logger.warnings)

    def test_rate_and_volume_are_clamped(self):
        self.assertEqual(WindowsSpeechProvider(rate=99, volume=999).rate, 10)
        self.assertEqual(WindowsSpeechProvider(rate=99, volume=999).volume, 100)
        self.assertEqual(WindowsSpeechProvider(rate=-99, volume=-99).rate, -10)
        self.assertEqual(WindowsSpeechProvider(rate="-99").rate, -10)
        self.assertEqual(WindowsSpeechProvider(rate="不是数字").rate, 0)
        self.assertEqual(WindowsSpeechProvider(volume=None).volume, 100)


class StopTests(TempDir):
    def test_stop_before_opening_is_false(self):
        provider = WindowsSpeechProvider(factory=factory_for(FakeEngine()))
        self.assertFalse(provider.stop())

    def test_stop_tells_the_engine_to_shut_up(self):
        engine = FakeEngine()
        provider = WindowsSpeechProvider(factory=factory_for(engine))
        provider.speak("你好")
        self.assertTrue(provider.stop())
        self.assertEqual(engine.calls[-1], ("stop",))
        self.assertFalse(provider.speaking())

    def test_stop_failure_is_swallowed(self):
        class BrokenStop(FakeEngine):
            def stop(self):
                raise OSError("停不下来")

        logger = FakeLogger()
        provider = WindowsSpeechProvider(
            factory=factory_for(BrokenStop()), logger=logger
        )
        provider._voice = BrokenStop()
        provider._available = True
        self.assertFalse(provider.stop())
        self.assertTrue(logger.warnings)


class DescribeTests(TempDir):
    def test_describe_is_a_readonly_view(self):
        engine = FakeEngine()
        provider = WindowsSpeechProvider(rate=2, volume=50, factory=factory_for(engine))
        view = provider.describe()
        for key in ("provider", "available", "speaking", "engine", "rate", "volume", "spoken", "note"):
            self.assertIn(key, view)
        self.assertEqual(view["provider"], "windows-speech")
        self.assertEqual(view["rate"], 2)
        self.assertEqual(view["volume"], 50)

    def test_missing_provider_describes_itself(self):
        view = NullSpeechProvider().describe()
        self.assertEqual(view["provider"], "null")
        self.assertFalse(view["available"])

    def test_provider_is_an_interface_implementation(self):
        self.assertIsInstance(WindowsSpeechProvider(), SpeechProvider)
        self.assertIsInstance(NullSpeechProvider(), SpeechProvider)
        self.assertIsInstance(SapiVoice, type)


class BuilderTests(TempDir):
    def test_disabled_switch_keeps_the_silent_default(self):
        config = Config(load_env=False)
        config.voice_output = False
        self.assertIsInstance(build_provider(config), NullSpeechProvider)

    def test_enabled_switch_uses_the_windows_provider(self):
        config = Config(load_env=False)
        config.voice_output = True
        config.voice_rate = 3
        config.voice_volume = 40
        provider = build_provider(config)
        self.assertIsInstance(provider, WindowsSpeechProvider)
        self.assertEqual(provider.rate, 3)
        self.assertEqual(provider.volume, 40)

    def test_builder_survives_a_config_without_the_field(self):
        class Bare:
            pass

        self.assertIsInstance(build_provider(Bare()), NullSpeechProvider)


class ConfigTests(TempDir):
    def test_defaults_are_off_and_sane(self):
        config = Config(load_env=False)
        self.assertFalse(config.voice_output)
        self.assertEqual(config.voice_rate, 0)
        self.assertEqual(config.voice_volume, 100)

    def test_env_can_turn_it_on(self):
        env = {
            "DESKPET_VOICE_OUTPUT": "1",
            "DESKPET_VOICE_RATE": "5",
            "DESKPET_VOICE_VOLUME": "80",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            config = Config(load_env=False)
        self.assertTrue(config.voice_output)
        self.assertEqual(config.voice_rate, 5)
        self.assertEqual(config.voice_volume, 80)

    def test_out_of_range_values_are_bounded(self):
        env = {"DESKPET_VOICE_RATE": "999", "DESKPET_VOICE_VOLUME": "9999"}
        with mock.patch.dict(os.environ, env, clear=False):
            config = Config(load_env=False)
        self.assertEqual(config.voice_rate, 10)
        self.assertEqual(config.voice_volume, 100)

    def test_redacted_view_hides_nothing_secret(self):
        view = Config(load_env=False).redacted()
        self.assertIn("voice_output", view)
        self.assertIn("voice_rate", view)
        self.assertIn("voice_volume", view)
        for value in view.values():
            self.assertNotIn("sk-", str(value))


class PipelineTests(TempDir):
    def test_silent_pipeline_says_nothing(self):
        pipeline = SpeechPipeline(provider=NullSpeechProvider())
        self.assertFalse(pipeline.available())
        self.assertFalse(pipeline.say("不该出声"))
        self.assertEqual(pipeline.spoken, 0)
        self.assertEqual(pipeline.describe()["provider"], "null")

    def test_enabled_pipeline_counts_what_it_says(self):
        engine = FakeEngine()
        pipeline = SpeechPipeline(
            provider=WindowsSpeechProvider(factory=factory_for(engine))
        )
        self.assertTrue(pipeline.say("你好", emotion="happy"))
        self.assertEqual(pipeline.spoken, 1)
        self.assertEqual(pipeline.last_text, "你好")
        self.assertIn(("speak", "你好"), engine.calls)
        self.assertTrue(pipeline.describe()["available"])

    def test_pipeline_truncates_long_text(self):
        engine = FakeEngine()
        pipeline = SpeechPipeline(
            provider=WindowsSpeechProvider(factory=factory_for(engine)), max_chars=6
        )
        pipeline.say("一二三四五六七八九十")
        self.assertEqual(pipeline.last_text, "一二三四五六")

    def test_broken_provider_does_not_crash_the_pipeline(self):
        class Broken(SpeechProvider):
            name = "broken"

            def is_available(self):
                raise RuntimeError("探测炸了")

            def speak(self, text, emotion=None):
                raise RuntimeError("发声炸了")

            def stop(self):
                raise RuntimeError("停止炸了")

        pipeline = SpeechPipeline(provider=Broken(), logger=FakeLogger())
        self.assertFalse(pipeline.say("你好"))
        pipeline.stop()
        self.assertEqual(pipeline.spoken, 0)


class BoundaryTests(TempDir):
    def test_agent_core_does_not_know_about_speech(self):
        text = (ROOT / "agent" / "core.py").read_text(encoding="utf-8").lower()
        for word in ("speak", "speech", "voice", "tts", "sapi", "speechsynthesizer"):
            self.assertNotIn(word, text)

    def test_speaker_has_no_shell_primitive(self):
        text = (ROOT / "voice" / "speaker.py").read_text(encoding="utf-8")
        for needle in ("os.system", "subprocess", "eval(", "exec(", "pickle.load"):
            self.assertNotIn(needle, text)

    def test_speaker_needs_no_third_party_library(self):
        text = (ROOT / "voice" / "speaker.py").read_text(encoding="utf-8")
        for needle in ("import requests", "import numpy", "import pyttsx3", "import pyaudio", "import win32", "import comtypes"):
            self.assertNotIn(needle, text)

    def test_speaker_only_imports_the_standard_library(self):
        text = (ROOT / "voice" / "speaker.py").read_text(encoding="utf-8")
        imported = [
            line.strip()
            for line in text.splitlines()
            if line.startswith("import ") or line.startswith("from ")
        ]
        self.assertEqual(
            set(imported),
            {
                "import ctypes",
                "import os",
                "from voice.tts import NullSpeechProvider, SpeechProvider",
            },
        )

    def test_memory_data_survives_a_speech_provider(self):
        config = Config(load_env=False)
        config.db_path = os.path.join(self.tmp.name, "deskpet.sqlite3")
        db = Database(config.db_path)
        db.migrate()
        config.voice_output = True
        provider = build_provider(config)
        self.assertIsInstance(provider, WindowsSpeechProvider)
        self.assertTrue(os.path.isfile(config.db_path))
        with db.connect() as conn:
            self.assertTrue(conn.execute("select 1").fetchone())

    def test_disabled_switch_never_touches_the_system_component(self):
        calls = []

        def factory():
            calls.append(1)
            return FakeEngine()

        config = Config(load_env=False)
        config.voice_output = False
        provider = build_provider(config)
        self.assertIsInstance(provider, NullSpeechProvider)
        self.assertEqual(calls, [])


class RealComponentTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "只在 Windows 上验证系统语音组件")
    def test_system_component_opens_without_speaking(self):
        provider = WindowsSpeechProvider(rate=0, volume=50)
        self.assertTrue(provider.is_available(), provider.reason)
        self.assertFalse(provider.speaking())
        self.assertEqual(provider.describe()["engine"], "系统自带语音")

    @unittest.skipUnless(os.name == "nt", "只在 Windows 上验证系统语音组件")
    def test_system_component_reports_a_boolean(self):
        voice = speaker.open_sapi_voice()
        try:
            self.assertIsInstance(voice.speaking(), bool)
        finally:
            voice.close()


if __name__ == "__main__":
    unittest.main()
