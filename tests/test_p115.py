"""P1：日历冲突/空闲/重复、提醒卡和文件整理撤销。"""

import sys
import tempfile
import time
import tkinter as tk
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database.db import Database
from events.bus import EventBus, REMINDER
from scheduler.manager import Scheduler
from scheduler.reminders import CalendarReminder
from scheduler.store import ScheduledJobStore
from security.audit import AuditLog
from memory.todo import TodoStore
from services.calendar import CalendarService
from services.file_organizer import FileOrganizer
from knowledge.service import KnowledgeService
from tools.file_organizer import register_file_organizer_tools
from tools.permission import PermissionManager
from tools.registry import ToolExecutor, ToolRegistry
from ui.desktop import DesktopPet
from ui.calendar_view import CalendarWindow
from ui.knowledge_view import KnowledgeWindow
from ui.notification import ReminderPopup


class Clock:
    def __init__(self, value):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += float(seconds)
        return self.value


def make_db():
    tmp = tempfile.TemporaryDirectory()
    db = Database(Path(tmp.name) / "t.sqlite3")
    db.migrate()
    return tmp, db


class CalendarV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.db = make_db()
        self.addCleanup(self.tmp.cleanup)
        self.now = datetime(2026, 10, 21, 8, 0)
        self.calendar = CalendarService(self.db, clock=lambda: self.now)

    def test_conflict_update_and_free_slots(self):
        first = self.calendar.add_event(
            "评审", "2026-10-21 10:00", end="2026-10-21 11:00"
        )
        self.assertTrue(first["ok"])
        second = self.calendar.add_event(
            "写方案", "2026-10-21 10:30", end="2026-10-21 11:30"
        )
        self.assertTrue(second["ok"])
        self.assertEqual(len(second["data"]["conflicts"]), 1)

        slots = self.calendar.free_slots("2026-10-21", work_start="09:00", work_end="18:00")[
            "data"
        ]["slots"]
        self.assertEqual(slots[0]["start"][11:16], "09:00")
        self.assertEqual(slots[0]["end"][11:16], "10:00")
        self.assertEqual(slots[-1]["start"][11:16], "11:30")

        moved = self.calendar.update_event(
            second["data"]["id"],
            when="2026-10-21 12:00",
            end="2026-10-21 13:00",
        )
        self.assertTrue(moved["ok"])
        self.assertEqual(moved["data"]["conflicts"], [])

    def test_repeating_event_expands_inside_range(self):
        self.calendar.add_event(
            "每日站会",
            "2026-10-21 09:00",
            end="2026-10-21 09:15",
            repeat_rule="daily",
        )
        rows = self.calendar.events_between(
            "2026-10-22 00:00", "2026-10-25 00:00"
        )["data"]["events"]
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["repeat_rule"] == "daily" for row in rows))
        self.assertEqual(rows[0]["when_at"][11:16], "09:00")

    def test_long_event_started_before_range_is_included(self):
        self.calendar.add_event(
            "长会议", "2026-10-21 08:00", end="2026-10-21 10:00"
        )
        rows = self.calendar.events_between(
            "2026-10-21 09:00", "2026-10-21 11:00"
        )["data"]["events"]
        self.assertEqual([row["title"] for row in rows], ["长会议"])

        slots = self.calendar.free_slots("2026-10-21")["data"]["slots"]
        self.assertEqual(slots[0]["start"][11:16], "10:00")

    def test_complete_changes_state(self):
        row = self.calendar.add_event("交周报", "2026-10-21 17:00")["data"]
        result = self.calendar.complete_event(row["id"])
        self.assertTrue(result["ok"])
        self.assertEqual(self.calendar.event(row["id"])["state"], "done")

    def test_changes_are_announced_for_reminder_rescheduling(self):
        changes = []
        self.calendar.on_change = lambda action, event_id: changes.append((action, event_id))
        row = self.calendar.add_event("同步", "2026-10-21 17:00")["data"]
        self.calendar.update_event(row["id"], title="同步一下")
        self.calendar.complete_event(row["id"])
        self.assertEqual(
            changes,
            [("add", row["id"]), ("update", row["id"]), ("complete", row["id"])],
        )


class CalendarReminderTests(unittest.TestCase):
    def test_reminder_becomes_a_real_due_event(self):
        tmp, db = make_db()
        self.addCleanup(tmp.cleanup)
        base = datetime(2026, 10, 21, 9, 0)
        calendar = CalendarService(db, clock=lambda: base)
        event = calendar.add_event(
            "见面", "2026-10-21 09:10", end="2026-10-21 09:40", remind_minutes=5
        )["data"]
        clock = Clock(base.timestamp())
        bus = EventBus()
        scheduler = Scheduler(
            bus, clock=clock, store=ScheduledJobStore(db, clock=clock)
        )
        seen = []
        bus.subscribe(REMINDER, seen.append)
        reminder = CalendarReminder(calendar, scheduler=scheduler, clock=lambda: base)
        self.assertTrue(reminder.schedule_event(event["id"]))
        self.assertEqual(len(scheduler.job_rows()), 1)
        self.assertEqual(scheduler.job_rows()[0]["event"], REMINDER)

        clock.advance(6 * 60)
        scheduler.tick(clock())
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].payload["kind"], "calendar")
        self.assertEqual(seen[0].payload["event_id"], event["id"])


class CalendarWindowV2Tests(unittest.TestCase):
    def test_edit_complete_and_free_slots_from_the_page(self):
        try:
            root = tk.Tk()
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        root.withdraw()
        self.addCleanup(root.destroy)
        tmp, db = make_db()
        self.addCleanup(tmp.cleanup)
        now = datetime(2026, 10, 21, 8, 0)
        calendar = CalendarService(db, clock=lambda: now)
        event = calendar.add_event(
            "评审", "2026-10-21 10:00", end="2026-10-21 11:00"
        )["data"]
        pet = SimpleNamespace(
            agent=SimpleNamespace(
                capabilities=SimpleNamespace(calendar=calendar),
                calendar_reminder=None,
            ),
            scheduler=None,
        )
        frame = tk.Frame(root)
        window = CalendarWindow(pet, parent=frame)
        self.assertTrue(window.edit_event(event["id"]))
        window.end_var.set("2026-10-21 11:30")
        window.priority_var.set("5")
        self.assertTrue(window.add_event())
        updated = calendar.event(event["id"])
        self.assertEqual(updated["end_at"], "2026-10-21 11:30")
        self.assertEqual(updated["priority"], 5)
        self.assertTrue(window.complete_event(event["id"]))
        self.assertEqual(calendar.event(event["id"])["state"], "done")
        self.assertTrue(window.show_free_slots())


class ReminderPopupTests(unittest.TestCase):
    def test_action_is_forwarded_to_host(self):
        try:
            root = tk.Tk()
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        root.withdraw()
        self.addCleanup(root.destroy)
        calls = []

        class Host:
            def __init__(self):
                self.root = root

            def handle_reminder_action(self, action, payload):
                calls.append((action, payload.get("event_id")))

        popup = ReminderPopup(
            Host(),
            {
                "kind": "calendar",
                "event_id": 7,
                "title": "开会",
                "when_at": "2026-10-21 10:00",
                "end_at": "2026-10-21 10:30",
            },
        )
        popup._act("complete")
        self.assertEqual(calls, [("complete", 7)])

    def test_desktop_complete_action_updates_calendar(self):
        tmp, db = make_db()
        self.addCleanup(tmp.cleanup)
        base = datetime(2026, 10, 21, 9, 0)
        calendar = CalendarService(db, clock=lambda: base)
        event = calendar.add_event("喝水", "2026-10-21 09:30")["data"]
        notes = []
        host = SimpleNamespace(
            agent=SimpleNamespace(
                capabilities=SimpleNamespace(calendar=calendar),
                calendar_reminder=None,
            ),
            scheduler=None,
            _hint=lambda text: notes.append(text),
        )
        ok = DesktopPet.handle_reminder_action(
            host,
            "complete",
            {"kind": "calendar", "event_id": event["id"]},
        )
        self.assertTrue(ok)
        self.assertEqual(calendar.event(event["id"])["state"], "done")

    def test_todo_postpone_moves_due_time_forward(self):
        tmp, db = make_db()
        self.addCleanup(tmp.cleanup)
        fixed = datetime(2026, 10, 21, 9, 0)
        todos = TodoStore(db, clock=lambda: fixed)
        row = todos.add("回信", due_at="2026-10-21 09:05")["data"]
        result = todos.postpone(row["id"], 15)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["due_at"], "2026-10-21 09:20")


class FileOrganizerTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.db = make_db()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "files"
        self.root.mkdir()
        self.service = FileOrganizer(
            self.db, authorized=lambda: [self.root], clock=time.time
        )

    def test_preview_apply_and_undo(self):
        (self.root / "note.txt").write_text("hello", encoding="utf-8")
        (self.root / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (self.root / "dup-a.txt").write_text("same", encoding="utf-8")
        (self.root / "dup-b.txt").write_text("same", encoding="utf-8")
        plan = self.service.preview(self.root)
        self.assertTrue(plan["ok"], plan)
        operation_id = plan["data"]["operation_id"]
        self.assertEqual(plan["data"]["files"], 4)
        self.assertGreaterEqual(len(plan["data"]["duplicates"]), 1)

        applied = self.service.apply(operation_id)
        self.assertTrue(applied["ok"], applied)
        self.assertFalse((self.root / "note.txt").exists())
        self.assertTrue((self.root / "documents" / "note.txt").is_file())
        self.assertTrue((self.root / "images" / "photo.png").is_file())

        undone = self.service.undo(operation_id)
        self.assertTrue(undone["ok"], undone)
        self.assertTrue((self.root / "note.txt").is_file())
        self.assertTrue((self.root / "photo.png").is_file())

    def test_knowledge_page_can_build_a_real_plan(self):
        try:
            root = tk.Tk()
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        root.withdraw()
        self.addCleanup(root.destroy)
        (self.root / "note.txt").write_text("hello", encoding="utf-8")
        knowledge = KnowledgeService(
            database=self.db, authorized=lambda: [self.root]
        )
        registry = ToolRegistry()
        register_file_organizer_tools(registry, service=self.service)
        executor = ToolExecutor(
            registry, PermissionManager(confirm_callback=lambda *args: True)
        )
        pet = SimpleNamespace(
            root=root,
            knowledge=knowledge,
            file_organizer=self.service,
            agent=SimpleNamespace(executor=executor),
        )
        frame = tk.Frame(root)
        window = KnowledgeWindow(pet, parent=frame)
        window.org_root_var.set(str(self.root))
        data = window.organize_preview()
        self.assertEqual(data["files"], 1)
        self.assertTrue(window._org_plan_id)

    def test_existing_target_stops_without_overwrite(self):
        (self.root / "note.txt").write_text("source", encoding="utf-8")
        target_dir = self.root / "documents"
        target_dir.mkdir()
        (target_dir / "note.txt").write_text("keep", encoding="utf-8")
        plan = self.service.preview(self.root)
        self.assertEqual(plan["data"]["conflicts"], 1)
        result = self.service.apply(plan["data"]["operation_id"])
        self.assertFalse(result["ok"])
        self.assertEqual((self.root / "note.txt").read_text(encoding="utf-8"), "source")
        self.assertEqual((target_dir / "note.txt").read_text(encoding="utf-8"), "keep")

    def test_apply_tool_requires_confirmation(self):
        (self.root / "note.txt").write_text("hello", encoding="utf-8")
        plan = self.service.preview(self.root)
        registry = ToolRegistry()
        register_file_organizer_tools(registry, service=self.service)
        executor = ToolExecutor(
            registry,
            PermissionManager(confirm_callback=lambda *args: False),
            audit=AuditLog(self.db),
        )
        outcome = executor.execute(
            "organize_apply", {"operation_id": plan["data"]["operation_id"]}
        )
        self.assertFalse(outcome.ok)
        self.assertFalse(outcome.allowed)
        self.assertTrue((self.root / "note.txt").is_file())


if __name__ == "__main__":
    unittest.main()
