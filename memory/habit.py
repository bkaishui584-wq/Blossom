"""习惯模型：从真实聊天时间推断活跃时段，只当推测，不当事实。

只读消息时间戳，按小时统计；样本不足就明确说「还看不出习惯」。
推断结果存成本地偏好（Inferred Habit），供开发者面板查看，
不直接改变桌宠行为：观察结果先当推测，绝不变成绝对事实。
"""

import json
import time

# 少于这么多样本就不下结论。
MIN_SAMPLES = 20
_PREF_KEY = "habit.inferred"

# 反馈里的语气词，用来估个主动程度建议。
_NEGATIVE = (
    "别烦", "闭嘴", "不用", "不要", "别说", "少说", "以后别", "太吵", "烦人", "安静点", "讨厌",
)
_POSITIVE = ("多说", "继续", "喜欢", "这个好", "可以多", "再说说", "很好", "谢谢")


def _tone(text):
    if any(word in text for word in _NEGATIVE):
        return "negative"
    if any(word in text for word in _POSITIVE):
        return "positive"
    return "neutral"


class HabitModel:
    """从真实数据推断习惯。数据库不可用时返回空结果，不影响桌宠运行。"""

    def __init__(
        self, database=None, preferences=None, clock=time.time, min_samples=MIN_SAMPLES
    ):
        self.database = database
        self.preferences = preferences
        self.clock = clock
        self.min_samples = max(1, int(min_samples))

    # ---------- 观察 ----------
    def observe(self):
        """按小时统计消息条数，从多到少。只读时间戳，不读内容。"""
        rows = self._query(
            "SELECT CAST(strftime('%H', created_at, 'localtime') AS INTEGER) AS hour,"
            " COUNT(*) AS n FROM messages GROUP BY hour ORDER BY n DESC, hour"
        )
        return [row for row in rows if row.get("hour") is not None]

    # ---------- 推断 ----------
    def refresh(self):
        """重新推断并记下结果。样本不够就明确说还看不出来。"""
        rows = self.observe()
        total = sum(int(row["n"] or 0) for row in rows)
        if total < self.min_samples:
            result = {"samples": total, "active_hours": [], "confidence": 0.0}
        else:
            hours = sorted(int(row["hour"]) for row in rows[:3])
            confidence = round(min(1.0, total / float(self.min_samples * 5)), 2)
            result = {"samples": total, "active_hours": hours, "confidence": confidence}
        self._store(result)
        return result

    def inferred(self):
        """读取上一次推断结果。没推断过或读不出来就返回空。"""
        empty = {"samples": 0, "active_hours": [], "confidence": 0.0}
        raw = self._preference(_PREF_KEY)
        if not raw:
            return empty
        try:
            data = json.loads(raw)
        except Exception:
            return empty
        return data if isinstance(data, dict) else empty

    def active_hours(self, top=3):
        hours = list(self.inferred().get("active_hours") or [])
        return hours[: max(1, int(top))]

    def note(self):
        """一句给上下文用的话。样本不够就返回空字符串。"""
        hours = self.active_hours()
        if not hours:
            return ""
        return f"你一般在 {hours[0]}-{hours[-1]} 点比较活跃（推测）。"

    def proactiveness(self):
        """从真实反馈推断该多主动。只做建议，不自动改行为。"""
        rows = self._query(
            "SELECT content FROM memories WHERE kind = 'feedback' ORDER BY id DESC LIMIT 50"
        )
        negative = sum(1 for row in rows if _tone(str(row.get("content") or "")) == "negative")
        positive = sum(1 for row in rows if _tone(str(row.get("content") or "")) == "positive")
        if negative > positive:
            level = "low"
        elif positive > negative and positive >= 3:
            level = "high"
        else:
            level = "normal"
        return {
            "level": level,
            "feedback_total": len(rows),
            "negative": negative,
            "positive": positive,
            "applied": False,
        }

    def describe(self):
        """开发者面板用的只读视图。绝不发给模型。"""
        data = self.inferred()
        return {
            "samples": data.get("samples", 0),
            "active_hours": data.get("active_hours", []),
            "confidence": data.get("confidence", 0.0),
            "note": self.note(),
            "proactiveness": self.proactiveness(),
        }

    # ---------- 内部 ----------
    def _store(self, result):
        if self.preferences is None:
            return False
        try:
            self.preferences.set_preference(
                _PREF_KEY, json.dumps(result, ensure_ascii=False)
            )
            return True
        except Exception:
            return False

    def _preference(self, key):
        if self.preferences is None:
            return None
        try:
            return self.preferences.get_preference(key)
        except Exception:
            return None

    def _query(self, sql):
        if self.database is None:
            return []
        try:
            return self.database.query(sql)
        except Exception:
            return []
