"""项目记忆：把「正在做的事」存成结构化状态。

长期记忆回答「发生过什么」，项目记忆回答「现在做到哪一步」。两者分开：
项目状态存进 preferences 表里的一条 JSON（键 `project.<名字>`），不动表结构，
旧数据库照常能用，打包升级也不会丢。

字段固定：目标、当前进度、已完成、待办、阻塞、状态、最后更新时间。
没给的值一律留空，绝不编造进度。
"""

import json
import re
from datetime import datetime, timezone

PREFIX = "project."
KEY_PREFIX = PREFIX + "state."

# 一条项目状态最多记这么多条明细，防止单个项目把记忆刷爆。
MAX_ITEMS = 12
MAX_TEXT = 200
MAX_SLUG = 40
# 渲染进上下文时最多带几个项目：只带最近更新的。
CONTEXT_LIMIT = 3

_SLUG_KEEP = re.compile(r"[\w\u4e00-\u9fff-]+")


def slug(name):
    """项目名变成稳定的键。中文原样保留，只去掉空白与标点。"""
    parts = _SLUG_KEEP.findall(str(name or "").strip().lower())
    return "-".join(parts)[:MAX_SLUG]


def _clean(text):
    return " ".join(str(text or "").split())[:MAX_TEXT]


def _clean_items(values):
    """明细去重、去空、限量。传字符串时当成单条，不当成字符序列。"""
    if isinstance(values, str):
        values = [values]
    out = []
    for item in values or ():
        text = _clean(item)
        if text and text not in out:
            out.append(text)
    return out[:MAX_ITEMS]


def _merge(old, new):
    """新增的并进去，旧的不丢。用户没提过的事不会被覆盖掉。"""
    merged = list(old or [])
    for item in new or ():
        if item not in merged:
            merged.append(item)
    return merged[:MAX_ITEMS]


class ProjectMemory:
    """项目状态读写。读不到、写不进都只是"没有"，不抛异常、不假装成功。"""

    def __init__(self, preferences=None, logger=None, clock=None):
        self.preferences = preferences
        self.logger = logger
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # ---------- 写 ----------
    def update(
        self,
        name,
        goal=None,
        progress=None,
        done=None,
        todo=None,
        blockers=None,
        state=None,
    ):
        """记一次项目状态。只有明确给到的字段才会改，其余保持原样。"""
        key = slug(name)
        if not key:
            return None
        current = self.get(key) or {
            "name": _clean(name),
            "goal": "",
            "progress": "",
            "done": [],
            "todo": [],
            "blockers": [],
            "state": "",
        }
        if goal is not None:
            current["goal"] = _clean(goal)
        if progress is not None:
            current["progress"] = _clean(progress)
        if state is not None:
            current["state"] = _clean(state)
        current["done"] = _merge(current.get("done"), _clean_items(done))
        current["todo"] = _clean_items(todo) if todo is not None else list(current.get("todo") or [])
        # 阻塞是当下的情况：给新的就整体替换，不给就保持原样。
        if blockers is not None:
            current["blockers"] = _clean_items(blockers)
        current["name"] = _clean(name) or current.get("name", key)
        current["updated_at"] = self._clock().isoformat(timespec="seconds")
        return self._save(key, current)

    def forget(self, name):
        """撤掉一个项目的状态。没有确认通道，只给界面按钮调用。"""
        key = slug(name)
        if not key or self.preferences is None:
            return False
        try:
            self.preferences.set_preference(self._pref_key(key), "")
            return True
        except Exception as exc:
            self._warn("project forget failed: %s", exc)
            return False

    # ---------- 读 ----------
    def get(self, name):
        key = slug(name)
        if not key or self.preferences is None:
            return None
        try:
            raw = self.preferences.get_preference(self._pref_key(key))
        except Exception as exc:
            self._warn("project read failed: %s", exc)
            return None
        return self._decode(raw)

    def all(self, limit=20):
        """所有项目状态，按最后更新时间倒序。坏掉的条目直接跳过。"""
        if self.preferences is None:
            return []
        try:
            rows = list(self.preferences.all_preferences() or [])
        except Exception as exc:
            self._warn("project list failed: %s", exc)
            return []
        items = []
        for row in rows:
            key = str(row.get("key", ""))
            if not key.startswith(KEY_PREFIX):
                continue
            data = self._decode(row.get("value"))
            if data is not None:
                data["key"] = key
                items.append(data)
        items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return items[: max(1, int(limit))]

    def context_lines(self, limit=CONTEXT_LIMIT):
        """给模型看的项目状态。只写真实字段，没有的字段不出现。"""
        lines = []
        for item in self.all(limit=limit):
            head = item.get("goal") or item.get("name") or ""
            if not head:
                continue
            parts = []
            if item.get("state"):
                parts.append("状态：%s" % item["state"])
            if item.get("progress"):
                parts.append("进度：%s" % item["progress"])
            if item.get("todo"):
                parts.append("待办：%s" % "；".join(item["todo"]))
            if item.get("blockers"):
                parts.append("阻塞：%s" % "；".join(item["blockers"]))
            detail = "，".join(parts) if parts else "暂无新进展"
            lines.append("- 在做的事「%s」：%s" % (_clean(head), detail))
        return lines

    def rows(self):
        """设置与记忆界面用的只读行。"""
        rows = []
        for item in self.all(limit=20):
            rows.append(
                {
                    "name": item.get("name") or item.get("key", ""),
                    "goal": item.get("goal", ""),
                    "state": item.get("state", ""),
                    "progress": item.get("progress", ""),
                    "done": list(item.get("done") or []),
                    "todo": list(item.get("todo") or []),
                    "blockers": list(item.get("blockers") or []),
                    "updated_at": item.get("updated_at", ""),
                }
            )
        return rows

    # ---------- 内部 ----------
    @staticmethod
    def _pref_key(key):
        return KEY_PREFIX + key

    def _save(self, key, data):
        if self.preferences is None:
            return None
        try:
            self.preferences.set_preference(
                self._pref_key(key), json.dumps(data, ensure_ascii=False)
            )
        except Exception as exc:
            self._warn("project write failed: %s", exc)
            return None
        return data

    @staticmethod
    def _decode(raw):
        text = str(raw or "").strip()
        if not text:
            return None
        try:
            data = json.loads(text)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)