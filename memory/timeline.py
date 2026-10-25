"""真实时间线：只记确实发生过的第一次，绝不编造天数。

事件来自真实数据：第一条消息就是第一次聊天，第一条反馈记忆就是第一次反馈，
其余第一次在真正发生的那一刻记下。同一件事只记一次，历史可追溯。

时间线只给关系、日记和开发者面板参考，不发给模型。
"""

import time

# 允许记录的时间点。写别的键会被忽略，避免时间线混进无关内容。
MILESTONES = (
    "first_start",
    "first_chat",
    "first_task",
    "first_voice",
    "first_voice_pack",
    "first_live2d",
    "first_device",
    "first_feedback",
)

LABELS = {
    "first_start": "第一次启动",
    "first_chat": "第一次聊天",
    "first_task": "第一次完成任务",
    "first_voice": "第一次语音互动",
    "first_voice_pack": "第一次用上语音包",
    "first_live2d": "第一次接入 Live2D",
    "first_device": "第一次接入设备",
    "first_feedback": "第一次人格反馈",
}


class Timeline:
    """重要事件的第一次。数据库不可用时只读不写，不影响桌宠运行。"""

    def __init__(self, database=None, clock=time.time):
        self.database = database
        self.clock = clock

    # ---------- 写 ----------
    def mark(self, key, detail="", when=None):
        """记一件事。已经记过的不会被覆盖，返回是否真的记上了。"""
        name = str(key or "").strip()
        if name not in MILESTONES or self.database is None:
            return False
        moment = when or time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(self.clock())
        )
        try:
            changed = self.database.execute(
                "INSERT INTO timeline (key, detail, happened_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO NOTHING",
                (name, str(detail or ""), str(moment)),
            )
        except Exception:
            return False
        return bool(changed)

    def sync(self, memory):
        """从真实数据补记错过的时间点：第一次聊天、第一次人格反馈。"""
        if self.database is None:
            return 0
        first = self._first_of("SELECT created_at FROM messages ORDER BY id LIMIT 1")
        if first:
            self.mark("first_chat", when=first)
        feedback = self._first_of(
            "SELECT created_at FROM memories WHERE kind = 'feedback' ORDER BY id LIMIT 1"
        )
        if feedback:
            self.mark("first_feedback", when=feedback)
        return len(self.entries())

    # ---------- 读 ----------
    def has(self, key):
        return str(key) in self._rows()

    def entries(self, limit=100):
        rows = self._query(
            "SELECT key, detail, happened_at FROM timeline ORDER BY id LIMIT ?",
            (max(1, int(limit)),),
        )
        return [
            {
                "key": row["key"],
                "label": LABELS.get(row["key"], row["key"]),
                "detail": row["detail"],
                "day": str(row["happened_at"] or "")[:10],
            }
            for row in rows
        ]

    def describe(self):
        """开发者面板用的只读视图。绝不发给模型。"""
        rows = self._query("SELECT key, happened_at FROM timeline ORDER BY id", ())
        marks = {row["key"]: str(row["happened_at"] or "")[:10] for row in rows}
        return {"count": len(marks), "milestones": marks}

    # ---------- 内部 ----------
    def _rows(self):
        return {row["key"] for row in self._query("SELECT key FROM timeline", ())}

    def _first_of(self, sql):
        rows = self._query(sql, ())
        if not rows:
            return ""
        return str(list(rows[0].values())[0] or "")

    def _query(self, sql, params):
        if self.database is None:
            return []
        try:
            return self.database.query(sql, params)
        except Exception:
            return []
