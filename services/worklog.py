"""Local work-mode records. Off by default, metadata only.

The service stores foreground application intervals and never reads screen
pixels, keyboard content, clipboard content or page bodies. Private chat
categories keep only the application name. Sampling is coalesced into
intervals so a long session does not become a stream of duplicate rows.
"""

import csv
import json
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

from behavior.awareness import PRIVATE_CATEGORIES, WindowsActivityProvider, category_label, classify

ENABLED_KEY = "work.enabled"
PAUSED_KEY = "work.paused"
CONSENT_KEY = "work.consent"
RETENTION_KEY = "work.retention_days"
PROJECT_KEY = "work.project"
EXCLUDE_KEY = "work.exclude"

DEFAULT_RETENTION_DAYS = 30
DEFAULT_IDLE_LIMIT = 300
MAX_TITLE_CHARS = 160


def _stamp(value):
    return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")


def _day(value):
    return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d")


def _as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _domain(raw):
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        parsed = urlparse(text if "://" in text else "https://" + text)
        return (parsed.hostname or "").lower()[:120]
    except Exception:
        return ""


class WorkLogService:
    """Persistent local work records and deterministic summaries."""

    def __init__(
        self,
        database,
        preferences=None,
        provider=None,
        logger=None,
        clock=time.time,
        idle_limit=DEFAULT_IDLE_LIMIT,
    ):
        self.db = database
        self.preferences = preferences
        self.provider = provider or WindowsActivityProvider()
        self.logger = logger
        self.clock = clock
        self.idle_limit = max(30, int(idle_limit))
        self._last_seen = 0.0
        self._last_prune = 0.0
        self.repair_open_intervals()
        self.prune()

    # ---------- settings ----------
    def _pref(self, key, default=None):
        if self.preferences is None:
            return default
        try:
            value = self.preferences.get_preference(key)
        except Exception:
            return default
        return default if value is None else value

    def _set(self, key, value):
        if self.preferences is None:
            return False
        try:
            self.preferences.set_preference(key, str(value))
            return True
        except Exception as exc:
            self._warn("work preference write failed: %s", exc)
            return False

    def enabled(self):
        return str(self._pref(ENABLED_KEY, "0")).strip() == "1"

    def paused(self):
        return str(self._pref(PAUSED_KEY, "1")).strip() == "1"

    def consent(self):
        return str(self._pref(CONSENT_KEY, "0")).strip() == "1"

    def active(self):
        return self.enabled() and not self.paused() and self.consent()

    def available(self):
        try:
            return bool(self.provider.is_available())
        except Exception:
            return False

    def set_enabled(self, flag, consent=False):
        wanted = bool(flag)
        if wanted and not (consent or self.consent()):
            return False
        now = self.clock()
        if not wanted:
            self.close_current(now)
        if wanted:
            self._set(CONSENT_KEY, "1")
            self._set(ENABLED_KEY, "1")
            self._set(PAUSED_KEY, "0")
            self._audit("enable", "用户明确开启工作记录")
        else:
            self._set(ENABLED_KEY, "0")
            self._set(PAUSED_KEY, "1")
            self._audit("disable", "用户关闭工作记录")
        return True

    def pause(self):
        self.close_current(self.clock())
        ok = self._set(PAUSED_KEY, "1")
        if ok:
            self._audit("pause", "用户暂停工作记录")
        return ok

    def resume(self):
        if not self.enabled() or not self.consent():
            return False
        ok = self._set(PAUSED_KEY, "0")
        if ok:
            self._audit("resume", "用户恢复工作记录")
        return ok

    def retention_days(self):
        value = _as_int(self._pref(RETENTION_KEY, DEFAULT_RETENTION_DAYS), DEFAULT_RETENTION_DAYS)
        return max(1, min(3650, value))

    def set_retention_days(self, days):
        value = max(1, min(3650, _as_int(days, DEFAULT_RETENTION_DAYS)))
        ok = self._set(RETENTION_KEY, value)
        if ok:
            self.prune()
            self._audit("retention", "记录保留 %d 天" % value)
        return value if ok else self.retention_days()

    def project(self):
        return str(self._pref(PROJECT_KEY, "") or "").strip()[:80]

    def set_project(self, value):
        text = " ".join(str(value or "").split())[:80]
        if not self._set(PROJECT_KEY, text):
            return False
        self._audit("project", "当前项目：%s" % (text or "未命名"))
        return True

    def exclusions(self):
        raw = self._pref(EXCLUDE_KEY, "")
        if not raw:
            return []
        try:
            rows = json.loads(raw)
        except Exception:
            return []
        return [str(item).strip().lower() for item in rows if str(item).strip()][:100]

    def add_exclusion(self, app):
        name = str(app or "").strip().lower()
        if not name:
            return False
        rows = self.exclusions()
        if name not in rows:
            rows.append(name)
            if not self._set(EXCLUDE_KEY, json.dumps(rows, ensure_ascii=False)):
                return False
            self._audit("exclude", "排除应用：%s" % name)
        return True

    def remove_exclusion(self, app):
        name = str(app or "").strip().lower()
        rows = [item for item in self.exclusions() if item != name]
        if not self._set(EXCLUDE_KEY, json.dumps(rows, ensure_ascii=False)):
            return False
        self._audit("include", "恢复应用：%s" % name)
        return True

    # ---------- sampling ----------
    def observe(self, now=None, idle_seconds=0, sample=None):
        """Record one foreground sample. Inactive, idle or excluded means no new row."""
        moment = _as_float(now, self.clock())
        if not self.active():
            self.close_current(moment)
            return None
        if max(0.0, _as_float(idle_seconds)) >= self.idle_limit:
            self.close_current(self._last_seen or moment)
            return None
        raw = sample
        if raw is None:
            try:
                raw = self.provider.current() or {}
            except Exception as exc:
                self._warn("work sample failed: %s", exc)
                return None
        app = str(raw.get("app") or "").strip()[:120]
        if not app:
            self.close_current(self._last_seen or moment)
            return None
        if self._excluded(app):
            self.close_current(self._last_seen or moment)
            return None

        title = str(raw.get("title") or "").strip()[:MAX_TITLE_CHARS]
        category, _confidence = classify(app, title)
        if category in PRIVATE_CATEGORIES:
            title = ""
        domain = _domain(raw.get("domain"))
        project = self.project()

        self._last_seen = moment
        last = self.last_open()
        if (
            last is not None
            and str(last.get("app") or "") == app
            and str(last.get("title") or "") == title
            and str(last.get("category") or "") == category
            and str(last.get("project") or "") == project
            and _day(last.get("started_at") or moment) == _day(moment)
        ):
            self._update(last["id"], moment, domain)
            self._maybe_prune(moment)
            return self.get(last["id"])

        self.close_current(self._last_seen if last is not None else moment)
        row_id = self.db.insert(
            "INSERT INTO work_intervals"
            " (app, title, category, domain, project, started_at, ended_at, seconds,"
            " source, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, NULL, 0, 'foreground', ?, ?)",
            (app, title, category, domain, project, moment, moment, moment),
        )
        self._audit("record", "开始记录 %s" % (category_label(category) if category else app))
        self._maybe_prune(moment)
        return self.get(row_id)

    def close_current(self, now=None):
        row = self.last_open()
        if row is None:
            return False
        moment = max(_as_float(now, self.clock()), _as_float(row.get("started_at")))
        self.db.execute(
            "UPDATE work_intervals SET ended_at=?, seconds=?, updated_at=?"
            " WHERE id=? AND ended_at IS NULL",
            (moment, max(0, int(moment - _as_float(row.get("started_at")))), moment, row["id"]),
        )
        return True

    def repair_open_intervals(self):
        """Close rows left open by a crash at their last known write time."""
        try:
            rows = self.db.query("SELECT * FROM work_intervals WHERE ended_at IS NULL")
        except Exception:
            return 0
        count = 0
        for row in rows:
            end = max(
                _as_float(row.get("started_at")),
                _as_float(row.get("updated_at"), _as_float(row.get("started_at"))),
            )
            self.db.execute(
                "UPDATE work_intervals SET ended_at=?, seconds=?, updated_at=?"
                " WHERE id=? AND ended_at IS NULL",
                (end, max(0, int(end - _as_float(row.get("started_at")))), end, row["id"]),
            )
            count += 1
        return count

    def last_open(self):
        try:
            row = self.db.query_one(
                "SELECT * FROM work_intervals WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
            )
        except Exception:
            return None
        return dict(row) if row else None

    def get(self, row_id):
        try:
            row = self.db.query_one("SELECT * FROM work_intervals WHERE id=?", (int(row_id),))
        except Exception:
            return None
        return self._decorate(row) if row else None

    def _update(self, row_id, moment, domain):
        row = self.db.query_one("SELECT * FROM work_intervals WHERE id=?", (int(row_id),))
        if not row:
            return
        start = _as_float(row.get("started_at"))
        self.db.execute(
            "UPDATE work_intervals SET ended_at=?, seconds=?, domain=?, updated_at=?"
            " WHERE id=? AND ended_at IS NULL",
            (moment, max(0, int(moment - start)), domain or row.get("domain") or "", moment, row_id),
        )

    def _excluded(self, app):
        name = str(app or "").lower()
        stem = name[:-4] if name.endswith(".exe") else name
        return any(item in (name, stem) for item in self.exclusions())

    # ---------- reads ----------
    def records(self, day=None, limit=200):
        stamp = day or time.strftime("%Y-%m-%d", time.localtime(self.clock()))
        try:
            rows = self.db.query(
                "SELECT * FROM work_intervals"
                " WHERE date(started_at, 'unixepoch', 'localtime') = ?"
                " ORDER BY started_at DESC LIMIT ?",
                (stamp, max(1, min(2000, int(limit or 200)))),
            )
        except Exception as exc:
            self._warn("work records read failed: %s", exc)
            return []
        return [self._decorate(row) for row in rows]

    def stats(self, day=None, end_day=None):
        start = day or time.strftime("%Y-%m-%d", time.localtime(self.clock()))
        end = end_day or start
        try:
            rows = self.db.query(
                "SELECT app, category, project, SUM(seconds) AS seconds,"
                " COUNT(*) AS intervals FROM work_intervals"
                " WHERE date(started_at, 'unixepoch', 'localtime') BETWEEN ? AND ?"
                " GROUP BY app, category, project ORDER BY seconds DESC, app",
                (start, end),
            )
        except Exception as exc:
            self._warn("work stats read failed: %s", exc)
            return {"start": start, "end": end, "seconds": 0, "rows": []}
        items = [
            {
                "app": row["app"],
                "category": row["category"],
                "category_label": category_label(row["category"]),
                "project": row["project"] or "",
                "seconds": int(row["seconds"] or 0),
                "intervals": int(row["intervals"] or 0),
            }
            for row in rows
        ]
        return {
            "start": start,
            "end": end,
            "seconds": sum(item["seconds"] for item in items),
            "rows": items,
        }

    def report(self, day=None, weekly=False):
        stamp = day or time.strftime("%Y-%m-%d", time.localtime(self.clock()))
        if weekly:
            try:
                end = datetime.strptime(stamp, "%Y-%m-%d")
            except ValueError:
                end = datetime.now()
            start = end - timedelta(days=6)
            facts = self.stats(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
            label = "%s 到 %s 这一周" % (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        else:
            facts = self.stats(stamp, stamp)
            label = stamp
        if not facts["rows"]:
            return {"ok": True, "day": stamp, "facts": facts, "text": label + "没有工作记录。"}
        lines = ["%s共记录 %s。" % (label, self.duration(facts["seconds"]))]
        for row in facts["rows"][:8]:
            name = row["category_label"] if row["category_label"] != "未分类" else row["app"]
            project = "，项目 %s" % row["project"] if row["project"] else ""
            lines.append("%s：%s%s" % (name, self.duration(row["seconds"]), project))
        return {"ok": True, "day": stamp, "facts": facts, "text": "\n".join(lines)}

    def audit_rows(self, limit=30):
        try:
            rows = self.db.query(
                "SELECT action, detail, created_at FROM work_audit"
                " ORDER BY id DESC LIMIT ?",
                (max(1, int(limit or 30)),),
            )
        except Exception:
            return []
        return [
            {"action": row["action"], "detail": row["detail"], "at": _stamp(row["created_at"])}
            for row in rows
        ]

    def state(self):
        return {
            "enabled": self.enabled(),
            "paused": self.paused(),
            "consent": self.consent(),
            "active": self.active(),
            "available": self.available(),
            "retention_days": self.retention_days(),
            "project": self.project(),
            "exclusions": self.exclusions(),
            "provider": getattr(self.provider, "name", "?"),
        }

    # ---------- delete/export ----------
    def delete_day(self, day=None):
        stamp = day or time.strftime("%Y-%m-%d", time.localtime(self.clock()))
        try:
            changed = self.db.execute(
                "DELETE FROM work_intervals"
                " WHERE date(started_at, 'unixepoch', 'localtime') = ?",
                (stamp,),
            )
        except Exception as exc:
            self._warn("work day delete failed: %s", exc)
            return 0
        self._audit("delete", "删除 %s 的工作记录" % stamp)
        return int(changed or 0)

    def clear(self):
        try:
            changed = self.db.execute("DELETE FROM work_intervals")
        except Exception as exc:
            self._warn("work clear failed: %s", exc)
            return 0
        self._audit("delete_all", "删除全部工作记录")
        return int(changed or 0)

    def prune(self):
        cutoff = self.clock() - self.retention_days() * 86400
        try:
            changed = self.db.execute(
                "DELETE FROM work_intervals WHERE started_at < ?", (cutoff,)
            )
        except Exception as exc:
            self._warn("work prune failed: %s", exc)
            return 0
        return int(changed or 0)

    def export_csv(self, target, day=None, end_day=None):
        start = day or time.strftime("%Y-%m-%d", time.localtime(self.clock()))
        end = end_day or start
        try:
            rows = self.db.query(
                "SELECT * FROM work_intervals"
                " WHERE date(started_at, 'unixepoch', 'localtime') BETWEEN ? AND ?"
                " ORDER BY started_at",
                (start, end),
            )
            with open(target, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(
                    ["开始", "结束", "应用", "窗口标题", "类别", "项目", "秒数", "域名"]
                )
                for row in rows:
                    writer.writerow(
                        [
                            _stamp(row["started_at"]),
                            _stamp(row["ended_at"] or row["updated_at"]),
                            row["app"],
                            row["title"],
                            category_label(row["category"]),
                            row["project"],
                            row["seconds"],
                            row["domain"],
                        ]
                    )
        except Exception as exc:
            self._warn("work export failed: %s", exc)
            return False
        self._audit("export", "导出 %s 到 %s" % (start, end))
        return True

    @staticmethod
    def duration(seconds):
        total = max(0, int(seconds or 0))
        hours, remainder = divmod(total, 3600)
        minutes = remainder // 60
        if hours:
            return "%d 小时 %d 分钟" % (hours, minutes)
        return "%d 分钟" % max(1, minutes) if total else "0 分钟"

    # ---------- internal ----------
    def _decorate(self, row):
        item = dict(row)
        item["start_label"] = _stamp(item.get("started_at"))
        item["end_label"] = _stamp(item.get("ended_at") or item.get("updated_at") or item.get("started_at"))
        item["category_label"] = category_label(item.get("category"))
        item["duration"] = self.duration(item.get("seconds"))
        return item

    def _maybe_prune(self, now):
        if now - self._last_prune < 3600:
            return 0
        self._last_prune = now
        return self.prune()

    def _audit(self, action, detail):
        try:
            self.db.execute(
                "INSERT INTO work_audit (action, detail, created_at) VALUES (?, ?, ?)",
                (str(action), str(detail)[:300], self.clock()),
            )
        except Exception as exc:
            self._warn("work audit write failed: %s", exc)

    def _warn(self, message, *args):
        if self.logger:
            try:
                self.logger.warning(message, *args)
            except Exception:
                pass
