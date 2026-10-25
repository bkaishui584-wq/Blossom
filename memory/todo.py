"""待办清单：只读写本地 SQLite，不联网也不需要模型。

没有截止时间的待办排在有时限的后面，因为列表要能一眼看出「今天要做什么」。
完成是标记状态，不删记录，用户之后还能翻回去看。
"""

from datetime import datetime, timedelta

from services.calendar import parse_when

OPEN = "open"
DONE = "done"


class TodoStore:
    """本地待办。所有写操作都返回 ok/error，调用方不用猜。"""

    def __init__(self, database, clock=datetime.now, logger=None):
        self.db = database
        self.clock = clock
        self.logger = logger

    def add(self, title, due_at="", detail="", priority=0):
        title = " ".join(str(title or "").split())
        if not title:
            return {"ok": False, "error": "待办要有内容，说一句要做什么就行"}
        if len(title) > 200:
            return {"ok": False, "error": "待办标题太长了，收短一点"}
        due, problem = self._due(due_at)
        if problem:
            return {"ok": False, "error": problem}
        try:
            todo_id = self.db.insert(
                "INSERT INTO todos (title, detail, due_at, priority) VALUES (?, ?, ?, ?)",
                (title, str(detail or "").strip()[:500], due, self._priority(priority)),
            )
        except Exception as exc:
            self._warn("todo add failed: %s", exc)
            return {"ok": False, "error": "待办没写进本地库，请稍后再试"}
        return {"ok": True, "data": {"id": todo_id, "title": title, "due_at": due}}

    def list(self, include_done=False, limit=20):
        try:
            limit = max(1, min(100, int(limit or 20)))
        except (TypeError, ValueError):
            limit = 20
        where = "" if include_done else "WHERE state = 'open'"
        try:
            rows = self.db.query(
                "SELECT id, title, detail, due_at, priority, state, created_at, done_at"
                f" FROM todos {where}"
                " ORDER BY CASE WHEN due_at = '' THEN 1 ELSE 0 END, due_at ASC,"
                " priority DESC, id ASC LIMIT ?",
                (limit,),
            )
        except Exception as exc:
            self._warn("todo list failed: %s", exc)
            return {"ok": False, "error": "待办读不出来，请稍后再试"}
        for row in rows:
            row["overdue"] = self._overdue(row)
        return {"ok": True, "data": {"count": len(rows), "todos": rows}}

    def done(self, todo_id):
        key = self._key(todo_id)
        if key is None:
            return {"ok": False, "error": "待办编号不对，先把编号告诉我"}
        row = self._get(key)
        if row is None:
            return {"ok": False, "error": "没有编号 %s 的待办" % key}
        if row["state"] == DONE:
            return {"ok": True, "data": {"id": key, "title": row["title"], "already": True}}
        try:
            self.db.execute(
                "UPDATE todos SET state = ?, done_at = ?, updated_at = datetime('now')"
                " WHERE id = ?",
                (DONE, self.clock().strftime("%Y-%m-%d %H:%M:%S"), key),
            )
        except Exception as exc:
            self._warn("todo done failed: %s", exc)
            return {"ok": False, "error": "待办没更新成功，请稍后再试"}
        return {"ok": True, "data": {"id": key, "title": row["title"], "already": False}}

    def remove(self, todo_id):
        """只删用户明确点名的那一条，普通流程不做删除。"""
        key = self._key(todo_id)
        if key is None:
            return {"ok": False, "error": "待办编号不对"}
        row = self._get(key)
        if row is None:
            return {"ok": False, "error": "没有编号 %s 的待办" % key}
        self.db.execute("DELETE FROM todos WHERE id = ?", (key,))
        return {"ok": True, "data": {"id": key, "title": row["title"]}}

    def postpone(self, todo_id, minutes=15):
        key = self._key(todo_id)
        if key is None:
            return {"ok": False, "error": "待办编号不对"}
        row = self._get(key)
        if row is None:
            return {"ok": False, "error": "没有编号 %s 的待办" % key}
        try:
            delta = timedelta(minutes=max(1, int(minutes or 15)))
        except (TypeError, ValueError):
            delta = timedelta(minutes=15)
        due = self._parse_due(row.get("due_at"))
        base = max(due, self.clock()) if due is not None else self.clock()
        updated = base + delta
        itself = updated.strftime("%Y-%m-%d %H:%M")
        self.db.execute(
            "UPDATE todos SET due_at=?, state='open', updated_at=? WHERE id=?",
            (itself, self.clock().strftime("%Y-%m-%d %H:%M:%S"), key),
        )
        return {"ok": True, "data": {"id": key, "title": row["title"], "due_at": itself}}

    def due(self, now=None):
        """到时间还没完成的待办。定时提醒在后面的阶段接上。"""
        stamp = (now or self.clock()).strftime("%Y-%m-%d %H:%M")
        try:
            return self.db.query(
                "SELECT id, title, due_at FROM todos"
                " WHERE state = 'open' AND due_at != '' AND due_at <= ?"
                " ORDER BY due_at ASC, id ASC",
                (stamp,),
            )
        except Exception as exc:
            self._warn("todo due failed: %s", exc)
            return []

    def open_count(self):
        row = self.db.query_one("SELECT COUNT(*) AS n FROM todos WHERE state = 'open'")
        return int(row["n"]) if row else 0

    def scheduled(self, limit=50):
        """还没做完、而且设了时间的待办。启动时按这个补挂提醒。"""
        try:
            limit = max(1, min(200, int(limit or 50)))
        except (TypeError, ValueError):
            limit = 50
        try:
            return self.db.query(
                "SELECT id, title, due_at FROM todos"
                " WHERE state = 'open' AND due_at != ''"
                " ORDER BY due_at ASC, id ASC LIMIT ?",
                (limit,),
            )
        except Exception as exc:
            self._warn("todo scheduled failed: %s", exc)
            return []

    # ---------- 内部 ----------
    def _due(self, raw):
        text = " ".join(str(raw or "").split())
        if not text:
            return "", ""
        parsed = parse_when(text, self.clock())
        if not parsed:
            return "", "时间不认识，可以写 2026-10-12 09:00，或者留空当普通待办"
        return parsed, ""

    @staticmethod
    def _parse_due(raw):
        text = parse_when(raw)
        if not text:
            return None
        try:
            return datetime.strptime(text, "%Y-%m-%d %H:%M")
        except ValueError:
            return None

    def _overdue(self, row):
        if row.get("state") != OPEN or not row.get("due_at"):
            return False
        return row["due_at"] <= self.clock().strftime("%Y-%m-%d %H:%M")

    def _get(self, key):
        try:
            return self.db.query_one("SELECT * FROM todos WHERE id = ?", (key,))
        except Exception as exc:
            self._warn("todo read failed: %s", exc)
            return None

    @staticmethod
    def _key(todo_id):
        try:
            return int(todo_id)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _priority(value):
        try:
            return max(0, min(9, int(value or 0)))
        except (TypeError, ValueError):
            return 0

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)
