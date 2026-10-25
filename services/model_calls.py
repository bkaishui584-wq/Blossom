"""Local model-call log: status, latency, role and token usage only.

Prompt text, completions, API keys and authorization headers never enter this
table. The log is for diagnostics and cost visibility, not conversation replay.
"""

import time

from model import errors


class ModelCallLog:
    def __init__(self, database, logger=None, clock=time.time):
        self.db = database
        self.logger = logger
        self.clock = clock

    def record(
        self,
        role="",
        provider="",
        model="",
        ok=False,
        latency_ms=0,
        prompt_tokens=0,
        completion_tokens=0,
        error_kind="",
        error="",
        session_id="",
        streamed=False,
    ):
        try:
            return self.db.insert(
                "INSERT INTO model_call_log"
                " (session_id, role, provider, model, status, latency_ms,"
                " prompt_tokens, completion_tokens, error_kind, error, streamed, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(session_id or "")[:120],
                    str(role or "")[:40],
                    str(provider or "")[:80],
                    str(model or "")[:160],
                    "ok" if ok else "failed",
                    max(0, int(latency_ms or 0)),
                    max(0, int(prompt_tokens or 0)),
                    max(0, int(completion_tokens or 0)),
                    str(error_kind or "")[:60],
                    errors.redact(str(error or ""))[:300],
                    1 if streamed else 0,
                    self.clock(),
                ),
            )
        except Exception as exc:
            self._warn("model call log write failed: %s", exc)
            return None

    def rows(self, limit=30):
        try:
            rows = self.db.query(
                "SELECT * FROM model_call_log ORDER BY id DESC LIMIT ?",
                (max(1, min(500, int(limit or 30))),),
            )
        except Exception as exc:
            self._warn("model call log read failed: %s", exc)
            return []
        return [
            {
                "id": row["id"],
                "at": time.strftime("%m-%d %H:%M:%S", time.localtime(row["created_at"])),
                "role": row["role"],
                "provider": row["provider"],
                "model": row["model"],
                "status": row["status"],
                "latency_ms": int(row["latency_ms"] or 0),
                "prompt_tokens": int(row["prompt_tokens"] or 0),
                "completion_tokens": int(row["completion_tokens"] or 0),
                "total_tokens": int(row["prompt_tokens"] or 0)
                + int(row["completion_tokens"] or 0),
                "error_kind": row["error_kind"],
                "error": row["error"],
                "streamed": bool(row["streamed"]),
            }
            for row in rows
        ]

    def summary(self):
        try:
            row = self.db.query_one(
                "SELECT COUNT(*) AS calls,"
                " COALESCE(SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END), 0) AS ok,"
                " COALESCE(SUM(CASE WHEN status!='ok' THEN 1 ELSE 0 END), 0) AS failed,"
                " COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,"
                " COALESCE(SUM(completion_tokens), 0) AS completion_tokens,"
                " COALESCE(AVG(latency_ms), 0) AS avg_latency_ms"
                " FROM model_call_log"
            ) or {}
        except Exception as exc:
            self._warn("model call summary failed: %s", exc)
            return {}
        return {
            "calls": int(row.get("calls") or 0),
            "ok": int(row.get("ok") or 0),
            "failed": int(row.get("failed") or 0),
            "prompt_tokens": int(row.get("prompt_tokens") or 0),
            "completion_tokens": int(row.get("completion_tokens") or 0),
            "total_tokens": int(row.get("prompt_tokens") or 0)
            + int(row.get("completion_tokens") or 0),
            "avg_latency_ms": int(row.get("avg_latency_ms") or 0),
        }

    def clear(self):
        try:
            return int(self.db.execute("DELETE FROM model_call_log") or 0)
        except Exception as exc:
            self._warn("model call clear failed: %s", exc)
            return 0

    def _warn(self, message, *args):
        if self.logger:
            try:
                self.logger.warning(message, *args)
            except Exception:
                pass
