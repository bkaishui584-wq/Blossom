"""Skill 运行记录。落库的是事实：调过什么、花了多久、成没成。

只写入自己的表，不碰记忆、权限和任务表。
"""

import json


class SkillRunStore:
    MAX_DETAIL = 800

    def __init__(self, database, logger=None):
        self.db = database
        self.logger = logger

    def save(self, result, request=""):
        if self.db is None:
            return None
        tools = [str(name) for name in (getattr(result, "tools", None) or [])]
        try:
            return self.db.insert(
                "INSERT INTO skill_runs"
                " (skill, request, ok, steps, seconds, tools, tokens, error, detail)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(getattr(result, "skill", "")),
                    str(request or "")[:200],
                    1 if getattr(result, "ok", False) else 0,
                    len(getattr(result, "steps", None) or []),
                    float(getattr(result, "seconds", 0.0) or 0.0),
                    json.dumps(tools, ensure_ascii=False),
                    int(getattr(result, "tokens", 0) or 0),
                    str(getattr(result, "error", "") or "")[:300],
                    str(getattr(result, "summary", "") or "")[: self.MAX_DETAIL],
                ),
            )
        except Exception as exc:
            if self.logger:
                self.logger.warning("skill run save failed: %s", exc)
            return None

    def recent(self, limit=50, skill=None):
        if self.db is None:
            return []
        try:
            if skill:
                return self.db.query(
                    "SELECT * FROM skill_runs WHERE skill = ? ORDER BY id DESC LIMIT ?",
                    (str(skill), int(limit)),
                )
            return self.db.query(
                "SELECT * FROM skill_runs ORDER BY id DESC LIMIT ?", (int(limit),)
            )
        except Exception as exc:
            if self.logger:
                self.logger.warning("skill run read failed: %s", exc)
            return []

    def stats(self, skill=None):
        """每个 Skill 的真实成功率、耗时和失败类型。读不到就返回空统计。"""
        rows = self.recent(limit=500, skill=skill)
        summary = {}
        for row in rows:
            name = str(row.get("skill") or "")
            bucket = summary.setdefault(
                name, {"skill": name, "runs": 0, "ok": 0, "seconds": 0.0, "tokens": 0, "failures": {}}
            )
            bucket["runs"] += 1
            if row.get("ok"):
                bucket["ok"] += 1
            else:
                error = str(row.get("error") or "未说明")
                bucket["failures"][error] = bucket["failures"].get(error, 0) + 1
            bucket["seconds"] += float(row.get("seconds") or 0.0)
            bucket["tokens"] += int(row.get("tokens") or 0)
        for bucket in summary.values():
            runs = bucket["runs"] or 1
            bucket["success_rate"] = round(bucket["ok"] / runs, 3)
            bucket["avg_seconds"] = round(bucket["seconds"] / runs, 3)
        return summary

    def counts(self):
        if self.db is None:
            return {}
        stats = self.stats()
        return {name: bucket["runs"] for name, bucket in stats.items()}
