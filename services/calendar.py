"""Local calendar service backed by SQLite.

The service supports start/end times, priority, simple recurrence, reminder
lead time, conflict checks and free-slot calculation. It never calls a model.
"""

import calendar as calendar_module
import re
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ACCEPTED_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
)

_MONTH_DAY = re.compile(r"^\d{1,2}-\d{1,2}( \d{1,2}:\d{2})?$")
_TIME_ONLY = re.compile(r"^\d{1,2}:\d{2}$")
REPEAT_RULES = ("", "daily", "weekly", "monthly")
REPEAT_LABELS = {
    "": "不重复",
    "daily": "每天",
    "weekly": "每周",
    "monthly": "每月",
}
STATE_LABELS = {"open": "待办", "done": "已完成"}
_UNSET = object()


def parse_when(raw, now=None):
    """Parse a local date/time string. Return None instead of guessing."""
    text = " ".join(str(raw or "").split())
    if not text:
        return None
    reference = now or datetime.now()
    if _MONTH_DAY.match(text):
        text = f"{reference.year}-{text}"
    elif _TIME_ONLY.match(text):
        text = f"{reference:%Y-%m-%d} {text}"
    for pattern in ACCEPTED_FORMATS:
        try:
            parsed = datetime.strptime(text, pattern)
        except ValueError:
            continue
        if pattern == "%Y-%m-%d":
            parsed = parsed.replace(hour=9)
        return parsed.strftime("%Y-%m-%d %H:%M")
    return None


def _as_dt(raw):
    text = parse_when(raw)
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def _stamp(value):
    return value.strftime("%Y-%m-%d %H:%M") if isinstance(value, datetime) else ""


def _normalize_repeat(value):
    wanted = str(value or "").strip().lower()
    return wanted if wanted in REPEAT_RULES else ""


def _add_month(value):
    year = value.year + (1 if value.month == 12 else 0)
    month = 1 if value.month == 12 else value.month + 1
    day = min(value.day, calendar_module.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _ics_escape(value):
    text = str(value or "")
    return (
        text.replace("\\", "\\\\")
        .replace("\r\n", "\\n")
        .replace("\r", "\\n")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def _ics_unescape(value):
    text = str(value or "")
    return (
        text.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def _ics_fold(line):
    """Fold a content line at 75 UTF-8 bytes, never through a character."""
    text = str(line or "")
    if len(text.encode("utf-8")) <= 75:
        return text
    chunks = []
    current = ""
    current_size = 0
    for char in text:
        size = len(char.encode("utf-8"))
        limit = 75 if not chunks else 74
        if current_size + size > limit:
            chunks.append(current)
            current = " " + char
            current_size = 1 + size
        else:
            current += char
            current_size += size
    chunks.append(current)
    return "\r\n".join(chunks)


def _ics_datetime(value):
    return value.strftime("%Y%m%dT%H%M%S") if isinstance(value, datetime) else ""


def _parse_ics_datetime(raw, params=None):
    text = str(raw or "").strip()
    if not text:
        return None
    utc = text.endswith("Z")
    if utc:
        text = text[:-1]
    parsed = None
    for pattern in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M", "%Y%m%d"):
        try:
            parsed = datetime.strptime(text, pattern)
        except ValueError:
            continue
        if pattern == "%Y%m%d":
            parsed = parsed.replace(hour=9)
        break
    if parsed is None:
        return None
    if utc:
        parsed = parsed.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    else:
        tzid = str((params or {}).get("TZID") or "").strip()
        if tzid:
            try:
                parsed = (
                    parsed.replace(tzinfo=ZoneInfo(tzid))
                    .astimezone()
                    .replace(tzinfo=None)
                )
            except (ZoneInfoNotFoundError, ValueError):
                return None
    return parsed


def _parse_trigger(raw):
    text = str(raw or "").strip().upper()
    match = re.match(r"^-P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$", text)
    if not match:
        return None
    days = int(match.group(1) or 0)
    hours = int(match.group(2) or 0)
    minutes = int(match.group(3) or 0)
    return max(0, min(1440, days * 1440 + hours * 60 + minutes))


def _unfold_ics(text):
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\n[ \t]", "", normalized).split("\n")


def _ics_property(line):
    if ":" not in line:
        return "", {}, ""
    head, value = line.split(":", 1)
    pieces = head.split(";")
    params = {}
    for piece in pieces[1:]:
        if "=" in piece:
            key, item = piece.split("=", 1)
            params[key.strip().upper()] = item.strip().strip('"')
    return pieces[0].strip().upper(), params, value


class CalendarService:
    """Local calendar. All writes go through explicit local methods."""

    def __init__(self, database, clock=None):
        self.db = database
        self.clock = clock or datetime.now
        self.on_change = None

    def _now(self):
        value = self.clock()
        return value if isinstance(value, datetime) else datetime.now()

    def _row(self, value):
        if not value:
            return None
        row = dict(value)
        start = _as_dt(row.get("when_at"))
        if start is None:
            return None
        end = _as_dt(row.get("end_at")) if row.get("end_at") else start + timedelta(minutes=30)
        if end is None or end <= start:
            end = start + timedelta(minutes=30)
        row["start_dt"] = start
        row["end_dt"] = end
        row["when_at"] = _stamp(start)
        row["end_at"] = _stamp(end)
        row["priority"] = int(row.get("priority") or 1)
        row["repeat_rule"] = _normalize_repeat(row.get("repeat_rule"))
        row["repeat_label"] = REPEAT_LABELS.get(row["repeat_rule"], "不重复")
        row["remind_minutes"] = max(0, min(1440, int(row.get("remind_minutes") or 0)))
        row["state"] = str(row.get("state") or "open")
        row["state_label"] = STATE_LABELS.get(row["state"], row["state"])
        return row

    def _end_for(self, start, end_raw):
        text = " ".join(str(end_raw or "").split())
        if not text:
            return start + timedelta(minutes=30), ""
        if _TIME_ONLY.match(text):
            text = "%s %s" % (start.strftime("%Y-%m-%d"), text)
        end = _as_dt(text)
        if end is None:
            return None, "结束时间格式无法识别"
        if end <= start:
            return None, "结束时间必须晚于开始时间"
        return end, ""

    def _overlap_conflicts(self, start, end, exclude_id=0):
        rows = self.events_between(
            (start - timedelta(days=1)).strftime("%Y-%m-%d %H:%M"),
            (end + timedelta(days=1)).strftime("%Y-%m-%d %H:%M"),
            include_done=False,
        )["data"]["events"]
        found = []
        for row in rows:
            if int(row.get("id") or 0) == int(exclude_id or 0):
                continue
            if row["start_dt"] < end and row["end_dt"] > start:
                found.append(row)
        return found

    def _notify(self, action, event_id):
        callback = getattr(self, "on_change", None)
        if not callable(callback):
            return None
        try:
            return callback(str(action), int(event_id))
        except Exception:
            return None

    # ---------- write ----------
    def add_event(
        self,
        title,
        when,
        note="",
        end="",
        priority=1,
        repeat_rule="",
        remind_minutes=10,
        todo_id=0,
    ):
        title = " ".join(str(title or "").split())
        if not title:
            return {"ok": False, "error": "日程需要标题"}
        start = _as_dt(when)
        if start is None:
            return {
                "ok": False,
                "error": "时间格式无法识别，请用 2026-10-07 09:00 这样的写法",
            }
        end_at, problem = self._end_for(start, end)
        if problem:
            return {"ok": False, "error": problem}
        try:
            priority = max(0, min(9, int(priority or 0)))
        except (TypeError, ValueError):
            priority = 1
        try:
            remind = max(0, min(1440, int(remind_minutes or 0)))
        except (TypeError, ValueError):
            remind = 10
        repeat = _normalize_repeat(repeat_rule)
        now = _stamp(self._now())
        event_id = self.db.insert(
            "INSERT INTO calendar_events"
            " (title, when_at, note, end_at, priority, repeat_rule, remind_minutes,"
            " state, todo_id, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
            (
                title,
                _stamp(start),
                str(note or "").strip(),
                _stamp(end_at),
                priority,
                repeat,
                remind,
                int(todo_id or 0),
                now,
            ),
        )
        conflicts = self._overlap_conflicts(start, end_at, exclude_id=event_id)
        self._notify("add", event_id)
        return {
            "ok": True,
            "data": {
                "id": event_id,
                "title": title,
                "when": _stamp(start),
                "end": _stamp(end_at),
                "conflicts": conflicts,
            },
        }

    def update_event(self, event_id, **fields):
        row = self.event(event_id)
        if row is None:
            return {"ok": False, "error": "没有这条日程"}
        title = " ".join(str(fields.get("title", row["title"]) or "").split())
        if not title:
            return {"ok": False, "error": "日程需要标题"}
        start_raw = fields.get("when", row["when_at"])
        start = _as_dt(start_raw)
        if start is None:
            return {"ok": False, "error": "开始时间格式无法识别"}
        end_raw = fields.get("end", row["end_at"])
        end_at, problem = self._end_for(start, end_raw)
        if problem:
            return {"ok": False, "error": problem}
        note = str(fields.get("note", row.get("note") or "")).strip()
        try:
            priority = max(0, min(9, int(fields.get("priority", row["priority"]))))
        except (TypeError, ValueError):
            return {"ok": False, "error": "优先级要是 0 到 9"}
        repeat = _normalize_repeat(fields.get("repeat_rule", row["repeat_rule"]))
        try:
            remind = max(
                0, min(1440, int(fields.get("remind_minutes", row["remind_minutes"])))
            )
        except (TypeError, ValueError):
            return {"ok": False, "error": "提醒分钟数要是 0 到 1440"}
        todo_id = int(fields.get("todo_id", row.get("todo_id") or 0) or 0)
        self.db.execute(
            "UPDATE calendar_events SET title=?, when_at=?, end_at=?, note=?,"
            " priority=?, repeat_rule=?, remind_minutes=?, todo_id=?, updated_at=?"
            " WHERE id=?",
            (
                title,
                _stamp(start),
                _stamp(end_at),
                note,
                priority,
                repeat,
                remind,
                todo_id,
                _stamp(self._now()),
                int(event_id),
            ),
        )
        conflicts = self._overlap_conflicts(start, end_at, exclude_id=event_id)
        self._notify("update", event_id)
        return {
            "ok": True,
            "data": {
                "id": int(event_id),
                "title": title,
                "when": _stamp(start),
                "end": _stamp(end_at),
                "conflicts": conflicts,
            },
        }

    def complete_event(self, event_id):
        try:
            key = int(event_id)
        except (TypeError, ValueError):
            return {"ok": False, "error": "日程编号不对"}
        row = self.event(key)
        if row is None:
            return {"ok": False, "error": "没有这条日程"}
        self.db.execute(
            "UPDATE calendar_events SET state='done', updated_at=? WHERE id=?",
            (_stamp(self._now()), key),
        )
        if int(row.get("todo_id") or 0):
            try:
                self.db.execute(
                    "UPDATE todos SET state='done', done_at=?, updated_at=? WHERE id=?",
                    (_stamp(self._now()), _stamp(self._now()), int(row["todo_id"])),
                )
            except Exception:
                pass
        self._notify("complete", key)
        return {"ok": True, "data": {"id": key, "title": row["title"]}}

    def delay_event(self, event_id, minutes=15):
        row = self.event(event_id)
        if row is None:
            return {"ok": False, "error": "没有这条日程"}
        try:
            delta = timedelta(minutes=max(1, int(minutes or 15)))
        except (TypeError, ValueError):
            delta = timedelta(minutes=15)
        start = row["start_dt"] + delta
        end = row["end_dt"] + delta
        return self.update_event(
            event_id, when=_stamp(start), end=_stamp(end), title=row["title"]
        )

    def cancel_event(self, event_id):
        try:
            key = int(event_id)
        except (TypeError, ValueError):
            return {"ok": False, "error": "日程编号不对"}
        row = self.db.query_one("SELECT title FROM calendar_events WHERE id = ?", (key,))
        if not row:
            return {"ok": False, "error": "没有编号 %s 的日程" % key}
        self.db.execute("DELETE FROM calendar_events WHERE id = ?", (key,))
        self._notify("delete", key)
        return {"ok": True, "data": {"id": key, "title": row["title"]}}

    # ---------- read ----------
    def event(self, event_id):
        try:
            key = int(event_id)
        except (TypeError, ValueError):
            return None
        return self._row(
            self.db.query_one("SELECT * FROM calendar_events WHERE id = ?", (key,))
        )

    def list_events(self, limit=10):
        try:
            limit = max(1, min(50, int(limit or 10)))
        except (TypeError, ValueError):
            limit = 10
        rows = self.db.query(
            "SELECT * FROM calendar_events WHERE state != 'cancelled'"
            " ORDER BY when_at ASC, id ASC LIMIT ?",
            (limit,),
        )
        events = [self._row(row) for row in rows]
        events = [event for event in events if event is not None]
        return {"ok": True, "data": {"count": len(events), "events": events}}

    def _occurrences(self, base, range_start, range_end, limit=500):
        repeat = base["repeat_rule"]
        start = base["start_dt"]
        duration = base["end_dt"] - start
        out = []
        if repeat not in ("daily", "weekly", "monthly"):
            if start < range_end and base["end_dt"] > range_start:
                out.append(base)
            return out
        current = start
        step_days = 1 if repeat == "daily" else 7 if repeat == "weekly" else 0
        guard = 0
        while current < range_end and guard < limit:
            guard += 1
            if current < range_end and current + duration > range_start:
                item = dict(base)
                item["occurrence_at"] = _stamp(current)
                item["occurrence_key"] = "%s:%s" % (base["id"], _stamp(current))
                item["start_dt"] = current
                item["end_dt"] = current + duration
                item["when_at"] = _stamp(current)
                item["end_at"] = _stamp(current + duration)
                out.append(item)
            if repeat == "monthly":
                current = _add_month(current)
            else:
                current += timedelta(days=step_days)
            if current <= start and guard > 1:
                break
        return out

    def events_between(self, start, end, limit=500, include_done=True):
        try:
            limit = max(1, min(2000, int(limit or 500)))
        except (TypeError, ValueError):
            limit = 500
        range_start = _as_dt(start)
        range_end = _as_dt(end)
        if range_start is None or range_end is None or range_end <= range_start:
            return {"ok": False, "error": "时间段无效", "data": {"count": 0, "events": []}}
        state_clause = "" if include_done else " AND state = 'open'"
        rows = self.db.query(
            "SELECT * FROM calendar_events WHERE state != 'cancelled'"
            + state_clause
            + " AND (when_at < ? OR repeat_rule != '')"
            " ORDER BY when_at ASC, id ASC",
            (_stamp(range_end),),
        )
        events = []
        for row in rows:
            base = self._row(row)
            if base is None:
                continue
            events.extend(self._occurrences(base, range_start, range_end, limit=limit))
            if len(events) >= limit:
                break
        events.sort(key=lambda item: (item["start_dt"], -item["priority"], item["id"]))
        events = events[:limit]
        return {"ok": True, "data": {"count": len(events), "events": events}}

    def conflicts(self, start, end="", exclude_id=0, limit=20):
        start_dt = _as_dt(start)
        if start_dt is None:
            return {"ok": False, "error": "开始时间格式无法识别"}
        end_dt, problem = self._end_for(start_dt, end)
        if problem:
            return {"ok": False, "error": problem}
        rows = self._overlap_conflicts(start_dt, end_dt, exclude_id=exclude_id)
        return {
            "ok": True,
            "data": {
                "count": len(rows),
                "events": rows[: max(1, int(limit or 20))],
            },
        }

    def free_slots(
        self,
        day,
        work_start="09:00",
        work_end="18:00",
        min_minutes=30,
        include_done=False,
    ):
        if isinstance(day, date):
            day_value = day
        else:
            parsed = _as_dt(str(day) + " 00:00" if len(str(day)) == 10 else day)
            if parsed is None:
                return {"ok": False, "error": "日期格式无法识别"}
            day_value = parsed.date()
        try:
            start_time = datetime.strptime(str(work_start), "%H:%M").time()
            end_time = datetime.strptime(str(work_end), "%H:%M").time()
        except ValueError:
            return {"ok": False, "error": "工作时间格式要用 HH:MM"}
        work_start_dt = datetime.combine(day_value, start_time)
        work_end_dt = datetime.combine(day_value, end_time)
        if work_end_dt <= work_start_dt:
            return {"ok": False, "error": "下班时间必须晚于上班时间"}
        rows = self.events_between(
            _stamp(work_start_dt), _stamp(work_end_dt), include_done=include_done
        )["data"]["events"]
        busy = []
        for row in rows:
            start_dt = max(work_start_dt, row["start_dt"])
            end_dt = min(work_end_dt, row["end_dt"])
            if end_dt > start_dt:
                busy.append((start_dt, end_dt, row))
        busy.sort(key=lambda item: item[0])
        merged = []
        for start_dt, end_dt, _row in busy:
            if not merged or start_dt > merged[-1][1]:
                merged.append([start_dt, end_dt])
            else:
                merged[-1][1] = max(merged[-1][1], end_dt)
        minimum = max(1, int(min_minutes or 1))
        cursor = work_start_dt
        slots = []
        for start_dt, end_dt in merged:
            if start_dt > cursor:
                minutes = int((start_dt - cursor).total_seconds() // 60)
                if minutes >= minimum:
                    slots.append(
                        {
                            "start": _stamp(cursor),
                            "end": _stamp(start_dt),
                            "minutes": minutes,
                        }
                    )
            cursor = max(cursor, end_dt)
        if work_end_dt > cursor:
            minutes = int((work_end_dt - cursor).total_seconds() // 60)
            if minutes >= minimum:
                slots.append(
                    {
                        "start": _stamp(cursor),
                        "end": _stamp(work_end_dt),
                        "minutes": minutes,
                    }
                )
        return {"ok": True, "data": {"day": day_value.isoformat(), "slots": slots}}

    def due_reminders(self, now=None, horizon_days=30, grace_minutes=60):
        moment = now if isinstance(now, datetime) else self._now()
        end = moment + timedelta(days=max(1, int(horizon_days or 30)))
        events = self.events_between(
            _stamp(moment - timedelta(minutes=max(0, int(grace_minutes or 0)))),
            _stamp(end),
            limit=1000,
            include_done=False,
        )["data"]["events"]
        rows = []
        for event in events:
            remind_at = event["start_dt"] - timedelta(minutes=event["remind_minutes"])
            if remind_at > end:
                continue
            if event["start_dt"] < moment - timedelta(minutes=max(1, int(grace_minutes or 1))):
                continue
            item = dict(event)
            item["remind_at"] = _stamp(remind_at)
            item["remind_at_epoch"] = remind_at.timestamp()
            rows.append(item)
        return rows

    # ---------- iCalendar ----------
    def export_ics(self, start, end):
        """Export the events that overlap a range as one standard ICS file."""
        range_start = _as_dt(start)
        range_end = _as_dt(end)
        if range_start is None or range_end is None or range_end <= range_start:
            return {"ok": False, "error": "导出时间段无效"}
        rows = self.db.query(
            "SELECT * FROM calendar_events WHERE state != 'cancelled'"
            " AND when_at < ? AND (end_at > ? OR repeat_rule != '')"
            " ORDER BY when_at ASC, id ASC",
            (_stamp(range_end), _stamp(range_start)),
        )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Blossom//Local Calendar//CN",
            "CALSCALE:GREGORIAN",
        ]
        count = 0
        for raw in rows:
            event = self._row(raw)
            if event is None:
                continue
            count += 1
            lines.extend(
                [
                    "BEGIN:VEVENT",
                    "UID:blossom-%s@local" % event["id"],
                    "DTSTAMP:" + stamp,
                    "DTSTART:" + _ics_datetime(event["start_dt"]),
                    "DTEND:" + _ics_datetime(event["end_dt"]),
                    "SUMMARY:" + _ics_escape(event["title"]),
                    "PRIORITY:%d" % event["priority"],
                    "STATUS:"
                    + ("COMPLETED" if event["state"] == "done" else "CONFIRMED"),
                ]
            )
            if event.get("note"):
                lines.append("DESCRIPTION:" + _ics_escape(event["note"]))
            if event["repeat_rule"]:
                lines.append(
                    "RRULE:FREQ=%s" % event["repeat_rule"].upper()
                )
            if event["remind_minutes"]:
                lines.extend(
                    [
                        "BEGIN:VALARM",
                        "TRIGGER:-PT%dM" % event["remind_minutes"],
                        "ACTION:DISPLAY",
                        "DESCRIPTION:" + _ics_escape(event["title"]),
                        "END:VALARM",
                    ]
                )
            lines.append("END:VEVENT")
        lines.append("END:VCALENDAR")
        body = "\r\n".join(_ics_fold(line) for line in lines) + "\r\n"
        return {"ok": True, "data": {"count": count, "text": body}}

    def import_ics(self, text, dry_run=False, skip_duplicates=True):
        """Import VEVENT blocks. The caller confirms before setting dry_run=False."""
        lines = _unfold_ics(text)
        parsed = []
        warnings = []
        current = None
        for line in lines:
            stripped = line.strip()
            upper = stripped.upper()
            if upper == "BEGIN:VEVENT":
                current = {}
                continue
            if upper == "END:VEVENT":
                if current is not None:
                    parsed.append(current)
                current = None
                continue
            if current is None or not stripped:
                continue
            name, params, value = _ics_property(stripped)
            if not name:
                continue
            if name == "DTSTART":
                current["start"] = _parse_ics_datetime(value, params)
                if current["start"] is None and params.get("TZID"):
                    current["timezone_error"] = params["TZID"]
            elif name == "DTEND":
                current["end"] = _parse_ics_datetime(value, params)
            elif name == "SUMMARY":
                current["title"] = _ics_unescape(value)
            elif name == "DESCRIPTION":
                current["note"] = _ics_unescape(value)
            elif name == "PRIORITY":
                try:
                    current["priority"] = max(0, min(9, int(value)))
                except ValueError:
                    pass
            elif name == "RRULE":
                current["rrule"] = value
            elif name == "TRIGGER":
                current["trigger"] = value
            elif name == "STATUS":
                current["status"] = value.upper()
        if not parsed:
            return {
                "ok": False,
                "error": "没有找到可导入的 VEVENT",
                "data": {"parsed": 0, "would_create": 0, "skipped": 0, "warnings": []},
            }

        prepared = []
        for item in parsed:
            start = item.get("start")
            if start is None:
                if item.get("timezone_error"):
                    warnings.append(
                        "跳过一条时区无法识别的日程：%s" % item["timezone_error"]
                    )
                else:
                    warnings.append("跳过一条没有可识别开始时间的日程")
                continue
            end = item.get("end")
            if end is not None and end <= start:
                end = start + timedelta(minutes=30)
                warnings.append("结束时间早于开始时间，按 30 分钟处理")
            repeat = ""
            rule = str(item.get("rrule") or "")
            if rule:
                values = {
                    part.split("=", 1)[0].upper(): part.split("=", 1)[1]
                    for part in rule.split(";")
                    if "=" in part
                }
                frequency = str(values.get("FREQ") or "").upper()
                repeat = {"DAILY": "daily", "WEEKLY": "weekly", "MONTHLY": "monthly"}.get(
                    frequency, ""
                )
                if not repeat:
                    warnings.append("跳过不支持的重复规则：%s" % rule)
                elif values.get("INTERVAL") not in (None, "", "1"):
                    warnings.append("重复间隔不是 1，已按普通日程导入")
                    repeat = ""
                if values.get("UNTIL") or values.get("COUNT"):
                    warnings.append("重复日程的结束条件未导入，已按不重复日程处理")
                    repeat = ""
            remind = _parse_trigger(item.get("trigger")) or 0
            prepared.append(
                {
                    "title": str(item.get("title") or "未命名日程").strip() or "未命名日程",
                    "start": start,
                    "end": end or start + timedelta(minutes=30),
                    "note": str(item.get("note") or "").strip(),
                    "priority": int(item.get("priority", 1) or 0),
                    "repeat_rule": repeat,
                    "remind_minutes": remind,
                    "state": "done" if item.get("status") == "COMPLETED" else "open",
                }
            )

        skipped = 0
        created = 0
        failed = 0
        for item in prepared:
            if skip_duplicates:
                row = self.db.query_one(
                    "SELECT id FROM calendar_events WHERE state != 'cancelled'"
                    " AND title=? AND when_at=? AND end_at=? LIMIT 1",
                    (
                        item["title"],
                        _stamp(item["start"]),
                        _stamp(item["end"]),
                    ),
                )
                if row is not None:
                    skipped += 1
                    continue
            if dry_run:
                continue
            result = self.add_event(
                title=item["title"],
                when=_stamp(item["start"]),
                note=item["note"],
                end=_stamp(item["end"]),
                priority=item["priority"],
                repeat_rule=item["repeat_rule"],
                remind_minutes=item["remind_minutes"],
            )
            if not result.get("ok"):
                warnings.append(
                    "%s：%s" % (item["title"], result.get("error") or "写入失败")
                )
                failed += 1
                continue
            if item["state"] == "done":
                self.complete_event(result["data"]["id"])
            created += 1
        would_create = len(prepared) - skipped if dry_run else created
        return {
            "ok": True,
            "data": {
                "parsed": len(parsed),
                "would_create": would_create,
                "created": created,
                "skipped": skipped,
                "failed": failed,
                "warnings": warnings,
                "preview": [
                    {
                        "title": item["title"],
                        "when_at": _stamp(item["start"]),
                        "end_at": _stamp(item["end"]),
                    }
                    for item in prepared[:20]
                ],
            },
        }
