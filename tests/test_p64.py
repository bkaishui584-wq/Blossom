"""阶段 P64 验收：Local First 第三阶段。事件驱动的生命状态与待办到点提醒。

要点：
    生命状态只能按合法路径走，模型改不了，睡觉也只能从该睡的状态进；
    每次切换都发一条事件，界面据此显示；
    待办到点由程序发提醒，重启后补挂、做完的不再响；
    整条链路不调用模型。
"""

import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from behavior.life import (  # noqa: E402
    ACTIVE,
    IDLE,
    LABELS,
    LifeStateMachine,
    RESTING,
    SLEEPING,
    WORKING,
)
from database.db import Database  # noqa: E402
from events import bus as bus_module  # noqa: E402
from events.bus import EventBus  # noqa: E402
from memory.todo import TodoStore  # noqa: E402
from scheduler.reminders import TodoReminder  # noqa: E402
from scheduler import rules as rules_module  # noqa: E402
from scheduler.manager import Scheduler  # noqa: E402
from tools.local import register_local_tools  # noqa: E402
from tools.permission import PermissionManager  # noqa: E402
from tools.registry import ToolExecutor, ToolRegistry  # noqa: E402

FIXED_NOW = datetime(2026, 10, 11, 10, 0, 0)
PAST = "2020-01-01 09:00"
FUTURE = "2099-01-01 09:00"


class FakeScheduler:
    """只记账，不起线程。"""

    def __init__(self):
        self.calls = []

    def schedule_once(self, seconds, **payload):
        self.calls.append((seconds, payload))
        return None


def temp_db():
    tmp = tempfile.TemporaryDirectory()
    database = Database(Path(tmp.name) / "deskpet.sqlite3")
    database.migrate()
    return tmp, database


class LifeStateTests(unittest.TestCase):
    def test_starts_idle(self):
        self.assertEqual(LifeStateMachine().state, IDLE)

    def test_legal_transition_moves_and_reports(self):
        machine = LifeStateMachine()
        self.assertTrue(machine.transition(WORKING, "开始处理"))
        self.assertEqual(machine.state, WORKING)
        self.assertEqual(machine.label(), LABELS[WORKING])
        self.assertEqual(machine.describe()["reason"], "开始处理")

    def test_unknown_state_is_refused(self):
        machine = LifeStateMachine()
        self.assertFalse(machine.transition("dreaming", "乱来"))
        self.assertEqual(machine.state, IDLE)

    def test_sleeping_cannot_jump_straight_to_working(self):
        machine = LifeStateMachine()
        machine.note_sleep()
        self.assertEqual(machine.state, SLEEPING)
        self.assertFalse(machine.transition(WORKING, "偷偷工作"))
        self.assertTrue(machine.transition(ACTIVE, "用户回来了"))

    def test_repeat_of_same_state_is_a_noop(self):
        machine = LifeStateMachine()
        self.assertFalse(machine.transition(IDLE, "还是待机"))
        self.assertEqual(machine.history, [])

    def test_transition_publishes_an_event(self):
        bus = EventBus()
        seen = []
        bus.subscribe(bus_module.LIFE_STATE, lambda event: seen.append(event.payload))
        machine = LifeStateMachine(bus=bus).attach(bus)
        machine.transition(ACTIVE, "用户说话")
        self.assertEqual(seen[-1]["state"], ACTIVE)
        self.assertEqual(seen[-1]["previous"], IDLE)
        self.assertEqual(seen[-1]["label"], LABELS[ACTIVE])


class LifeWiringTests(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()
        self.machine = LifeStateMachine(bus=self.bus).attach(self.bus)

    def test_user_message_wakes_the_pet(self):
        self.bus.emit(bus_module.USER_MESSAGE, text="在吗")
        self.assertEqual(self.machine.state, ACTIVE)

    def test_user_leaving_goes_idle_and_returning_wakes(self):
        self.bus.emit(bus_module.USER_MESSAGE, text="在吗")
        self.bus.emit(bus_module.USER_IDLE, idle_seconds=3000)
        self.assertEqual(self.machine.state, IDLE)
        self.bus.emit(bus_module.USER_RETURNED, idle_seconds=0)
        self.assertEqual(self.machine.state, ACTIVE)

    def test_tool_completion_marks_working(self):
        self.bus.emit(bus_module.USER_MESSAGE, text="跑个测试")
        self.bus.emit(bus_module.TOOL_COMPLETED, name="run_tests", ok=True)
        self.assertEqual(self.machine.state, WORKING)

    def test_no_bus_keeps_manual_control(self):
        machine = LifeStateMachine(bus=None).attach(None)
        self.assertTrue(machine.note_work("手动"))
        self.assertEqual(machine.state, WORKING)


class TodoReminderTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.db = temp_db()
        self.addCleanup(self.tmp.cleanup)
        self.todos = TodoStore(self.db, clock=lambda: FIXED_NOW)
        self.scheduler = FakeScheduler()
        self.reminder = TodoReminder(
            self.todos, scheduler=self.scheduler, clock=lambda: FIXED_NOW
        )

    def test_arm_reschedules_future_todos(self):
        self.todos.add("开会", due_at=FUTURE)
        self.assertEqual(self.reminder.arm(), 1)
        self.assertEqual(len(self.scheduler.calls), 1)
        self.assertIn("开会", self.scheduler.calls[0][1]["local_text"])

    def test_arm_handles_overdue_todos_soon(self):
        self.todos.add("交作业", due_at=PAST)
        self.reminder.arm()
        self.assertEqual(len(self.scheduler.calls), 1)
        self.assertEqual(self.scheduler.calls[0][0], self.reminder.catch_up)

    def test_done_todos_are_not_rearmed(self):
        row = self.todos.add("已做的事", due_at=FUTURE)["data"]
        self.todos.done(row["id"])
        self.assertEqual(self.reminder.arm(), 0)
        self.assertEqual(self.scheduler.calls, [])

    def test_timeless_todos_do_not_get_a_timer(self):
        self.todos.add("随手记")
        self.assertEqual(self.reminder.arm(), 0)

    def test_overdue_is_reported_only_once(self):
        self.todos.add("过期的事", due_at=PAST)
        first = self.reminder.pending_overdue()
        self.assertIsNotNone(first)
        self.reminder.mark(first["id"])
        self.assertIsNone(self.reminder.pending_overdue())

    def test_take_overdue_marks_it(self):
        self.todos.add("过期的事", due_at=PAST)
        taken = self.reminder.take_overdue()
        self.assertIsNotNone(taken)
        self.assertIsNone(self.reminder.take_overdue())

    def test_schedule_todo_arms_a_new_todo_immediately(self):
        row = self.todos.add("新待办", due_at=FUTURE)["data"]
        self.assertTrue(self.reminder.schedule_todo(row["id"]))
        self.assertEqual(len(self.scheduler.calls), 1)

    def test_schedule_todo_ignores_timeless_entries(self):
        row = self.todos.add("没时间")["data"]
        self.assertFalse(self.reminder.schedule_todo(row["id"]))

    def test_arm_without_scheduler_is_safe(self):
        self.todos.add("开会", due_at=FUTURE)
        bare = TodoReminder(self.todos, scheduler=None, clock=lambda: FIXED_NOW)
        self.assertEqual(bare.arm(), 0)

    def test_broken_due_text_is_skipped(self):
        self.db.execute(
            "INSERT INTO todos (title, due_at) VALUES (?, ?)", ("坏时间", "下周三")
        )
        self.assertEqual(self.reminder.arm(), 0)


class TodoToolHookTests(unittest.TestCase):
    def test_adding_a_todo_arms_a_reminder(self):
        tmp, database = temp_db()
        self.addCleanup(tmp.cleanup)
        todos = TodoStore(database)
        scheduler = FakeScheduler()
        reminder = TodoReminder(todos, scheduler=scheduler)
        registry = ToolRegistry()
        register_local_tools(
            registry,
            database=database,
            todos=todos,
            on_todo_added=reminder.schedule_todo,
        )
        executor = ToolExecutor(registry, PermissionManager())
        outcome = executor.execute("todo_add", {"title": "写周报", "due": FUTURE})
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(len(scheduler.calls), 1)
        self.assertIn("写周报", scheduler.calls[0][1]["local_text"])

    def test_hook_failure_does_not_break_the_tool(self):
        tmp, database = temp_db()
        self.addCleanup(tmp.cleanup)

        def boom(_todo_id):
            raise RuntimeError("闹钟坏了")

        registry = ToolRegistry()
        register_local_tools(registry, database=database, on_todo_added=boom)
        executor = ToolExecutor(registry, PermissionManager())
        outcome = executor.execute("todo_add", {"title": "照样记下", "due": FUTURE})
        self.assertTrue(outcome.ok, outcome.error)


class FakeClock:
    def __init__(self, stamp):
        self.stamp = stamp

    def __call__(self):
        return self.stamp


def at(hour, minute=0):
    parts = list(time.localtime())
    parts[3:6] = [hour, minute, 0]
    return time.mktime(time.struct_time(parts))


class FakeWeather:
    def __init__(self, temperature=18, ok=True):
        self.temperature = temperature
        self.ok = ok
        self.calls = 0

    def current(self, city=""):
        self.calls += 1
        if not self.ok:
            return {"ok": False, "error": "取不到天气"}
        return {
            "ok": True,
            "data": {"city": "北京", "temperature": self.temperature, "weather": "晴"},
        }


class ProactiveRuleTests(unittest.TestCase):
    """晚间收尾和早间天气都由程序判断，只用真实数据；取不到就闭嘴。"""

    def build(self, hour, todos=None, weather=None, tasks=None):
        bus = EventBus()
        fired = []
        bus.subscribe(bus_module.COMPANION, lambda event: fired.append(event))
        scheduler = Scheduler(bus, clock=FakeClock(at(hour)))
        config = SimpleNamespace(
            proactive=True, work_reminder_seconds=3600, proactive_topics=False
        )
        rules_module.install(
            scheduler, config, todos=todos, tasks=tasks, weather=weather
        )
        return scheduler, fired

    def test_new_rules_only_install_when_their_data_source_exists(self):
        scheduler, _ = self.build(19)
        names = [rule.name for rule in scheduler.rules]
        self.assertNotIn("daily_summary", names)
        self.assertNotIn("weather_morning", names)

    def test_daily_summary_speaks_in_the_evening_with_real_todos(self):
        tmp, database = temp_db()
        self.addCleanup(tmp.cleanup)
        todos = TodoStore(database)
        todos.add("写周报")
        scheduler, fired = self.build(19, todos=todos)
        self.assertTrue([rule for rule in scheduler.rules if rule.name == "daily_summary"])
        scheduler.tick()
        events = [event for event in fired if event.payload.get("rule") == "daily_summary"]
        self.assertEqual(len(events), 1)
        self.assertIn("写周报", events[0].payload["local_text"])

    def test_daily_summary_stays_quiet_when_there_is_nothing_to_report(self):
        tmp, database = temp_db()
        self.addCleanup(tmp.cleanup)
        scheduler, fired = self.build(19, todos=TodoStore(database))
        scheduler.tick()
        self.assertEqual(fired, [])

    def test_daily_summary_is_not_fired_in_the_morning(self):
        tmp, database = temp_db()
        self.addCleanup(tmp.cleanup)
        todos = TodoStore(database)
        todos.add("写周报")
        scheduler, fired = self.build(9, todos=todos)
        scheduler.tick()
        self.assertEqual([e for e in fired if e.payload.get("rule") == "daily_summary"], [])

    def test_weather_rule_speaks_only_with_real_data(self):
        weather = FakeWeather(temperature=18)
        scheduler, fired = self.build(8, weather=weather)
        scheduler.tick()
        events = [event for event in fired if event.payload.get("rule") == "weather_morning"]
        self.assertEqual(len(events), 1)
        self.assertIn("18", events[0].payload["local_text"])
        self.assertIn("北京", events[0].payload["local_text"])

    def test_weather_rule_stays_quiet_when_the_service_fails(self):
        scheduler, fired = self.build(8, weather=FakeWeather(ok=False))
        scheduler.tick()
        self.assertEqual(
            [event for event in fired if event.payload.get("rule") == "weather_morning"], []
        )


if __name__ == "__main__":
    unittest.main()
