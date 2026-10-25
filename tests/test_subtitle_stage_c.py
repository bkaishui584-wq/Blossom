"""实时字幕阶段 C：历史、导出、外观、快捷键和交给 Agent 的边界。"""

import sys
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database.db import MIGRATIONS, Database
from ui.desktop import DesktopPet
from ui.subtitle_overlay import SubtitleOverlay
from ui.tray import TrayIcon
from voice.subtitle import SubtitleSegment
from voice.subtitle_history import SubtitleHistoryRepository


def make_segment(**changes):
    values = {
        "session_id": "sub-test",
        "segment_id": "sub-test:1",
        "source_language": "en",
        "target_language": "中文",
        "source_text": "hello",
        "translated_text": "",
        "is_final": True,
        "recognition_status": "final",
        "translation_status": "pending",
        "start_time": 1.0,
        "end_time": 2.5,
        "created_at": time.time(),
    }
    values.update(changes)
    return SubtitleSegment(**values)


class SubtitleHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "subtitle.sqlite3")
        self.db.migrate()
        self.history = SubtitleHistoryRepository(self.db)

    def test_migration_22_is_incremental(self):
        versions = [version for version, _name, _sql in MIGRATIONS]
        self.assertIn(22, versions)
        self.assertIn("subtitle_segments", self.db.health()["tables"])
        self.assertEqual(self.db.migrate(), [])

    def test_partial_is_not_saved_and_final_is_upserted(self):
        partial = make_segment(is_final=False, segment_id="sub-test:partial")
        self.assertFalse(self.history.save(partial))
        self.assertTrue(self.history.save(make_segment()))
        self.assertTrue(
            self.history.save(
                make_segment(
                    translated_text="你好",
                    translation_status="done",
                )
            )
        )
        rows = self.history.recent()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_text"], "hello")
        self.assertEqual(rows[0]["translated_text"], "你好")

    def test_clear_requires_confirmation(self):
        self.assertTrue(self.history.save(make_segment()))
        self.assertEqual(self.history.clear(), 0)
        self.assertEqual(self.history.count(), 1)
        self.assertEqual(self.history.clear(confirm=True), 1)
        self.assertEqual(self.history.count(), 0)

    def test_export_txt_srt_and_vtt(self):
        self.assertTrue(self.history.save(make_segment(translated_text="你好")))
        folder = Path(self.tmp.name)
        srt = self.history.export(folder / "out.srt", "srt")
        self.assertTrue(srt["ok"], srt.get("error"))
        text = (folder / "out.srt").read_text(encoding="utf-8")
        self.assertIn("00:00:01,000 --> 00:00:02,500", text)
        self.assertIn("hello", text)
        self.assertIn("你好", text)

        vtt = self.history.export(folder / "out.vtt", "vtt")
        self.assertTrue(vtt["ok"])
        self.assertTrue(
            (folder / "out.vtt").read_text(encoding="utf-8").startswith("WEBVTT")
        )

        txt = self.history.export(folder / "out.txt", "txt")
        self.assertTrue(txt["ok"])
        plain = (folder / "out.txt").read_text(encoding="utf-8")
        self.assertIn("[00:00:01] hello", plain)

    def test_empty_history_is_not_exported_as_fake_file(self):
        result = self.history.export(Path(self.tmp.name) / "empty.srt", "srt")
        self.assertFalse(result["ok"])
        self.assertIn("还没有", result["error"])


class SubtitleOverlayStageCTests(unittest.TestCase):
    def make_overlay(self):
        try:
            root = tk.Tk()
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        root.withdraw()
        self.addCleanup(root.destroy)

        class Owner:
            def __init__(self):
                self.root = root
                self.prefs = {}
                self.hints = []
                self.handed = None

            def _preference(self, key):
                return self.prefs.get(key)

            def _save_preference(self, key, value):
                self.prefs[key] = value
                return True

            def _hint(self, text):
                self.hints.append(text)

            def hand_subtitle_to_chat(self, action, text):
                self.handed = (action, text)
                return True

        owner = Owner()
        overlay = SubtitleOverlay(owner)
        self.addCleanup(overlay.destroy)
        return owner, overlay

    def test_appearance_has_a_readable_alpha_floor(self):
        owner, overlay = self.make_overlay()
        self.assertEqual(overlay.set_opacity(0.1, save=True), overlay.MIN_ALPHA)
        self.assertEqual(
            float(owner.prefs[overlay.ALPHA_PREFERENCE]), overlay.MIN_ALPHA
        )
        self.assertEqual(overlay.set_font_size(3), 9)
        self.assertEqual(overlay.set_line_spacing(99), 12)

    def test_agent_handoff_requires_selection_and_only_prefills(self):
        owner, overlay = self.make_overlay()
        overlay.update_segment(make_segment(source_text="hello world"))
        self.assertFalse(overlay._hand_to_agent("summary"))
        self.assertIsNone(owner.handed)

        overlay.text.tag_add(tk.SEL, "1.0", "1.5")
        self.assertTrue(overlay._hand_to_agent("summary"))
        self.assertEqual(owner.handed, ("summary", "hello"))


class SubtitleHotkeyTests(unittest.TestCase):
    class Owner:
        def __init__(self):
            self.calls = []

        def start_subtitles_from_preferences(self):
            self.calls.append("start_subtitles_from_preferences")

        def toggle_subtitle_pause(self):
            self.calls.append("toggle_subtitle_pause")

        def stop_subtitles(self):
            self.calls.append("stop_subtitles")

        def toggle_subtitle_overlay(self):
            self.calls.append("toggle_subtitle_overlay")

    def test_subtitle_hotkeys_dispatch_by_id(self):
        owner = self.Owner()
        icon = TrayIcon(
            owner,
            extra_hotkeys={
                "start_subtitles_from_preferences": "ctrl+alt+1",
                "toggle_subtitle_pause": "ctrl+alt+2",
                "stop_subtitles": "ctrl+alt+3",
                "toggle_subtitle_overlay": "ctrl+alt+4",
            },
        )
        self.assertTrue(icon._on_hotkey(2))
        self.assertTrue(icon._on_hotkey(3))
        self.assertTrue(icon._on_hotkey(4))
        self.assertTrue(icon._on_hotkey(5))
        self.assertFalse(icon._on_hotkey(99))
        self.assertEqual(
            owner.calls,
            [
                "start_subtitles_from_preferences",
                "toggle_subtitle_pause",
                "stop_subtitles",
                "toggle_subtitle_overlay",
            ],
        )

    def test_duplicate_subtitle_hotkeys_are_refused(self):
        pet = DesktopPet.__new__(DesktopPet)
        pet.config = SimpleNamespace(hotkey="")
        pet.tray = None
        pet.logger = None
        pet._preference = lambda key: ""
        pet._hint = lambda text: setattr(pet, "last_hint", text)
        pet._save_preference = lambda key, value: True
        self.assertFalse(
            pet.apply_subtitle_hotkeys(
                {
                    "start_subtitles_from_preferences": "ctrl+alt+1",
                    "toggle_subtitle_pause": "ctrl+alt+1",
                }
            )
        )
        self.assertIn("重复", pet.last_hint)


if __name__ == "__main__":
    unittest.main()
