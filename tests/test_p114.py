"""P0：持久计划任务、重启恢复、幂等和任务步骤原子写入。"""

import sys
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.task import TaskRecord, TaskStep
from agent.task_store import TaskStore
from database.db import Database
from events.bus import EventBus, COMPANION
from memory.todo import TodoStore
from scheduler.jobs import PersistentJobs
from scheduler.manager import Scheduler
from scheduler.reminders import TodoReminder
from scheduler.store import (
    PENDING,
    WAITING_CONFIRMATION,
    ScheduledJobStore,
)
from ui.schedule_view import ScheduleWindow


class Clock:
    def __init__(self, value=1000.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += float(seconds)
        return self.value


def database():
    tmp = tempfile.TemporaryDirectory()
    db = Database(Path(tmp.name) / "t.sqlite3")
    db.migrate()
    return tmp, db


class MigrationTests(unittest.TestCase):
    def test_scheduled_tables_are_created(self):
        tmp, db = database()
        self.addCleanup(tmp.cleanup)
        tables = set(db.health()["tables"])
        self.assertIn("scheduled_jobs", tables)
        self.assertIn("scheduled_job_runs", tables)
        self.assertIn(17, {row["version"] for row in db.query("SELECT version FROM schema_migrations")})


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.db = database()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.store = ScheduledJobStore(self.db, clock=self.clock)

    def test_idempotency_key_returns_the_same_job(self):
        first = self.store.create(
            COMPANION,
            self.clock(),
            {"local_text": "提醒"},
            idempotency_key="todo:1:2026-10-21 10:00",
        )
        second = self.store.create(
            COMPANION,
            self.clock() + 999,
            {"local_text": "另一条"},
            idempotency_key="todo:1:2026-10-21 10:00",
        )
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(self.store.recent_jobs()), 1)

    def test_running_non_idempotent_job_waits_for_confirmation_after_crash(self):
        job = self.store.create(
            COMPANION, self.clock(), {"local_text": "副作用"}, idempotent=False
        )
        self.assertTrue(self.store.claim(job.id, now=self.clock()))
        recovered = self.store.recover_interrupted(now=self.clock() + 1)
        self.assertEqual(recovered, [job.id])
        again = self.store.get(job.id)
        self.assertEqual(again.state, WAITING_CONFIRMATION)
        self.assertIn("不会自动重跑", again.error)
        self.assertTrue(self.store.resume(job.id, now=self.clock() + 2))
        resumed = self.store.get(job.id)
        self.assertEqual(resumed.state, PENDING)
        self.assertEqual(resumed.next_run_at, self.clock() + 2)

    def test_running_idempotent_job_is_requeued_after_crash(self):
        job = self.store.create(COMPANION, self.clock(), {"local_text": "安全"})
        self.store.claim(job.id, now=self.clock())
        self.store.recover_interrupted(now=self.clock() + 1)
        again = self.store.get(job.id)
        self.assertEqual(again.state, PENDING)
        self.assertEqual(again.next_run_at, self.clock() + 1)


class PersistentJobsTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.db = database()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.store = ScheduledJobStore(self.db, clock=self.clock)
        self.bus = EventBus()
        self.jobs = PersistentJobs(
            self.store, self.bus, clock=self.clock
        )

    def test_restart_does_not_deliver_the_same_job_twice(self):
        seen = []
        self.bus.subscribe(COMPANION, seen.append)
        job = self.jobs.schedule_once(
            5, COMPANION, {"local_text": "到点"}, idempotency_key="same"
        )
        self.clock.advance(6)
        self.jobs.run_due()

        restarted = PersistentJobs(
            ScheduledJobStore(self.db, clock=self.clock), self.bus, clock=self.clock
        )
        self.assertEqual(restarted.run_due(), [])
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.store.get(job.id).state, "completed")

    def test_a_failed_dispatch_retries_only_when_safe(self):
        attempts = []

        def broken(event):
            attempts.append(event.payload["local_text"])
            raise RuntimeError("临时失败")

        self.bus.subscribe(COMPANION, broken)
        job = self.jobs.schedule_once(1, COMPANION, {"local_text": "重试"})
        self.clock.advance(2)
        self.jobs.run_due()
        current = self.store.get(job.id)
        self.assertEqual(current.state, PENDING)
        self.assertEqual(current.attempts, 1)
        self.assertGreater(current.next_run_at, self.clock())

    def test_non_idempotent_failure_is_not_retried(self):
        def broken(event):
            raise RuntimeError("副作用不能重跑")

        self.bus.subscribe(COMPANION, broken)
        job = self.jobs.schedule_once(
            1, COMPANION, {"local_text": "一次"}, idempotent=False
        )
        self.clock.advance(2)
        self.jobs.run_due()
        current = self.store.get(job.id)
        self.assertEqual(current.state, "failed")
        self.assertEqual(current.attempts, 1)

    def test_interval_job_is_rescheduled_after_success(self):
        seen = []
        self.bus.subscribe(COMPANION, seen.append)
        job = self.jobs.schedule_interval(
            60, COMPANION, {"local_text": "周期"}, first_delay=1
        )
        self.clock.advance(2)
        self.jobs.run_due()
        current = self.store.get(job.id)
        self.assertEqual(current.state, PENDING)
        self.assertEqual(current.attempts, 0)
        self.assertEqual(current.next_run_at, self.clock() + 60)
        self.assertEqual(len(seen), 1)


class SchedulerWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.db = database()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.bus = EventBus()
        self.store = ScheduledJobStore(self.db, clock=self.clock)

    def test_scheduler_picks_up_a_job_created_by_a_previous_scheduler(self):
        seen = []
        self.bus.subscribe(COMPANION, seen.append)
        first = Scheduler(self.bus, clock=self.clock, store=self.store)
        job = first.schedule_once(5, COMPANION, local_text="跨重启")
        self.clock.advance(6)

        second = Scheduler(
            self.bus,
            clock=self.clock,
            store=ScheduledJobStore(self.db, clock=self.clock),
        )
        second.tick(self.clock())
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].payload["local_text"], "跨重启")
        self.assertEqual(self.store.get(job.id).state, "completed")

    def test_todo_reminders_are_not_duplicated_across_restart(self):
        todos = TodoStore(self.db, clock=lambda: __import__("datetime").datetime(2026, 10, 21, 9, 0))
        due = "2099-01-01 09:00"
        todo = todos.add("写周报", due_at=due)["data"]
        first = TodoReminder(todos, scheduler=Scheduler(self.bus, clock=self.clock, store=self.store))
        first.arm()
        first.arm()
        self.assertEqual(len(self.store.recent_jobs()), 1)

        restarted_store = ScheduledJobStore(self.db, clock=self.clock)
        restarted = TodoReminder(
            todos,
            scheduler=Scheduler(self.bus, clock=self.clock, store=restarted_store),
        )
        restarted.arm()
        self.assertEqual(len(restarted_store.recent_jobs()), 1)
        self.assertTrue(todo["id"])


class TaskStepAtomicityTests(unittest.TestCase):
    def test_bad_step_data_does_not_clear_existing_steps(self):
        tmp, db = database()
        self.addCleanup(tmp.cleanup)
        store = TaskStore(db)
        record = TaskRecord(goal="保留步骤")
        store.save(record)
        store.replace_steps(
            record.id,
            [
                TaskStep(index=1, title="第一步", status="done"),
                TaskStep(index=2, title="第二步", status="pending"),
            ],
        )
        self.assertEqual(len(store.steps(record.id)), 2)
        changed = store.replace_steps(
            record.id, [{"index": object(), "title": "坏步骤"}]
        )
        self.assertEqual(changed, 0)
        self.assertEqual(len(store.steps(record.id)), 2)


class ScheduleWindowTests(unittest.TestCase):
    def test_jobs_are_visible_and_cancellable(self):
        try:
            root = tk.Tk()
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        root.withdraw()
        self.addCleanup(root.destroy)
        tmp, db = database()
        self.addCleanup(tmp.cleanup)
        clock = Clock()
        scheduler = Scheduler(
            EventBus(), clock=clock, store=ScheduledJobStore(db, clock=clock)
        )
        job = scheduler.schedule_once(60, COMPANION, name="测试计划", local_text="提醒")
        frame = tk.Frame(root)
        window = ScheduleWindow(SimpleNamespace(scheduler=scheduler), parent=frame)
        self.assertEqual(window.jobs.get_children(), (job.id,))
        window.jobs.selection_set(job.id)
        self.assertTrue(window.cancel_selected())
        self.assertEqual(
            ScheduledJobStore(db, clock=clock).get(job.id).state, "cancelled"
        )


if __name__ == "__main__":
    unittest.main()
