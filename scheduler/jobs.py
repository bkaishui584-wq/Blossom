"""Runs persistent scheduled jobs on the existing EventBus."""

import json
import time

from events.bus import Event


class PersistentJobs:
    """Dispatch due jobs once, record real results, and retry safely."""

    def __init__(self, store, bus, logger=None, clock=time.time):
        self.store = store
        self.bus = bus
        self.logger = logger
        self.clock = clock

    def schedule_once(
        self,
        seconds,
        event,
        payload=None,
        *,
        name="",
        idempotency_key="",
        max_attempts=3,
        retryable=True,
        idempotent=True,
    ):
        due = float(self.clock()) + max(1, float(seconds))
        return self.store.create(
            event,
            due,
            payload,
            name=name,
            kind="once",
            max_attempts=max_attempts,
            retryable=retryable,
            idempotent=idempotent,
            idempotency_key=idempotency_key,
        )

    def schedule_at(
        self,
        due_at,
        event,
        payload=None,
        *,
        name="",
        idempotency_key="",
        max_attempts=3,
        retryable=True,
        idempotent=True,
    ):
        return self.store.create(
            event,
            float(due_at),
            payload,
            name=name,
            kind="once",
            max_attempts=max_attempts,
            retryable=retryable,
            idempotent=idempotent,
            idempotency_key=idempotency_key,
        )

    def schedule_interval(
        self,
        every_seconds,
        event,
        payload=None,
        *,
        name="",
        first_delay=None,
        idempotency_key="",
        max_attempts=3,
        retryable=True,
        idempotent=True,
    ):
        every = max(1, int(float(every_seconds)))
        delay = every if first_delay is None else max(1, float(first_delay))
        due = float(self.clock()) + delay
        return self.store.create(
            event,
            due,
            payload,
            name=name,
            kind="interval",
            interval_seconds=every,
            max_attempts=max_attempts,
            retryable=retryable,
            idempotent=idempotent,
            idempotency_key=idempotency_key,
        )

    def cancel(self, job_id):
        return self.store.cancel(job_id, now=self.clock())

    def cancel_prefix(self, prefix):
        return self.store.cancel_prefix(prefix, now=self.clock())

    def resume(self, job_id):
        ok = self.store.resume(job_id, now=self.clock())
        if ok:
            self.store.record_run(job_id, "pending", result="用户确认重新排队")
        return ok

    def run_due(self, now=None):
        moment = float(self.clock() if now is None else now)
        summaries = []
        for listed in self.store.due(moment):
            if not self.store.claim(listed.id, now=moment):
                continue
            current = self.store.get(listed.id)
            if current is None:
                continue
            # claim() already incremented the row; mirror that on this snapshot.
            current.attempts = int(listed.attempts or 0) + 1
            current.last_run_at = moment
            payload = dict(current.payload or {})
            payload.setdefault("job_id", current.id)
            payload.setdefault("job_name", current.name)
            event = Event(name=current.event, payload=payload, created_at=moment)
            try:
                results = self.bus.publish(event)
                errors = [
                    str(item.get("error"))
                    for item in (results or [])
                    if isinstance(item, dict) and item.get("error")
                ]
                ok = not errors
                result_text = json.dumps(
                    {
                        "event": current.event,
                        "handlers": len(results or []),
                        "delivered": ok,
                        "job_name": current.name,
                    },
                    ensure_ascii=False,
                )
                error_text = "；".join(errors)
            except Exception as exc:
                ok = False
                result_text = ""
                error_text = "事件派发失败：%s" % exc
            state = self.store.finish(
                current,
                ok,
                result=result_text,
                error=error_text,
                now=moment,
            )
            summaries.append(
                {
                    "id": current.id,
                    "name": current.name,
                    "event": current.event,
                    "ok": ok,
                    "state": state,
                    "error": error_text,
                }
            )
            if self.logger and not ok:
                self.logger.warning(
                    "scheduled job %s failed: %s", current.id, error_text
                )
        return summaries

    def recover(self):
        return self.store.recover_interrupted(now=self.clock())

    def next_due(self):
        return self.store.next_due(now=self.clock())

    def rows(self, limit=50):
        return [job.to_dict() for job in self.store.recent_jobs(limit=limit)]

    def runs(self, job_id=None, limit=50):
        return self.store.recent_runs(job_id=job_id, limit=limit)

    def stats(self):
        return self.store.stats()
