"""P60 阶段 F：真实音频采集与识别引擎的诚实边界。

要点：
    没麦克风、没识别引擎、没授权时一律说清楚原因，不假装能用；
    录音只在用户按下按钮时采一段，采到的字节数与文件大小来自真实磁盘；
    识别出的文字走和打字同一条聊天通道，Agent Core 不知道语音的存在；
    语音输入、语音输出、唤醒词三者的可用性互不冒充。
"""

import io
import os
import sys
import tempfile
import unittest
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from persona import history as persona_history  # noqa: E402
from security.config import Config  # noqa: E402
from security.privacy import PrivacyCenter  # noqa: E402
from voice.capture import (  # noqa: E402
    DEFAULT_SECONDS,
    MAX_SECONDS,
    MciCapture,
    NullCapture,
    clamp_seconds,
    default_capture_path,
)
from voice.capture import build_capture as build_audio_capture  # noqa: E402
from voice.manager import SpeechPipeline, TranscribePipeline, VoicePipeline  # noqa: E402
from voice.stt import (  # noqa: E402
    ENGINES,
    NullSttProvider,
    VoskSttProvider,
    build_stt,
    describe_engines,
    pcm16,
    probe_engines,
)
from voice.tts import NullSpeechProvider  # noqa: E402


def write_wav(path, frames=800):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x01\x02" * frames)
    return path


class SilentCapture(NullCapture):
    """按脚本回答的假录音：不动真实设备，只验流程。"""

    def __init__(self, ok=True, seconds=1.0, payload=b"\x00\x01" * 400, error="录不到"):
        super().__init__("测试用假录音")
        self.name = "silent"
        self.ok = ok
        self.error = error
        self.seconds = seconds
        self.payload = payload
        self.calls = []

    def is_available(self):
        return self.ok

    def describe(self):
        return {"provider": self.name, "available": self.ok, "note": "测试用假录音"}

    def record(self, seconds=None, path=None):
        self.calls.append((seconds, path))
        if not self.ok:
            return {"ok": False, "error": self.error, "path": None, "seconds": 0.0, "bytes": 0}
        target = Path(str(path))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.payload)
        return {
            "ok": True,
            "path": str(target),
            "seconds": self.seconds,
            "bytes": len(self.payload),
        }


class ScriptedStt(NullSttProvider):
    def __init__(self, text="你好呀", available=True):
        self.text = text
        self.flag = available
        self.calls = []

    def is_available(self):
        return self.flag

    def transcribe(self, audio, sample_rate=None, language=None):
        self.calls.append(audio)
        return self.text if self.flag else ""

    def describe(self):
        return {"provider": "scripted", "available": self.flag, "note": ""}


class Permissions:
    def __init__(self, **flags):
        self.flags = flags

    def allows(self, key):
        return bool(self.flags.get(key))


class CaptureProbeTests(unittest.TestCase):
    def test_device_probe_returns_a_number_and_never_raises(self):
        count = MciCapture().devices()
        self.assertIsInstance(count, int)
        self.assertGreaterEqual(count, 0)

    def test_unavailable_capture_records_nothing_and_says_why(self):
        provider = NullCapture("这台机器没有可用的麦克风")
        result = provider.record(seconds=1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "这台机器没有可用的麦克风")
        self.assertIsNone(result["path"])
        self.assertFalse(provider.describe()["available"])

    def test_build_capture_respects_the_microphone_permission(self):
        provider = build_audio_capture(allows=Permissions(listen=False))
        self.assertFalse(provider.is_available())
        self.assertIn("权限", provider.describe()["note"])

    def test_build_capture_is_honest_when_there_is_no_device(self):
        provider = build_audio_capture(allows=Permissions(listen=True))
        if os.name == "nt" and MciCapture().devices() > 0:
            self.assertTrue(provider.is_available())
        else:
            self.assertFalse(provider.is_available())
            self.assertTrue(provider.describe()["note"])

    def test_injected_provider_is_used_as_is(self):
        provider = SilentCapture()
        self.assertIs(build_audio_capture(provider=provider), provider)

    def test_seconds_are_clamped_to_something_sane(self):
        self.assertEqual(clamp_seconds("nope"), DEFAULT_SECONDS)
        self.assertLessEqual(clamp_seconds(10 ** 6), MAX_SECONDS)
        self.assertGreaterEqual(clamp_seconds(-5), 0.2)

    def test_default_path_lands_in_the_recordings_folder(self):
        config = Config(load_env=False)
        path = default_capture_path(config)
        self.assertTrue(str(path).endswith(".wav"))
        self.assertEqual(path.parent.name, "recordings")


class RecordFlowTests(unittest.TestCase):
    def test_successful_recording_reports_real_files(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        target = Path(tmp.name) / "out" / "pick.wav"
        provider = MciCapture()
        provider.is_available = lambda: True
        written = {}

        def fake_command(text):
            if text.startswith("save "):
                staged = Path(text.split('"')[1])
                write_wav(staged)
                written["path"] = staged
            return True, ""

        provider._command = fake_command
        result = provider.record(seconds=0.2, path=target)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["path"], str(target))
        self.assertTrue(target.is_file())
        self.assertEqual(result["bytes"], target.stat().st_size)
        self.assertGreater(result["bytes"], 0)
        # 临时文件不留在磁盘上
        self.assertFalse(written["path"].exists())

    def test_device_that_records_nothing_is_reported_not_faked(self):
        provider = MciCapture()
        provider.is_available = lambda: True
        provider._command = lambda text: (True, "")
        result = provider.record(seconds=0.2)
        self.assertFalse(result["ok"])
        self.assertIn("没有录到声音", result["error"])

    def test_open_failure_is_reported_verbatim(self):
        provider = MciCapture()
        provider.is_available = lambda: True
        provider._command = lambda text: (False, "设备被占用")
        result = provider.record(seconds=0.2)
        self.assertFalse(result["ok"])
        self.assertIn("设备被占用", result["error"])
        # 失败原因留在状态里，界面下一眼就能看到，不是只在那一秒闪过。
        self.assertIn("设备被占用", provider.last_error)
        self.assertIn("上次录音失败", provider.describe()["note"])


class SttEngineTests(unittest.TestCase):
    def test_probe_covers_every_known_engine(self):
        found = probe_engines()
        self.assertEqual(set(found), set(ENGINES))
        for value in found.values():
            self.assertIsInstance(value, bool)

    def test_no_engine_means_no_stt_provider(self):
        config = Config(load_env=False)
        provider = build_stt(config)
        self.assertFalse(provider.is_available())
        self.assertEqual(provider.transcribe(b"\x00\x01"), "")

    def test_engine_without_a_model_stays_unavailable(self):
        provider = VoskSttProvider("")
        self.assertFalse(provider.is_available())
        self.assertIn("vosk", provider.describe()["note"])

    def test_missing_model_folder_stays_unavailable(self):
        provider = VoskSttProvider(str(Path(tempfile.gettempdir()) / "no-such-model"))
        self.assertFalse(provider.is_available())

    def test_describe_engines_is_honest(self):
        data = describe_engines(Config(load_env=False))
        self.assertIn("installed", data)
        self.assertIsInstance(data["installed"], list)
        self.assertEqual(data["model"], None)

    def test_pcm16_accepts_raw_bytes_and_wav_paths(self):
        self.assertEqual(pcm16(b"\x00\x01"), b"\x00\x01")
        self.assertEqual(pcm16(bytearray(b"\x02")), b"\x02")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = write_wav(Path(tmp.name) / "a.wav", frames=10)
        self.assertEqual(len(pcm16(path)), 20)
        self.assertEqual(pcm16(""), b"")
        self.assertEqual(pcm16(str(Path(tmp.name) / "missing.wav")), b"")
        self.assertEqual(pcm16(object()), b"")


class TranscribePipelineTests(unittest.TestCase):
    def test_transcribed_text_reaches_the_chat_channel(self):
        seen = []
        pipeline = TranscribePipeline(
            provider=ScriptedStt("把灯关了"), on_text=seen.append
        )
        self.assertEqual(pipeline.feed(b"\x00\x01"), "把灯关了")
        self.assertEqual(seen, ["把灯关了"])
        self.assertIn("把灯关了", pipeline.describe()["last_text"])

    def test_no_engine_does_not_deliver_anything(self):
        seen = []
        pipeline = TranscribePipeline(provider=NullSttProvider(), on_text=seen.append)
        self.assertEqual(pipeline.feed(b"\x00"), "")
        self.assertEqual(seen, [])
        self.assertFalse(pipeline.describe()["available"])

    def test_voice_output_and_recognition_are_independent(self):
        speech = SpeechPipeline(provider=NullSpeechProvider())
        stt = TranscribePipeline(provider=ScriptedStt())
        self.assertFalse(speech.available())
        self.assertTrue(stt.available())
        voice = VoicePipeline(provider=None)
        self.assertFalse(voice.available())
        self.assertEqual(voice.describe()["provider"], "null")


class ListLogger:
    """记下界面提示。设置窗口没开时，提示就走日志。"""

    def __init__(self):
        self.lines = []

    def info(self, *args):
        self.lines.append(" ".join(str(item) for item in args))

    def warning(self, *args):
        self.lines.append(" ".join(str(item) for item in args))

    def debug(self, *args):
        pass

    def text(self):
        return " ".join(self.lines)


class DesktopVoiceWiringTests(unittest.TestCase):
    def setUp(self):
        try:
            import tkinter as tk
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        try:
            self.window = tk.Tk()
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        self.window.withdraw()
        self.addCleanup(self._destroy)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _destroy(self):
        try:
            self.window.destroy()
        except Exception:
            pass

    def _pet(self, capture=None, stt_provider=None, listen=True):
        from agent.context import ContextManager
        from agent.core import AgentCore
        from database.db import Database
        from memory.manager import MemoryManager
        from memory.repository import MemoryRepository
        from model.base import ChatResult, ModelProvider
        from ui.desktop import DesktopPet

        class Silent(ModelProvider):
            name = "silent"

            def is_available(self):
                return True

            def chat(self, messages, tools=None, temperature=0.7):
                return ChatResult(ok=True, content="嗯。", model="silent")

            def stream(self, messages, tools=None, temperature=0.7):
                yield ChatResult(ok=True, content="嗯。", model="silent")

        base = Path(self.tmp.name)
        config = Config(load_env=False)
        config.data_dir = base
        config.log_dir = base / "logs"
        config.db_path = base / "t.sqlite3"
        config.characters_dir = base / "characters"
        config.live2d_dir = base / "live2d"
        config.devices_dir = base / "devices"
        config.voices_dir = base / "voices"
        config.tray = False
        database = Database(config.db_path)
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        agent = AgentCore(Silent(), memory, ContextManager(token_budget=800))
        privacy = PrivacyCenter(preferences=memory)
        privacy.set("listen", listen)
        self.logger = ListLogger()
        return DesktopPet(
            agent,
            config,
            memory=memory,
            root=self.window,
            capture=capture if capture is not None else SilentCapture(),
            stt=TranscribePipeline(
                provider=stt_provider if stt_provider is not None else ScriptedStt(),
                logger=None,
            ),
            privacy=privacy,
            logger=self.logger,
        )

    def test_recording_is_refused_when_the_microphone_is_off(self):
        pet = self._pet(listen=False)
        self.assertFalse(pet.record_phrase(1))
        self.assertFalse(pet._record_busy)

    def test_recording_is_refused_without_a_device(self):
        pet = self._pet(capture=SilentCapture(ok=False))
        self.assertFalse(pet.record_phrase(1))
        self.assertIn("录不了音", self.logger.text())

    def test_recording_is_refused_without_a_recognition_engine(self):
        pet = self._pet(stt_provider=NullSttProvider())
        self.assertFalse(pet.record_phrase(1))
        self.assertIn("识别引擎", self.logger.text())

    def test_recording_hands_the_text_to_the_chat_channel(self):
        seen = []
        pet = self._pet()
        pet.stt.on_text = seen.append
        self.assertTrue(pet.record_phrase(0.5))
        self.assertTrue(pet._record_busy)
        result = {"ok": True, "path": str(Path(self.tmp.name) / "a.wav"), "bytes": 12}
        # 直接走完成回调，避免依赖主循环
        pet.stt.provider.calls = []
        self.assertTrue(pet._record_done(result))
        self.assertEqual(seen, ["你好呀"])
        self.assertFalse(pet._record_busy)

    def test_empty_transcription_is_reported_not_swallowed(self):
        pet = self._pet(stt_provider=ScriptedStt("", available=True))
        pet.stt.on_text = lambda text: None
        self.assertFalse(pet._record_done({"ok": True, "path": "x", "bytes": 8}))
        self.assertIn("没听出内容", self.logger.text())

    def test_failed_recording_is_reported(self):
        pet = self._pet()
        self.assertFalse(pet._record_done({"ok": False, "error": "设备被占用"}))
        self.assertIn("设备被占用", self.logger.text())

    def test_rebuild_stt_keeps_a_working_provider(self):
        pet = self._pet()
        self.assertTrue(pet.rebuild_stt())
        self.assertIsInstance(pet.stt.provider, NullSttProvider)

    def test_voice_rows_report_capture_and_engine_separately(self):
        rows = dict(self._pet().voice_center_rows())
        self.assertIn("录音设备", rows)
        self.assertIn("识别引擎", rows)
        self.assertIn("语音输出", rows)
        self.assertIn("测试用假录音", rows["录音设备"])
        self.assertTrue(rows["识别引擎"].startswith("可用"))

    def test_voice_rows_say_why_the_engine_is_unusable(self):
        rows = dict(self._pet(stt_provider=NullSttProvider()).voice_center_rows())
        self.assertTrue(rows["识别引擎"].startswith("不可用"))

    def test_persona_history_is_reachable_from_the_host(self):
        pet = self._pet()
        before = pet.persona_version()
        pet.record_persona_change({"温柔": 50}, {"活泼": 80})
        self.assertEqual(pet.persona_version(), before + 1)
        self.assertEqual(pet.persona_history_rows()[0]["after"], {"活泼": 80})
        self.assertEqual(
            persona_history.version(pet.memory), pet.persona_version()
        )


if __name__ == "__main__":
    unittest.main()