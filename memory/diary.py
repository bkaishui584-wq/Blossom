"""日记：每天一条，由程序从当天数据里总结，不调用模型。

日记只写事实：那天说了多少句话、新记住几件事、我们认识多久了。
不编故事，不猜心情，也不把聊天内容原样抄进去。

写日记是懒触发：启动时补一次已经过完的日子，用户问日记时再补一次。
没有任何轮询，也不会为了写日记去问模型。
"""

import time
from datetime import datetime, timedelta

DEFAULT_MAX_CHARS = 300
CLIP = 40


def _clip(text, limit=CLIP):
    clean = " ".join(str(text or "").split())
    return clean if len(clean) <= limit else clean[:limit] + "…"


class DiaryService:
    """日记的读写。数据库不可用时只读不写，不影响桌宠运行。"""

    def __init__(
        self,
        database=None,
        relationship=None,
        clock=time.time,
        max_chars=DEFAULT_MAX_CHARS,
    ):
        self.database = database
        self.relationship = relationship
        self.clock = clock
        self.max_chars = int(max_chars)

    # ---------- 写 ----------
    def ensure_recent(self, days=2):
        """把已经过完的日子补上；同一天不会重复写。"""
        written = []
        for offset in range(1, max(1, int(days)) + 1):
            day = self._day(offset)
            if self.entry(day):
                continue
            written.append(self.write_day(day))
        return written

    def write_day(self, day=None, force=False):
        day = str(day or self._day(1))
        existing = None if force else self.entry(day)
        if existing:
            return {"day": day, "content": existing["content"], "saved": True, "existing": True}
        content = self._content(day)
        if self.database is None:
            return {"day": day, "content": content, "saved": False}
        try:
            self.database.execute(
                "INSERT INTO diary_entries (day, content) VALUES (?, ?)"
                " ON CONFLICT(day) DO UPDATE SET content=excluded.content",
                (day, content),
            )
        except Exception as exc:
            return {"day": day, "content": content, "saved": False, "error": str(exc)}
        return {"day": day, "content": content, "saved": True}

    # ---------- 读 ----------
    def entry(self, day):
        if self.database is None:
            return None
        try:
            return self.database.query_one(
                "SELECT day, content FROM diary_entries WHERE day = ?", (str(day),)
            )
        except Exception:
            return None

    def read(self, limit=7):
        if self.database is None:
            return []
        try:
            return self.database.query(
                "SELECT day, content FROM diary_entries ORDER BY day DESC LIMIT ?",
                (max(1, int(limit)),),
            )
        except Exception:
            return []

    def read_text(self, limit=3):
        rows = self.read(limit)
        return "\n".join(f"{row['day']}：{row['content']}" for row in rows)

    def count(self):
        if self.database is None:
            return 0
        try:
            row = self.database.query_one("SELECT COUNT(*) AS n FROM diary_entries")
        except Exception:
            return 0
        return int(row["n"]) if row else 0

    def describe(self):
        """只给开发者面板看，绝不发给模型。"""
        rows = self.read(1)
        return {"entries": self.count(), "latest": rows[0]["day"] if rows else None}

    # ---------- 内部 ----------
    def _content(self, day):
        spoken = self._count(
            "SELECT COUNT(*) AS n FROM messages WHERE date(created_at, 'localtime') = ?",
            day,
        )
        memories = self._query(
            "SELECT content FROM memories WHERE date(created_at, 'localtime') = ?"
            " ORDER BY id DESC LIMIT 5",
            day,
        )
        parts = [f"我们说了 {spoken} 句话。"]
        if memories:
            joined = "、".join(_clip(row["content"]) for row in memories)
            parts.append(f"新记住 {len(memories)} 件事：{joined}。")
        else:
            parts.append("没有新记住的事。")
        note = self._note()
        if note:
            parts.append(note)
        return "".join(parts)[: self.max_chars]

    def _note(self):
        if self.relationship is None:
            return ""
        try:
            return self.relationship.note()
        except Exception:
            return ""

    def _count(self, sql, day):
        rows = self._query(sql, day)
        if not rows:
            return 0
        return int(list(rows[0].values())[0] or 0)

    def _query(self, sql, day):
        if self.database is None:
            return []
        try:
            return self.database.query(sql, (day,))
        except Exception:
            return []

    def _day(self, offset=0):
        moment = datetime.fromtimestamp(self.clock()) - timedelta(days=int(offset))
        return moment.strftime("%Y-%m-%d")
