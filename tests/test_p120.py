"""日历增强：月/周/日视图、ICS 导入导出与时区。"""

import sys
import tempfile
import tkinter as tk
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database.db import Database
from services.calendar import CalendarService
from ui.calendar_view import CalendarWindow


def make_calendar():
    tmp = tempfile.TemporaryDirectory()
    db = Database(Path(tmp.name) / "calendar.sqlite3")
    db.migrate()
    return tmp, db, CalendarService(db)


class CalendarIcsTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.db, self.calendar = make_calendar()
        self.addCleanup(self.tmp.cleanup)

    def test_export_writes_real_fields_escaping_and_folding(self):
        title = "每周评审：代码与项目" * 5
        note = "第一行, 含逗号; 含分号\\反斜杠\n第二行"
        self.calendar.add_event(
            title,
            "2026-10-21 10:00",
            end="2026-10-21 11:00",
            note=note,
            priority=6,
            repeat_rule="weekly",
            remind_minutes=15,
        )
        result = self.calendar.export_ics(
            "2026-10-01 00:00", "2026-11-01 00:00"
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["count"], 1)
        text = result["data"]["text"]
        self.assertIn("BEGIN:VCALENDAR", text)
        self.assertIn("DTSTART:20261021T100000", text)
        self.assertIn("DTEND:20261021T110000", text)
        self.assertIn("RRULE:FREQ=WEEKLY", text)
        self.assertIn("TRIGGER:-PT15M", text)
        self.assertIn("PRIORITY:6", text)
        self.assertIn("\\,", text)
        self.assertIn("\\;", text)
        self.assertIn("\\n", text)
        self.assertIn("\r\n ", text)
        for line in text.split("\r\n"):
            if line:
                self.assertLessEqual(len(line.encode("utf-8")), 75)

    def test_dry_run_does_not_write_and_duplicate_import_is_skipped(self):
        text = (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "BEGIN:VEVENT\r\n"
            "SUMMARY:导入测试\r\n"
            "DTSTART:20261022T090000\r\n"
            "DTEND:20261022T093000\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        preview = self.calendar.import_ics(text, dry_run=True)
        self.assertTrue(preview["ok"])
        self.assertEqual(preview["data"]["would_create"], 1)
        self.assertEqual(preview["data"]["created"], 0)
        self.assertEqual(
            self.db.query_one("SELECT COUNT(*) AS n FROM calendar_events")["n"], 0
        )

        first = self.calendar.import_ics(text)
        self.assertTrue(first["ok"])
        self.assertEqual(first["data"]["created"], 1)
        self.assertEqual(self.calendar.list_events()["data"]["count"], 1)

        again = self.calendar.import_ics(text)
        self.assertTrue(again["ok"])
        self.assertEqual(again["data"]["created"], 0)
        self.assertEqual(again["data"]["skipped"], 1)

    def test_import_keeps_repeat_reminder_and_completed_state(self):
        text = (
            "BEGIN:VCALENDAR\n"
            "BEGIN:VEVENT\n"
            "SUMMARY:已完成晨会\n"
            "DTSTART:20261021T090000\n"
            "DTEND:20261021T091500\n"
            "RRULE:FREQ=DAILY\n"
            "STATUS:COMPLETED\n"
            "BEGIN:VALARM\n"
            "TRIGGER:-PT20M\n"
            "ACTION:DISPLAY\n"
            "END:VALARM\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        result = self.calendar.import_ics(text)
        self.assertTrue(result["ok"], result)
        row = self.calendar.list_events()["data"]["events"][0]
        self.assertEqual(row["repeat_rule"], "daily")
        self.assertEqual(row["remind_minutes"], 20)
        self.assertEqual(row["state"], "done")

    def test_utc_and_tzid_times_become_local_wall_time(self):
        text = (
            "BEGIN:VCALENDAR\n"
            "BEGIN:VEVENT\n"
            "SUMMARY:上海时间\n"
            "DTSTART;TZID=Asia/Shanghai:20261021T090000\n"
            "DTEND;TZID=Asia/Shanghai:20261021T100000\n"
            "END:VEVENT\n"
            "BEGIN:VEVENT\n"
            "SUMMARY:UTC 时间\n"
            "DTSTART:20261021T010000Z\n"
            "DTEND:20261021T020000Z\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        result = self.calendar.import_ics(text)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["created"], 2)
        rows = {
            row["title"]: row
            for row in self.calendar.list_events()["data"]["events"]
        }
        shanghai = (
            datetime(2026, 10, 21, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
            .astimezone()
            .replace(tzinfo=None)
        )
        utc = (
            datetime(2026, 10, 21, 1, tzinfo=timezone.utc)
            .astimezone()
            .replace(tzinfo=None)
        )
        self.assertEqual(rows["上海时间"]["when_at"], shanghai.strftime("%Y-%m-%d %H:%M"))
        self.assertEqual(rows["UTC 时间"]["when_at"], utc.strftime("%Y-%m-%d %H:%M"))

    def test_empty_bad_and_unknown_timezone_files_do_not_write(self):
        self.assertFalse(self.calendar.import_ics("")["ok"])
        self.assertFalse(self.calendar.import_ics("not a calendar")["ok"])
        unknown = (
            "BEGIN:VCALENDAR\n"
            "BEGIN:VEVENT\n"
            "SUMMARY:未知时区\n"
            "DTSTART;TZID=No/Such_Zone:20261021T090000\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        result = self.calendar.import_ics(unknown)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["created"], 0)
        self.assertEqual(result["data"]["would_create"], 0)
        self.assertTrue(result["data"]["warnings"])
        self.assertIn("No/Such_Zone", result["data"]["warnings"][0])
        self.assertEqual(
            self.db.query_one("SELECT COUNT(*) AS n FROM calendar_events")["n"], 0
        )


class CalendarViewTests(unittest.TestCase):
    def test_ranges_use_monday_and_current_view(self):
        window = CalendarWindow.__new__(CalendarWindow)
        window.year = 2026
        window.month = 10
        window.selected = date(2026, 10, 22)
        window.view_mode = "month"
        self.assertEqual(window.month_range(), ("2026-10-01 00:00", "2026-11-01 00:00"))
        self.assertEqual(window.week_range(), ("2026-10-19 00:00", "2026-10-26 00:00"))
        self.assertEqual(window.day_range(), ("2026-10-22 00:00", "2026-10-23 00:00"))
        window.view_mode = "week"
        self.assertEqual(window.visible_range(), window.week_range())
        window.view_mode = "day"
        self.assertEqual(window.visible_range(), window.day_range())

    def test_shift_moves_week_and_day_by_the_expected_amount(self):
        window = CalendarWindow.__new__(CalendarWindow)
        window.selected = date(2026, 10, 22)
        window.year = 2026
        window.month = 10
        window.refresh = lambda: 0
        window.view_mode = "week"
        window.shift(1)
        self.assertEqual(window.selected, date(2026, 10, 29))
        window.view_mode = "day"
        window.shift(-1)
        self.assertEqual(window.selected, date(2026, 10, 28))

    def test_set_view_rejects_unknown_modes(self):
        window = CalendarWindow.__new__(CalendarWindow)
        window.view_mode = "month"
        window.refresh = lambda: 0
        self.assertFalse(window.set_view("year"))
        self.assertEqual(window.view_mode, "month")
        self.assertTrue(window.set_view("week"))
        self.assertEqual(window.view_mode, "week")


class CalendarWindowIcsTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.tmp, self.db, self.calendar = make_calendar()
        self.addCleanup(self.tmp.cleanup)
        self.pet = SimpleNamespace(
            agent=SimpleNamespace(
                capabilities=SimpleNamespace(calendar=self.calendar),
                calendar_reminder=None,
            ),
            scheduler=None,
        )
        self.frame = tk.Frame(self.root)
        self.window = CalendarWindow(self.pet, parent=self.frame)
        self.path = Path(self.tmp.name) / "calendar.ics"

    def test_import_cancel_confirm_and_export(self):
        today = date.today()
        self.calendar.add_event(
            "页面导出",
            "%s 10:00" % today.strftime("%Y-%m-%d"),
            end="%s 10:30" % today.strftime("%Y-%m-%d"),
        )
        exported = self.window.export_ics(self.path)
        self.assertEqual(exported, 1)
        self.assertIn("SUMMARY:页面导出", self.path.read_text(encoding="utf-8"))

        import_path = Path(self.tmp.name) / "import.ics"
        import_path.write_text(
            "BEGIN:VCALENDAR\n"
            "BEGIN:VEVENT\n"
            "SUMMARY:页面导入\n"
            "DTSTART:20261030T140000\n"
            "DTEND:20261030T150000\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n",
            encoding="utf-8",
        )
        before = self.calendar.list_events(limit=50)["data"]["count"]
        self.assertEqual(self.window.import_ics(import_path, confirm=lambda text: False), 0)
        self.assertEqual(self.calendar.list_events(limit=50)["data"]["count"], before)
        self.assertEqual(self.window.import_ics(import_path, confirm=lambda text: True), 1)
        self.assertEqual(self.calendar.list_events(limit=50)["data"]["count"], before + 1)


if __name__ == "__main__":
    unittest.main()
