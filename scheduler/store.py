"""Persistent scheduled jobs and execution history.

The scheduler still dispatches events, but the job itself lives in SQLite.
That keeps timers and reminders across restarts without polling a model or
duplicating the same delivery.
"""

import json
import time
import uuid
from dataclasses import dataclass, field

PENDING = "pending"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
WAITING_CONFIRMATION = "waiting_confirmation"

STATE_LABELS = {
    PENDING: "待执行",
    RUNNING: "执行中",
    COMPLETED: "已完成",
    FAILED: "失败",
    CANCELLED: "已取消",
    WAITING_CONFIRMATION: "等待确认",
}


def _loads(raw):
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _dumps(value):
    try:
        return json.dumps(value or {}, ensure_ascii=False, default=str)
    except Exception:
        return "{}"


@dataclass
class ScheduledJob:
    id: str
    name: str = ""
    kind: str = "once"
    event: str = ""
    payload: dict = field(default_factory=dict)
    state: str = PENDING
    due_at: float = 0.0
    next_run_at: float = 0.0
    interval_seconds: int = 0
    attempts: int = 0
    max_attempts: int = 3
    retryable: bool = True
    idempotent: bool = True
    idempotency_key: str = ""
    result: str = ""
    error: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    last_run_at: float = 0.0
    cancelled_at: float = 0.0

    @property
    def state_label(self):
        return STATE_LABELS.get(self.state, self.state)

    @classmethod
    def from_row(cls, row):
        row = dict(row or {})
        return cls(
            id=str(row.get("id") or ""),
            name=str(row.get("name") or ""),
            kind=str(row.get("kind") or "once"),
            event=str(row.get("event") or ""),
            payload=_loads(row.get("payload")),
            state=str(row.get("state") or PENDING),
            due_at=float(row.get("due_at") or 0),
            next_run_at=float(row.get("next_run_at") or row.get("due_at") or 0),
            interval_seconds=int(row.get("interval_seconds") or 0),
            attempts=int(row.get("attempts") or 0),
            max_attempts=int(row.get("max_attempts") or 3),
            retryable=bool(row.get("retryable", 1)),
            idempotent=bool(row.get("idempotent", 1)),
            idempotency_key=str(row.get("idempotency_key") or ""),
            result=str(row.get("result") or ""),
            error=str(row.get("error") or ""),
            created_at=float(row.get("created_at") or 0),
            updated_at=float(row.get("updated_at") or 0),
            last_run_at=float(row.get("last_run_at") or 0),
            cancelled_at=float(row.get("cancelled_at") or 0),
        )

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "event": self.event,
            "state": self.state,
            "state_label": self.state_label,
            "due_at": self.due_at,
            "next_run_at": self.next_run_at,
            "interval_seconds": self.interval_seconds,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "retryable": self.retryable,
            "idempotent": self.idempotent,
            "idempotency_key": self.idempotency_key,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_run_at": self.last_run_at,
        }


class ScheduledJobStore:
    """SQLite-backed job repository. No business decisions live here."""

    def __init__(self, database, clock=time.time, logger=None):
        self.db = database
        self.clock = clock
        self.logger = logger

    def create(
        self,
        event,
        due_at,
        payload=None,
        *,
        name="",
        kind="once",
        interval_seconds=0,
        max_attempts=3,
        retryable=True,
        idempotent=True,
        idempotency_key="",
    ):
        now = float(self.clock())
        key = str(idempotency_key or "").strip()
        if key:
            existing = self.get_by_key(key)
            if existing is not None:
                return existing
        due = float(due_at)
        job = ScheduledJob(
            id="job-" + uuid.uuid4().hex[:12],
            name=str(name or "").strip(),
            kind=str(kind or "once"),
            event=str(event or ""),
            payload=dict(payload or {}),
            state=PENDING,
            due_at=due,
            next_run_at=due,
            interval_seconds=max(0, int(interval_seconds or 0)),
            max_attempts=max(1, int(max_attempts or 3)),
            retryable=bool(retryable),
            idempotent=bool(idempotent),
            idempotency_key=key,
            created_at=now,
            updated_at=now,
        )
        if not job.event:
            raise ValueError("计划任务必须有事件名")
        try:
            self.db.execute(
                "INSERT INTO scheduled_jobs"
                " (id, name, kind, event, payload, state, due_at, next_run_at,"
                " interval_seconds, attempts, max_attempts, retryable, idempotent,"
                " idempotency_key, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)",
                (
                    job.id,
                    job.name,
                    job.kind,
                    job.event,
                    _dumps(job.payload),
                    job.state,
                    job.due_at,
                    job.next_run_at,
                    job.interval_seconds,
                    job.max_attempts,
                    int(job.retryable),
                    int(job.idempotent),
                    job.idempotency_key,
                    job.created_at,
                    job.updated_at,
                ),
            )
        except Exception:
            if key:
                existing = self.get_by_key(key)
                if existing is not None:
                    return existing
            raise
        return job

    def get(self, job_id):
        row = self.db.query_one(
            "SELECT * FROM scheduled_jobs WHERE id = ?", (str(job_id or ""),)
        )
        return ScheduledJob.from_row(row) if row else None

    def get_by_key(self, key):
        key = str(key or "").strip()
        if not key:
            return None
        row = self.db.query_one(
            "SELECT * FROM scheduled_jobs WHERE idempotency_key = ?", (key,)
        )
        return ScheduledJob.from_row(row) if row else None

    def due(self, now=None, limit=50):
        moment = float(self.clock() if now is None else now)
        limit = max(1, min(200, int(limit or 50)))
        rows = self.db.query(
            "SELECT * FROM scheduled_jobs"
            " WHERE state = ? AND COALESCE(next_run_at, due_at) <= ?"
            " ORDER BY COALESCE(next_run_at, due_at) ASC, created_at ASC LIMIT ?",
            (PENDING, moment, limit),
        )
        return [ScheduledJob.from_row(row) for row in rows]

    def next_due(self, now=None):
        moment = float(self.clock() if now is None else now)
        row = self.db.query_one(
            "SELECT MIN(COALESCE(next_run_at, due_at)) AS next_at"
            " FROM scheduled_jobs WHERE state = ?",
            (PENDING,),
        )
        value = row.get("next_at") if row else None
        return float(value) if value is not None else None

    def claim(self, job_id, now=None):
        moment = float(self.clock() if now is None else now)
        changed = self.db.execute(
            "UPDATE scheduled_jobs"
            " SET state = ?, attempts = attempts + 1, last_run_at = ?, updated_at = ?"
            " WHERE id = ? AND state = ?",
            (RUNNING, moment, moment, str(job_id or ""), PENDING),
        )
        return bool(changed)

    def finish(self, job, ok, result="", error="", now=None):
        moment = float(self.clock() if now is None else now)
        attempt = int(job.attempts or 0)
        next_state = COMPLETED if ok else FAILED
        next_run = None
        attempts = attempt
        text = str(result or "")[:1000]
        failure = str(error or "")[:1000]

        if ok and job.kind == "interval" and job.interval_seconds > 0:
            next_state = PENDING
            next_run = moment + job.interval_seconds
            attempts = 0
        elif (
            not ok
            and job.retryable
            and job.idempotent
            and attempt < job.max_attempts
        ):
            next_state = PENDING
            next_run = moment + min(300, 2 ** max(1, attempt))

        self.db.execute(
            "UPDATE scheduled_jobs SET state = ?, next_run_at = ?, attempts = ?,"
            " result = ?, error = ?, updated_at = ? WHERE id = ?",
            (next_state, next_run, attempts, text, failure, moment, job.id),
        )
        self.record_run(
            job.id,
            next_state,
            attempt=attempt,
            started_at=float(job.last_run_at or moment),
            finished_at=moment,
            result=text,
            error=failure,
        )
        return next_state

    def cancel(self, job_id, now=None):
        moment = float(self.clock() if now is None else now)
        return bool(
            self.db.execute(
                "UPDATE scheduled_jobs SET state = ?, next_run_at = NULL,"
                " cancelled_at = ?, updated_at = ?"
                " WHERE id = ? AND state IN (?, ?)",
                (CANCELLED, moment, moment, str(job_id or ""), PENDING, WAITING_CONFIRMATION),
            )
        )

    def cancel_prefix(self, prefix, now=None):
        moment = float(self.clock() if now is None else now)
        key = str(prefix or "")
        if not key:
            return 0
        return int(
            self.db.execute(
                "UPDATE scheduled_jobs SET state = ?, next_run_at = NULL,"
                " cancelled_at = ?, updated_at = ?"
                " WHERE state = ? AND idempotency_key LIKE ?",
                (CANCELLED, moment, moment, PENDING, key + "%"),
            )
        )

    def resume(self, job_id, now=None):
        """User explicitly confirms a failed/interrupted job may run again."""
        moment = float(self.clock() if now is None else now)
        return bool(
            self.db.execute(
                "UPDATE scheduled_jobs SET state = ?, next_run_at = ?,"
                " error = '', updated_at = ?"
                " WHERE id = ? AND state IN (?, ?)",
                (
                    PENDING,
                    moment,
                    moment,
                    str(job_id or ""),
                    WAITING_CONFIRMATION,
                    FAILED,
                ),
            )
        )

    def recover_interrupted(self, now=None):
        """A crash while running is retried only for idempotent jobs."""
        moment = float(self.clock() if now is None else now)
        rows = self.db.query(
            "SELECT * FROM scheduled_jobs WHERE state = ?", (RUNNING,)
        )
        recovered = []
        for row in rows:
            job = ScheduledJob.from_row(row)
            if job.idempotent:
                state = PENDING
                next_run = moment
                error = "上次执行中断，已安全重新排队"
            else:
                state = WAITING_CONFIRMATION
                next_run = None
                error = "上次执行中断，非幂等任务不会自动重跑"
            self.db.execute(
                "UPDATE scheduled_jobs SET state = ?, next_run_at = ?, error = ?,"
                " updated_at = ? WHERE id = ?",
                (state, next_run, error, moment, job.id),
            )
            self.record_run(
                job.id,
                state,
                attempt=job.attempts,
                started_at=job.last_run_at or job.created_at,
                finished_at=moment,
                error=error,
            )
            recovered.append(job.id)
        return recovered

    def record_run(
        self,
        job_id,
        state,
        *,
        attempt=0,
        started_at=0,
        finished_at=0,
        result="",
        error="",
    ):
        try:
            return self.db.insert(
                "INSERT INTO scheduled_job_runs"
                " (job_id, state, attempt, started_at, finished_at, result, error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(job_id or ""),
                    str(state or ""),
                    int(attempt or 0),
                    float(started_at or self.clock()),
                    float(finished_at or self.clock()),
                    str(result or "")[:1000],
                    str(error or "")[:1000],
                ),
            )
        except Exception as exc:
            self._warn("scheduled run record failed: %s", exc)
            return None

    def recent_jobs(self, limit=50):
        rows = self.db.query(
            "SELECT * FROM scheduled_jobs ORDER BY updated_at DESC LIMIT ?",
            (max(1, min(500, int(limit or 50))),),
        )
        return [ScheduledJob.from_row(row) for row in rows]

    def recent_runs(self, job_id=None, limit=50):
        if job_id:
            rows = self.db.query(
                "SELECT * FROM scheduled_job_runs WHERE job_id = ?"
                " ORDER BY id DESC LIMIT ?",
                (str(job_id), max(1, min(500, int(limit or 50)))),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM scheduled_job_runs ORDER BY id DESC LIMIT ?",
                (max(1, min(500, int(limit or 50))),),
            )
        return rows

    def stats(self):
        rows = self.db.query(
            "SELECT state, COUNT(*) AS n FROM scheduled_jobs GROUP BY state"
        )
        counts = {str(row["state"]): int(row["n"] or 0) for row in rows}
        return {
            "total": sum(counts.values()),
            "pending": counts.get(PENDING, 0),
            "running": counts.get(RUNNING, 0),
            "completed": counts.get(COMPLETED, 0),
            "failed": counts.get(FAILED, 0),
            "cancelled": counts.get(CANCELLED, 0),
            "waiting_confirmation": counts.get(WAITING_CONFIRMATION, 0),
        }

    def _warn(self, message, *args):
        if self.logger:
            try:
                self.logger.warning(message, *args)
            except Exception:
                pass
