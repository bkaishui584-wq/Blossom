"""收尾验收：日历入口、拖放解析与卡片底色。"""

import sys
import tempfile
import tkinter as tk
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database.db import Database
from services.calendar import CalendarService
from ui import dnd, theme
from ui.calendar_view import CalendarWindow
from ui.chat_wiring import ChatWiring
from ui.settings_view import SettingsWindow


class CalendarWindowTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "calendar.sqlite3")
        self.db.migrate()
        self.service = CalendarService(self.db)
        self.pet = SimpleNamespace(
            agent=SimpleNamespace(capabilities=SimpleNamespace(calendar=self.service))
        )
        self.frame = tk.Frame(self.root)
        self.window = CalendarWindow(self.pet, parent=self.frame)

    def test_navigation_opens_the_calendar_page(self):
        class Fake(ChatWiring):
            def __init__(self):
                self.hits = []

            def __getattr__(self, name):
                if name.startswith("open_") or name in ("new_session", "_chat_search"):
                    return lambda *args, **kwargs: None
                raise AttributeError(name)

            def open_calendar(self):
                self.hits.append("calendar")
                return "opened"

        view = Fake()
        self.assertEqual(view._chat_nav("calendar"), "opened")
        self.assertEqual(view._chat_open("calendar"), "opened")
        self.assertEqual(view.hits, ["calendar", "calendar"])

    def test_month_view_adds_and_deletes_a_real_event(self):
        today = date.today()
        day = today.replace(day=min(15, max(1, today.day)))
        when = "%s 10:00" % day.strftime("%Y-%m-%d")
        self.window.title_var.set("测试日程")
        self.window.when_var.set(when)
        self.assertTrue(self.window.add_event())
        rows = self.service.events_between(
            "%s 00:00" % day.strftime("%Y-%m-%d"),
            "%s 23:59" % day.strftime("%Y-%m-%d"),
        )["data"]["events"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "测试日程")
        self.assertTrue(self.window.delete_event(rows[0]["id"]))
        self.assertEqual(
            self.service.events_between(
                "%s 00:00" % day.strftime("%Y-%m-%d"),
                "%s 23:59" % day.strftime("%Y-%m-%d"),
            )["data"]["count"],
            0,
        )

    def test_month_shift_changes_the_heading(self):
        before = self.window.month_text()
        self.window.shift_month(1)
        self.assertNotEqual(self.window.month_text(), before)
        self.window.shift_month(-1)
        self.assertEqual(self.window.month_text(), before)


class DragDropTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def test_split_files_understands_tk_list_syntax(self):
        self.assertEqual(
            dnd.split_files(self.root, "{C:/a b/one.png} C:/two.jpg"),
            ["C:/a b/one.png", "C:/two.jpg"],
        )

    def test_settings_drop_uses_the_first_image_only(self):
        window = SettingsWindow.__new__(SettingsWindow)
        window.pet = mock.Mock()
        window.pet.set_background.return_value = {"ok": True}
        window._refresh_background_state = mock.Mock()
        window._hint = mock.Mock()
        self.assertTrue(window._drop_background(["C:/one.png", "C:/two.png"]))
        window.pet.set_background.assert_called_once_with("C:/one.png")
        self.assertIn("只用了第一张", window._hint.call_args.args[0])


class CardSurfaceTests(unittest.TestCase):
    def test_cardify_moves_only_background_surfaces(self):
        try:
            root = tk.Tk()
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        root.withdraw()
        self.addCleanup(root.destroy)
        frame = tk.Frame(root, bg=theme.BG)
        label = tk.Label(frame, text="文字", bg=theme.BG, fg=theme.TEXT)
        label.pack()
        field = tk.Entry(frame, bg=theme.FIELD)
        field.pack()
        theme.cardify(frame)
        self.assertEqual(str(frame.cget("bg")), theme.CARD)
        self.assertEqual(str(label.cget("bg")), theme.CARD)
        self.assertEqual(str(label.cget("fg")), theme.TEXT)
        self.assertEqual(str(field.cget("bg")), theme.FIELD)


if __name__ == "__main__":
    unittest.main()
