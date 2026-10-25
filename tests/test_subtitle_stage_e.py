"""实时字幕阶段 E：浮窗阅读控制、复制和翻译引擎选择。"""

import sys
import time
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ui import theme
from ui.desktop import DesktopPet
from ui.subtitle_overlay import SubtitleOverlay, TEXT_COLOR_LABELS
from voice.capture import SoundCardStreamCapture, signal_level
from voice.stt import SherpaOnlineSttProvider


def segment(number, source, translated="", final=True):
    return {
        "segment_id": "sub-e:%d" % number,
        "source_text": source,
        "translated_text": translated,
        "is_final": final,
        "start_time": float(number),
        "created_at": time.time(),
    }


class OverlayOwner:
    def __init__(self, root):
        self.root = root
        self.prefs = {}
        self.hints = []

    def _preference(self, key):
        return self.prefs.get(key)

    def _save_preference(self, key, value):
        self.prefs[key] = value
        return True

    def _hint(self, text):
        self.hints.append(text)


class OverlayReadingTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.owner = OverlayOwner(self.root)
        self.overlay = SubtitleOverlay(self.owner)
        self.addCleanup(self.overlay.destroy)

    def test_recent_segment_limit_is_real(self):
        for number, text in ((1, "one"), (2, "two"), (3, "three")):
            self.overlay.update_segment(segment(number, text))
        self.assertEqual(self.overlay.set_recent_lines(2), 2)
        body = self.overlay.text.get("1.0", "end")
        self.assertNotIn("one", body)
        self.assertIn("two", body)
        self.assertIn("three", body)
        self.assertEqual(
            self.owner.prefs[self.overlay.ROWS_PREFERENCE], "2"
        )
        self.assertEqual(self.overlay.set_recent_lines(99), 12)
        self.assertEqual(self.overlay.set_recent_lines(0), 1)

    def test_copy_uses_the_visible_mode_and_real_clipboard(self):
        self.overlay.update_segment(segment(1, "hello", "你好"))
        self.overlay.set_mode("source")
        self.assertTrue(self.overlay.copy_current())
        self.assertEqual(self.root.clipboard_get(), "hello")
        self.overlay.set_mode("bilingual")
        self.assertTrue(self.overlay.copy_current())
        self.assertIn("你好", self.root.clipboard_get())

    def test_auto_scroll_can_pause_and_resume_without_losing_text(self):
        self.overlay.update_segment(segment(1, "first"))
        self.overlay.set_auto_scroll(False)
        self.assertFalse(self.overlay.auto_scroll)
        self.assertEqual(
            self.owner.prefs[self.overlay.SCROLL_PREFERENCE], "0"
        )
        with mock.patch.object(self.overlay.text, "yview_moveto") as restore:
            self.overlay.update_segment(segment(2, "second"))
        self.assertTrue(restore.called)
        self.assertIn("second", self.overlay.text.get("1.0", "end"))
        with mock.patch.object(self.overlay.text, "see") as see:
            self.overlay.set_auto_scroll(True)
        self.assertTrue(see.called)
        self.assertEqual(
            self.owner.prefs[self.overlay.SCROLL_PREFERENCE], "1"
        )

    def test_every_text_color_stays_readable_in_both_themes(self):
        original = theme.active()
        self.addCleanup(lambda: theme.apply(original))
        for mode in ("dark", "light"):
            theme.apply(mode)
            self.overlay.apply_theme()
            for color in TEXT_COLOR_LABELS:
                self.assertTrue(self.overlay.set_text_color(color, save=False))
                for role in ("source", "translation"):
                    ratio = theme.contrast_ratio(
                        self.overlay._color_for(role), theme.CARD
                    )
                    self.assertGreaterEqual(
                        ratio, 4.5, "%s/%s/%s=%.2f" % (mode, color, role, ratio)
                    )

    def test_unknown_font_or_color_is_refused(self):
        current = self.overlay.font_family
        self.assertFalse(self.overlay.set_font_family("No Such Font 12345"))
        self.assertEqual(self.overlay.font_family, current)
        self.assertFalse(self.overlay.set_text_color("rainbow"))
        self.assertEqual(self.overlay.text_color, "theme")


class TranslationRoleTests(unittest.TestCase):
    def make_pet(self):
        pet = DesktopPet.__new__(DesktopPet)
        pet.prefs = {}
        pet.hints = []
        pet._preference = lambda key: pet.prefs.get(key)

        def save(key, value):
            pet.prefs[key] = value
            return True

        pet._save_preference = save
        pet._hint = lambda text: pet.hints.append(text)
        pet.agent = SimpleNamespace(
            task_slot=SimpleNamespace(
                provider=SimpleNamespace(
                    name="task",
                    model="task-model",
                    base_url="http://127.0.0.1:11434/v1",
                    is_available=lambda: True,
                )
            ),
            companion_slot=SimpleNamespace(
                provider=SimpleNamespace(
                    name="companion",
                    model="chat-model",
                    base_url="http://127.0.0.1:11434/v1",
                    is_available=lambda: True,
                )
            ),
        )
        return pet

    def test_translation_can_use_task_companion_or_off(self):
        pet = self.make_pet()
        self.assertEqual(pet.subtitle_translation_role(), "task")
        self.assertIn("任务模型可用", pet.subtitle_translation_note())
        self.assertTrue(pet.set_subtitle_translation_role("companion"))
        self.assertIn("陪伴模型可用", pet.subtitle_translation_note())
        self.assertTrue(pet.set_subtitle_translation_role("off"))
        self.assertIn("关闭字幕翻译", pet.subtitle_translation_note())
        self.assertFalse(pet.set_subtitle_translation_role("unknown"))

    def test_the_selected_role_really_selects_its_provider(self):
        pet = self.make_pet()
        self.assertEqual(pet._subtitle_translation_provider().name, "task")
        pet.set_subtitle_translation_role("companion")
        self.assertEqual(
            pet._subtitle_translation_provider().name, "companion"
        )
        pet.set_subtitle_translation_role("off")
        self.assertIsNone(pet._subtitle_translation_provider())


class CaptureProbeTests(unittest.TestCase):
    def test_signal_level_uses_real_samples(self):
        level = signal_level([0.0, 0.25, -0.5, 0.0])
        self.assertEqual(level["percent"], 50)
        self.assertGreater(level["rms"], 0.0)
        self.assertGreater(level["db"], -120.0)
        self.assertEqual(signal_level([])["percent"], 0)

    def test_source_probe_reads_a_short_sample_and_stops_the_device(self):
        class Probe(SoundCardStreamCapture):
            def __init__(self):
                super().__init__()
                self.started = ""
                self.stopped = False
                self.reads = 0

            def start(self, source_id=""):
                self.started = source_id
                return True

            def read(self, timeout=0.2):
                import time

                time.sleep(0.02)
                self.reads += 1
                if self.reads > 3:
                    return None
                return [0.2, -0.4]

            def stop(self):
                self.stopped = True
                return True

        capture = Probe()
        result = capture.probe("speaker", seconds=0.2)
        self.assertTrue(result["ok"])
        self.assertEqual(result["level"]["percent"], 40)
        self.assertEqual(capture.started, "speaker")
        self.assertTrue(capture.stopped)


class RecognitionPerformanceTests(unittest.TestCase):
    def test_thread_count_is_bounded_and_reported(self):
        provider = SherpaOnlineSttProvider(num_threads=99)
        self.assertEqual(provider.num_threads, 16)
        self.assertEqual(provider.describe()["num_threads"], 16)
        self.assertEqual(
            SherpaOnlineSttProvider(num_threads=0).num_threads, 1
        )

    def test_saved_thread_preference_is_bounded(self):
        pet = DesktopPet.__new__(DesktopPet)
        pet._preference = lambda key: "99"
        self.assertEqual(pet.subtitle_asr_threads(), 8)
        pet._preference = lambda key: "bad"
        self.assertEqual(pet.subtitle_asr_threads(), 2)


class OverlayPreferenceWiringTests(unittest.TestCase):
    def test_overlay_preferences_are_clamped_and_saved(self):
        pet = DesktopPet.__new__(DesktopPet)
        pet.prefs = {}
        pet._preference = lambda key: pet.prefs.get(key)

        def save(key, value):
            pet.prefs[key] = value
            return True

        pet._save_preference = save
        overlay = mock.Mock()
        overlay.set_font_family.return_value = True
        overlay.set_text_color.return_value = True
        pet._ensure_subtitle_overlay = lambda: overlay

        self.assertTrue(pet.set_subtitle_overlay_option("recent_lines", 99))
        overlay.set_recent_lines.assert_called_once_with(12, save=False)
        self.assertEqual(pet.prefs["subtitle.overlay.rows"], "12")
        self.assertTrue(pet.set_subtitle_overlay_option("auto_scroll", False))
        overlay.set_auto_scroll.assert_called_once_with(False, save=False)
        self.assertEqual(
            pet.prefs["subtitle.overlay.auto_scroll"], "0"
        )
        self.assertTrue(pet.set_subtitle_overlay_option("text_color", "sky"))
        overlay.set_text_color.assert_called_once_with("sky", save=False)


if __name__ == "__main__":
    unittest.main()
