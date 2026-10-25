"""每日与每周回顾。

事实来自数据库里的真实记录：聊过几句、写过什么记忆、完成过哪些任务、
时间线上确实发生的事。先把事实摆出来，需要措辞时再交给模型，
绝不为了凑字数编事件。
"""

import time
from datetime import datetime, timedelta

DAY = "%Y-%m-%d"


class ReviewService:
    def __init__(self, database, memory=None, tasks=None, logger=None, clock=time.time):
        self.db = database
        self.memory = memory
        self.tasks = tasks
        self.logger = logger
        self.clock = clock

    def _warn(self, message, *args):
        if self.logger:
            try:
                self.logger.warning(message, *args)
            except Exception:
                pass

    def _rows(self, sql, params=()):
        try:
            return self.db.query(sql, params)
        except Exception as exc:
            self._warn("review query failed: %s", exc)
            return []

    # ---------- 事实 ----------
    def facts(self, start, end):
        """start / end 都是 YYYY-MM-DD 字符串，闭区间。"""
        facts = {
            "range": [start, end],
            "messages": 0,
            "memories": 0,
            "tasks_done": 0,
            "tasks_failed": 0,
            "events": [],
            "timeline": [],
            "timeline_count": 0,
            "days": 1,
        }
        row = self._rows(
            "SELECT COUNT(*) AS n FROM messages WHERE date(created_at) BETWEEN ? AND ?",
            (start, end),
        )
        facts["messages"] = int(row[0]["n"]) if row else 0
        row = self._rows(
            "SELECT COUNT(*) AS n FROM memories WHERE date(created_at) BETWEEN ? AND ?",
            (start, end),
        )
        facts["memories"] = int(row[0]["n"]) if row else 0
        row = self._rows(
            "SELECT COUNT(*) AS n FROM timeline WHERE date(happened_at) BETWEEN ? AND ?",
            (start, end),
        )
        facts["timeline_count"] = int(row[0]["n"]) if row else 0
        rows = self._rows(
            "SELECT key, detail, happened_at FROM timeline"
            " WHERE date(happened_at) BETWEEN ? AND ? ORDER BY happened_at",
            (start, end),
        )
        facts["timeline"] = [
            {"key": row["key"], "detail": row["detail"], "at": row["happened_at"]}
            for row in rows
        ]
        rows = self._rows(
            "SELECT title, when_at FROM calendar_events"
            " WHERE date(when_at) BETWEEN ? AND ? ORDER BY when_at",
            (start, end),
        )
        facts["events"] = [{"title": row["title"], "when": row["when_at"]} for row in rows]
        try:
            first = datetime.strptime(start, DAY)
            last = datetime.strptime(end, DAY)
            facts["days"] = max(1, (last - first).days + 1)
        except ValueError:
            facts["days"] = 1
        if self.tasks is not None:
            try:
                for record in self.tasks.recent(limit=50):
                    stamp = str(record.updated_at or "")[:10]
                    if not (start <= stamp <= end):
                        continue
                    if record.state == "completed":
                        facts["tasks_done"] += 1
                    elif record.state == "failed":
                        facts["tasks_failed"] += 1
            except Exception as exc:
                self._warn("review task scan failed: %s", exc)
        return facts

    # ---------- 文字：只用事实，凑不出来就说没有记录 ----------
    @staticmethod
    def _facts_sentence(facts):
        messages = facts["messages"]
        memories = facts["memories"]
        done = facts["tasks_done"]
        failed = facts["tasks_failed"]
        bits = []
        if messages:
            bits.append("我们聊了 %d 句" % messages)
        if memories:
            bits.append("记下 %d 条东西" % memories)
        if done:
            bits.append("做完 %d 个任务" % done)
        if failed:
            bits.append("%d 个任务没成" % failed)
        timeline = facts.get("timeline") or []
        if timeline:
            bits.append("还发生了「%s」" % timeline[-1].get("key", ""))
        if not bits:
            return "没有留下记录。"
        return "，".join(bits) + "。"

    def daily(self, day=None):
        stamp = day or time.strftime(DAY, time.localtime(self.clock()))
        facts = self.facts(stamp, stamp)
        return {
            "ok": True,
            "day": stamp,
            "facts": facts,
            "text": stamp + " 那天，" + self._facts_sentence(facts),
        }

    def weekly(self, day=None):
        try:
            end = datetime.strptime(day or time.strftime(DAY, time.localtime(self.clock())), DAY)
        except ValueError:
            end = datetime.now()
        start = end - timedelta(days=6)
        facts = self.facts(start.strftime(DAY), end.strftime(DAY))
        span = "%s 到 %s 这一周，" % (start.strftime(DAY), end.strftime(DAY))
        return {
            "ok": True,
            "day": end.strftime(DAY),
            "facts": facts,
            "text": span + self._facts_sentence(facts),
        }

    def describe(self):
        today = self.daily()
        week = self.weekly()
        return {
            "today": today["text"],
            "week": week["text"],
            "today_messages": today["facts"]["messages"],
            "week_messages": week["facts"]["messages"],
        }
