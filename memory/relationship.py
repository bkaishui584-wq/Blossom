"""关系状态：认识多久、聊过多少次、现在算什么关系。

只存日期和次数，不存聊天内容。存在自己的小表里，不写进偏好表
（偏好表会被整块带进上下文，关系只用一行摘要进上下文）。

关系只影响桌宠怎么说话，不影响权限、工具和安全规则：
人格和关系都不能绕过 Permission。
"""

import json
import time
from datetime import date, datetime

# 天数门槛 -> 阶段名，从大到小第一个满足的算数
STAGES = (
    (30, "很亲近"),
    (7, "老朋友"),
    (1, "熟了"),
    (0, "刚认识"),
)


def _day(now=None):
    moment = datetime.fromtimestamp(now) if now else datetime.now()
    return moment.strftime("%Y-%m-%d")


class RelationshipState:
    """关系状态的读写。读不到就当作刚认识，绝不影响桌宠运行。"""

    def __init__(self, database=None, clock=time.time):
        self.database = database
        self.clock = clock
        self._cache = None

    # ---------- 读 ----------
    def load(self):
        if self._cache is not None:
            return self._cache
        self._cache = self._read()
        return self._cache

    def _read(self):
        if self.database is None:
            return {}
        try:
            row = self.database.query_one("SELECT data FROM relationship_state WHERE id = 1")
        except Exception:
            return {}
        if not row:
            return {}
        try:
            data = json.loads(row["data"] or "{}")
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data):
        if self.database is None:
            return False
        try:
            self.database.execute(
                "INSERT INTO relationship_state (id, data, updated_at)"
                " VALUES (1, ?, datetime('now'))"
                " ON CONFLICT(id) DO UPDATE SET data=excluded.data,"
                " updated_at=datetime('now')",
                (json.dumps(data, ensure_ascii=False),),
            )
        except Exception:
            return False
        self._cache = data
        return True

    # ---------- 写 ----------
    def touch(self, now=None):
        """每收到一句用户消息走一次：记次数与最近一次见面。"""
        moment = self.clock() if now is None else now
        today = _day(moment)
        data = dict(self.load())
        data["first_seen"] = data.get("first_seen") or today
        data["last_seen"] = today
        data["interactions"] = int(data.get("interactions") or 0) + 1
        self._write(data)
        return data

    # ---------- 算 ----------
    def days_known(self, now=None):
        first = self.load().get("first_seen")
        if not first:
            return 0
        try:
            started = date.fromisoformat(str(first))
        except ValueError:
            return 0
        today = date.fromisoformat(_day(self.clock() if now is None else now))
        return max(0, (today - started).days)

    def interactions(self):
        return int(self.load().get("interactions") or 0)

    def stage(self, now=None):
        days = self.days_known(now)
        for threshold, label in STAGES:
            if days >= threshold:
                return label
        return STAGES[-1][1]

    def note(self, now=None):
        """给上下文用的一行关系摘要。还没聊过就返回空字符串。"""
        if not self.load():
            return ""
        return (
            f"我们认识 {self.days_known(now)} 天，"
            f"一共聊过 {self.interactions()} 次，算{self.stage(now)}。"
        )

    def reset(self, confirm=False):
        """清掉认识天数与互动次数，回到刚认识。没有确认就拒绝。"""
        if not confirm or self.database is None:
            return False
        try:
            self.database.execute("DELETE FROM relationship_state WHERE id = 1")
        except Exception:
            return False
        self._cache = {}
        return True

    def describe(self):
        """只给开发者面板看，绝不发给模型。"""
        data = self.load()
        return {
            "first_seen": data.get("first_seen"),
            "last_seen": data.get("last_seen"),
            "days": self.days_known(),
            "interactions": self.interactions(),
            "stage": self.stage(),
        }
