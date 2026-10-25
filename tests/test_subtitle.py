"""实时字幕阶段 B：模拟音频、假识别、假翻译和权限边界。"""

import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from model.base import ChatResult
from security.privacy import CONTROLS, PrivacyCenter
from ui.subtitle_overlay import SubtitleOverlay
from voice.capture import SoundCardStreamCapture
from voice.stt import (
    AsrUpdate,
    NullStreamingSttProvider,
    SherpaOnlineSttProvider,
    build_streaming_stt,
)
from voice.subtitle import SubtitlePipeline, SubtitleTranslator


class FakeAsr:
    name = "fake-asr"
    sample_rate = 16000

    def __init__(self):
        self.updates = []
        self.started = 0
        self.stopped = 0
        self.final_text = ""

    def is_available(self):
        return True

    def start(self):
        self.started += 1
        return True

    def accept(self, audio, sample_rate=None):
        return list(self.updates.pop(0)) if self.updates else []

    def finish(self):
        return AsrUpdate(self.final_text, True) if self.final_text else None

    def stop(self):
        self.stopped += 1
        return True

    def describe(self):
        return {"available": True, "note": ""}


class FakeCapture:
    name = "fake-capture"
    sample_rate = 16000

    def __init__(self):
        self.last_error = ""
        self.started = ""
        self.paused = False

    def sources(self):
        return [{"id": "loop", "name": "测试扬声器", "kind": "system"}]

    def start(self, source_id=""):
        self.started = source_id
        return True

    def read(self, timeout=0.2):
        return None

    def pause(self):
        self.paused = True
        return True

    def resume(self):
        self.paused = False
        return True

    def stop(self):
        return True

    def describe(self):
        return {"available": True, "note": ""}


class FakeTranslator:
    def __init__(self, mapping=None, delay=0.0, block=None):
        self.mapping = dict(mapping or {})
        self.delay = delay
        self.block = block
        self.calls = []

    def available(self):
        return True

    def translate(self, text, source_language="", target_language=""):
        self.calls.append((text, source_language, target_language))
        if self.block is not None:
            self.block.wait(2)
        if self.delay:
            time.sleep(self.delay)
        if text == "fail":
            return False, "", "模拟翻译失败"
        return True, self.mapping.get(text, "T:" + text), ""


class FakeProvider:
    name = "fake-model"
    model = "fake-translate"
    base_url = "http://127.0.0.1:11434/v1"

    def __init__(self):
        self.messages = None

    def is_available(self):
        return True

    def chat(self, messages, tools=None, temperature=0.7):
        self.messages = messages
        return ChatResult(ok=True, content="你好", model=self.model)


class FakePreferences:
    def __init__(self):
        self.values = {}

    def get_preference(self, key):
        return self.values.get(key)

    def set_preference(self, key, value):
        self.values[key] = value
        return True


def wait_for(predicate, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class SubtitlePipelineTests(unittest.TestCase):
    def make_pipeline(self, updates, translator=None):
        asr = FakeAsr()
        asr.updates = list(updates)
        capture = FakeCapture()
        events = []
        pipeline = SubtitlePipeline(
            capture,
            asr,
            translator=translator,
            on_event=events.append,
            max_translation_queue=2,
        )
        self.assertTrue(pipeline.begin(source_language="en", target_language="中文"))
        self.addCleanup(pipeline.stop)
        return pipeline, capture, events

    def test_partial_result_is_not_translated(self):
        translator = FakeTranslator()
        pipeline, _capture, events = self.make_pipeline(
            [[AsrUpdate("hello", False)]], translator
        )
        pipeline.feed_audio([0.0] * 160)
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0].is_final)
        self.assertEqual(events[0].translation_status, "idle")
        self.assertEqual(translator.calls, [])

    def test_final_sentence_is_translated_asynchronously(self):
        translator = FakeTranslator({"hello": "你好"})
        pipeline, _capture, events = self.make_pipeline(
            [
                [AsrUpdate("hel", False)],
                [AsrUpdate("hello", True)],
            ],
            translator,
        )
        pipeline.feed_audio([0.0] * 160)
        pipeline.feed_audio([0.0] * 160)
        self.assertTrue(
            wait_for(
                lambda: any(
                    item.is_final and item.translation_status == "done" for item in events
                )
            )
        )
        final = [item for item in events if item.is_final and item.translated_text][-1]
        self.assertEqual(final.source_text, "hello")
        self.assertEqual(final.translated_text, "你好")
        self.assertEqual(translator.calls[0][2], "中文")

    def test_two_final_sentences_keep_their_own_translation(self):
        translator = FakeTranslator({"one": "一", "two": "二"})
        pipeline, _capture, _events = self.make_pipeline(
            [
                [AsrUpdate("one", True)],
                [AsrUpdate("two", True)],
            ],
            translator,
        )
        pipeline.feed_audio([0.0] * 160)
        pipeline.feed_audio([0.0] * 160)
        self.assertTrue(
            wait_for(
                lambda: len(
                    [
                        item
                        for item in pipeline.latest(8)
                        if item.translation_status == "done"
                    ]
                )
                == 2
            )
        )
        rows = {item.source_text: item.translated_text for item in pipeline.latest(8)}
        self.assertEqual(rows["one"], "一")
        self.assertEqual(rows["two"], "二")

    def test_translation_failure_keeps_source_text(self):
        translator = FakeTranslator()
        pipeline, _capture, _events = self.make_pipeline(
            [[AsrUpdate("fail", True)]], translator
        )
        pipeline.feed_audio([0.0] * 160)
        self.assertTrue(
            wait_for(
                lambda: any(
                    item.translation_status == "failed"
                    for item in pipeline.latest(4)
                )
            )
        )
        row = pipeline.latest(4)[-1]
        self.assertEqual(row.source_text, "fail")
        self.assertEqual(row.translation_status, "failed")

    def test_pause_and_resume_are_passed_to_capture(self):
        pipeline, capture, _events = self.make_pipeline([])
        self.assertTrue(pipeline.pause())
        self.assertTrue(capture.paused)
        self.assertTrue(pipeline.resume())
        self.assertFalse(capture.paused)

    def test_stop_flushes_last_partial_as_final(self):
        asr = FakeAsr()
        capture = FakeCapture()
        events = []
        pipeline = SubtitlePipeline(capture, asr, on_event=events.append)
        self.assertTrue(pipeline.begin())
        asr.accept = lambda audio, sample_rate=None: [AsrUpdate("last", False)]
        pipeline.feed_audio([0.0] * 160)
        asr.final_text = "last"
        pipeline.finish = lambda: None
        pipeline.stop()
        self.assertTrue(any(item.is_final and item.source_text == "last" for item in events))

    def test_read_exception_ends_session_and_releases_devices(self):
        class BrokenCapture(FakeCapture):
            def __init__(self):
                super().__init__()
                self.stops = 0

            def read(self, timeout=0.2):
                raise RuntimeError("设备已拔出")

            def stop(self):
                self.stops += 1
                return True

        asr = FakeAsr()
        capture = BrokenCapture()
        errors = []
        pipeline = SubtitlePipeline(
            capture, asr, on_error=errors.append, read_timeout=0.05
        )
        self.assertTrue(pipeline.start("loop"))
        self.assertTrue(wait_for(lambda: not pipeline.describe()["active"]))
        self.assertIn("设备已拔出", pipeline.last_error)
        self.assertEqual(capture.stops, 1)
        self.assertEqual(asr.stopped, 1)
        self.assertTrue(any("设备已拔出" in item for item in errors))

    def test_capture_error_without_exception_also_ends_session(self):
        class DisconnectedCapture(FakeCapture):
            def read(self, timeout=0.2):
                self.last_error = "音频设备已断开"
                return None

        asr = FakeAsr()
        capture = DisconnectedCapture()
        pipeline = SubtitlePipeline(capture, asr, read_timeout=0.05)
        self.assertTrue(pipeline.start("loop"))
        self.assertTrue(wait_for(lambda: not pipeline.describe()["active"]))
        self.assertEqual(pipeline.last_error, "音频设备已断开")
        self.assertEqual(asr.stopped, 1)

    def test_long_session_keeps_only_recent_segments(self):
        asr = FakeAsr()
        asr.updates = [[AsrUpdate("s%d" % index, True)] for index in range(6)]
        pipeline = SubtitlePipeline(FakeCapture(), asr, max_segments=3)
        self.addCleanup(pipeline.stop)
        self.assertTrue(pipeline.begin())
        for _index in range(6):
            pipeline.feed_audio([0.0] * 160)
        self.assertEqual(pipeline.describe()["segments"], 3)
        self.assertEqual(
            [item.source_text for item in pipeline.latest(10)], ["s3", "s4", "s5"]
        )

    def test_trimming_keeps_a_sentence_that_is_still_translating(self):
        block = threading.Event()
        translator = FakeTranslator(block=block)
        asr = FakeAsr()
        asr.updates = [
            [AsrUpdate("first", True)],
            [AsrUpdate("second", True)],
        ]
        pipeline = SubtitlePipeline(
            FakeCapture(), asr, translator=translator, max_segments=1
        )
        self.addCleanup(block.set)
        self.addCleanup(pipeline.stop)
        self.assertTrue(pipeline.begin(target_language="中文"))
        pipeline.feed_audio([0.0] * 160)
        pipeline.feed_audio([0.0] * 160)
        self.assertEqual(pipeline.describe()["segments"], 2)
        block.set()
        self.assertTrue(
            wait_for(
                lambda: [
                    item.translation_status for item in pipeline.latest(10)
                ]
                == ["done"]
            )
        )
        row = pipeline.latest(10)[0]
        self.assertEqual(row.source_text, "second")
        self.assertEqual(row.translated_text, "T:second")

    def test_asr_reported_error_ends_the_session(self):
        class BrokenAsr(FakeAsr):
            def __init__(self):
                super().__init__()
                self.last_error = ""

            def accept(self, audio, sample_rate=None):
                self.last_error = "在线识别失败：模型已失效"
                return []

        asr = BrokenAsr()
        pipeline = SubtitlePipeline(FakeCapture(), asr)
        self.addCleanup(pipeline.stop)
        self.assertTrue(pipeline.begin())
        pipeline.feed_audio([0.0] * 160)
        self.assertFalse(pipeline.describe()["active"])
        self.assertEqual(pipeline.last_error, "在线识别失败：模型已失效")
        self.assertEqual(asr.stopped, 1)

    def test_asr_exception_ends_the_session(self):
        class BrokenAsr(FakeAsr):
            def accept(self, audio, sample_rate=None):
                raise RuntimeError("识别器崩溃")

        asr = BrokenAsr()
        pipeline = SubtitlePipeline(FakeCapture(), asr)
        self.addCleanup(pipeline.stop)
        self.assertTrue(pipeline.begin())
        pipeline.feed_audio([0.0] * 160)
        self.assertFalse(pipeline.describe()["active"])
        self.assertIn("识别器崩溃", pipeline.last_error)
        self.assertEqual(asr.stopped, 1)


class StreamingSttTests(unittest.TestCase):
    def test_missing_model_returns_null_provider_with_reason(self):
        provider = build_streaming_stt("")
        self.assertIsInstance(provider, NullStreamingSttProvider)
        self.assertIn("模型", provider.describe()["note"])

    def test_transducer_and_paraformer_layouts_are_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tokens.txt").write_text("a\n", encoding="utf-8")
            (root / "encoder.onnx").write_bytes(b"x")
            (root / "decoder.onnx").write_bytes(b"x")
            (root / "joiner.onnx").write_bytes(b"x")
            provider = SherpaOnlineSttProvider(str(root))
            kind, _params = provider._plan()
            self.assertEqual(kind, "transducer")
            (root / "joiner.onnx").unlink()
            provider = SherpaOnlineSttProvider(str(root))
            kind, _params = provider._plan()
            self.assertEqual(kind, "paraformer")

    def test_unsupported_layout_is_not_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tokens.txt").write_text("a\n", encoding="utf-8")
            (root / "model.onnx").write_bytes(b"x")
            provider = SherpaOnlineSttProvider(str(root))
            self.assertIsNone(provider._plan())
            self.assertFalse(provider.is_available())

    def test_online_decode_failure_is_exposed_to_the_pipeline(self):
        class FakeStream:
            def accept_waveform(self, sample_rate, samples):
                return None

        class BrokenRecognizer:
            def decode_stream(self, stream):
                raise RuntimeError("推理失败")

        provider = SherpaOnlineSttProvider("")
        provider._stream = FakeStream()
        provider._recognizer = BrokenRecognizer()
        self.assertEqual(provider.accept([0.0], sample_rate=16000), [])
        self.assertIn("推理失败", provider.last_error)


class CaptureAndPermissionTests(unittest.TestCase):
    def test_missing_soundcard_is_reported_not_faked(self):
        capture = SoundCardStreamCapture()
        capture._module = lambda: (_ for _ in ()).throw(ImportError("no soundcard"))
        self.assertEqual(capture.sources(), [])
        self.assertIn("soundcard", capture.describe()["note"])

    def test_audio_permissions_default_to_off(self):
        preferences = FakePreferences()
        privacy = PrivacyCenter(preferences=preferences)
        self.assertFalse(privacy.allows("system_audio"))
        self.assertFalse(privacy.allows("subtitle_cloud"))
        keys = {row[0] for row in CONTROLS}
        self.assertIn("system_audio", keys)
        self.assertIn("subtitle_cloud", keys)

    def test_subtitle_translator_uses_task_provider(self):
        provider = FakeProvider()
        translator = SubtitleTranslator(provider)
        ok, text, error = translator.translate("hello", "en", "中文")
        self.assertTrue(ok, error)
        self.assertEqual(text, "你好")
        self.assertIn("Return only the translation", provider.messages[0]["content"])


class OverlayTests(unittest.TestCase):
    def test_overlay_renders_bilingual_text(self):
        try:
            root = tk.Tk()
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        root.withdraw()
        self.addCleanup(root.destroy)

        class Owner:
            def _preference(self, key):
                return ""

            def _save_preference(self, key, value):
                return True

            def _hint(self, text):
                return None

        owner = Owner()
        owner.root = root
        overlay = SubtitleOverlay(owner)
        self.addCleanup(overlay.destroy)
        overlay.update_segment(
            {
                "segment_id": "s1",
                "source_text": "hello",
                "translated_text": "你好",
                "is_final": True,
                "start_time": 1,
                "created_at": time.time(),
            }
        )
        text = overlay.text.get("1.0", "end")
        self.assertIn("hello", text)
        self.assertIn("你好", text)
        overlay.set_mode("source")
        self.assertNotIn("你好", overlay.text.get("1.0", "end"))
        overlay.set_session("测试扬声器设备名称很长很长", "en", "中文")
        session = overlay._session.cget("text")
        self.assertIn("测试扬声器", session)
        self.assertIn("en→中文", session)


if __name__ == "__main__":
    unittest.main()
