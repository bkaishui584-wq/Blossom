"""待办到点提醒。程序按时间发事件，不轮询模型。

启动时把还没到期、设了时间的待办重新挂上一次性定时器；已经过期还没提醒的
补一句。运行中新加的待办由调度器每轮扫一遍兜底。做完的待办立刻不再提醒。
"""

from datetime import datetime, timedelta

from events import bus as bus_module
from services.calendar import parse_when


class TodoReminder:
    """把本地待办变成到点的提醒事件。只认状态是 open 的待办。"""

    def __init__(
        self,
        todos,
        scheduler=None,
        logger=None,
        clock=datetime.now,
        catch_up=15,
        policy=None,
    ):
        self.todos = todos
        self.scheduler = scheduler
        self.logger = logger
        self.clock = clock
        self.catch_up = max(1, int(catch_up))
        self.policy = policy
        self.reminded = set()

    def attach(self, scheduler):
        self.scheduler = scheduler
        return self

    def arm(self):
        """重启后补挂提醒。返回挂了几条。"""
        if self.scheduler is None or self.todos is None:
            return 0
        now = self.clock()
        count = 0
        for row in self.todos.scheduled():
            key = row.get("id")
            if key in self.reminded:
                continue
            due = self._parse(row.get("due_at"))
            if due is None:
                continue
            seconds = int((due - now).total_seconds())
            delay = seconds if seconds > 0 else self.catch_up
            if self._schedule(row, max(1, delay)):
                self.reminded.add(key)
                count += 1
        return count

    def pending_overdue(self):
        """过期还没提醒、也还没做完的最早一条。运行中新加的待办靠它兜底。"""
        if self.todos is None:
            return None
        now = self.clock()
        for row in self.todos.scheduled():
            key = row.get("id")
            if key in self.reminded:
                continue
            due = self._parse(row.get("due_at"))
            if due is None or due > now:
                continue
            return row
        return None

    def take_overdue(self):
        """取出并标记一条过期待办，供调度器规则使用。"""
        row = self.pending_overdue()
        if row is not None:
            self.reminded.add(row.get("id"))
        return row

    def mark(self, todo_id):
        self.reminded.add(todo_id)

    def schedule_todo(self, todo_id):
        """刚加的待办马上挂上到点提醒；没设时间或挂不上就返回 False。"""
        if self.scheduler is None or self.todos is None:
            return False
        for row in self.todos.scheduled():
            if row.get("id") != todo_id:
                continue
            due = self._parse(row.get("due_at"))
            if due is None:
                return False
            seconds = int((due - self.clock()).total_seconds())
            if self._schedule(row, max(1, seconds)):
                self.reminded.add(todo_id)
                return True
            return False
        return False

    # ---------- 内部 ----------
    def _schedule(self, row, seconds):
        try:
            text = "待办到时间了：%s" % row.get("title")
            if self.policy is not None:
                try:
                    text = self.policy.reminder_text(text)
                except Exception as exc:
                    self._warn("todo reminder persona text failed: %s", exc)
            result = self.scheduler.schedule_once(
                seconds,
                event=bus_module.COMPANION,
                name="待办提醒 #%s" % row.get("id"),
                idempotency_key="todo:%s:%s" % (row.get("id"), row.get("due_at") or ""),
                situation="用户设的待办到时间了，提醒他一句。",
                local_text=text,
                emotion="neutral",
                capability="todo",
                todo_id=row.get("id"),
            )
            return False if result is False else True
        except Exception as exc:
            self._warn("todo reminder schedule failed: %s", exc)
            return None

    @staticmethod
    def _parse(raw):
        text = parse_when(raw)
        if not text:
            return None
        try:
            return datetime.strptime(text, "%Y-%m-%d %H:%M")
        except ValueError:
            return None

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)


class CalendarReminder:
    """Schedule local calendar reminders through the persistent job store."""

    def __init__(
        self,
        calendar,
        scheduler=None,
        logger=None,
        clock=datetime.now,
        horizon_days=30,
        catch_up=15,
    ):
        self.calendar = calendar
        self.scheduler = scheduler
        self.logger = logger
        self.clock = clock
        self.horizon_days = max(1, int(horizon_days or 30))
        self.catch_up = max(1, int(catch_up or 15))
        self.reminded = set()

    def attach(self, scheduler):
        self.scheduler = scheduler
        return self

    def arm(self):
        if self.calendar is None or self.scheduler is None:
            return 0
        now = self.clock()
        count = 0
        try:
            rows = self.calendar.due_reminders(
                now=now, horizon_days=self.horizon_days
            )
        except Exception as exc:
            self._warn("calendar reminder read failed: %s", exc)
            return 0
        seen = set()
        for row in rows:
            key = self._key(row)
            if key in self.reminded or key in seen:
                continue
            seen.add(key)
            remind_at = datetime.fromtimestamp(float(row.get("remind_at_epoch") or 0))
            delay = int((remind_at - now).total_seconds())
            if self._schedule(row, max(1, delay if delay > 0 else self.catch_up)):
                self.reminded.add(key)
                count += 1
        return count

    def schedule_event(self, event_id):
        if self.calendar is None or self.scheduler is None:
            return False
        try:
            event = self.calendar.event(event_id)
        except Exception as exc:
            self._warn("calendar event read failed: %s", exc)
            return False
        if event is None or event.get("state") != "open":
            return False
        remind_at = event["start_dt"] - timedelta(minutes=event["remind_minutes"])
        delay = int((remind_at - self.clock()).total_seconds())
        row = dict(event)
        row["remind_at_epoch"] = remind_at.timestamp()
        key = self._key(row)
        if key in self.reminded:
            return True
        if self._schedule(row, max(1, delay if delay > 0 else self.catch_up)):
            self.reminded.add(key)
            return True
        return False

    def forget_event(self, event_id):
        prefix = "calendar:%s:" % event_id
        self.reminded = {
            item for item in self.reminded if not str(item).startswith(prefix)
        }
        return True

    @staticmethod
    def _key(row):
        return "calendar:%s:%s:%s" % (
            row.get("id"),
            row.get("when_at") or row.get("occurrence_at") or "",
            row.get("remind_minutes") or 0,
        )

    def _schedule(self, row, seconds):
        try:
            result = self.scheduler.schedule_once(
                seconds,
                event=bus_module.REMINDER,
                name="日程提醒：%s" % (row.get("title") or ""),
                idempotency_key=self._key(row),
                kind="calendar",
                event_id=row.get("id"),
                title=row.get("title") or "",
                when_at=row.get("when_at") or "",
                end_at=row.get("end_at") or "",
                note=row.get("note") or "",
                remind_minutes=row.get("remind_minutes") or 0,
                remind_at_epoch=row.get("remind_at_epoch") or 0,
                occurrence_key=row.get("occurrence_key") or "",
            )
            return False if result is False else True
        except Exception as exc:
            self._warn("calendar reminder schedule failed: %s", exc)
            return False

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)
