"""人格改动记录：版本号、历史与撤销。

每次改性格强度或恢复默认都记一笔；撤销就是把上一次的强度写回去，再记一笔。
记录存在内部偏好里（persona.history / persona.version），重启之后还在，不发给模型。
"""

import json
import time

from persona import traits as traits_module

HISTORY_KEY = "persona.history"
VERSION_KEY = "persona.version"
MAX_ROWS = 50


def _read(preferences, key, default):
    if preferences is None:
        return default
    try:
        raw = preferences.get_preference(key)
    except Exception:
        return default
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def version(preferences):
    """人格改过几次。读不出来就按 0 算。"""
    try:
        return max(0, int(_read(preferences, VERSION_KEY, 0) or 0))
    except (TypeError, ValueError):
        return 0


def rows(preferences, limit=20):
    """历史记录，最新的在前。坏记录直接跳过。"""
    raw = _read(preferences, HISTORY_KEY, [])
    if not isinstance(raw, list):
        return []
    cleaned = [row for row in raw if isinstance(row, dict)]
    cleaned.sort(key=lambda row: row.get("v", 0), reverse=True)
    try:
        count = max(1, int(limit))
    except (TypeError, ValueError):
        count = 20
    return cleaned[:count]


def record(preferences, before, after, note=""):
    """记一笔改动并推进版本号。存不下来返回空字典，界面据此如实提示。"""
    if preferences is None:
        return {}
    current = version(preferences)
    entry = {
        "v": current + 1,
        "t": time.strftime("%Y-%m-%d %H:%M:%S"),
        "kind": "traits",
        "before": traits_module.normalize(before),
        "after": traits_module.normalize(after),
        "note": str(note or ""),
    }
    history = _read(preferences, HISTORY_KEY, [])
    if not isinstance(history, list):
        history = []
    history.append(entry)
    history = history[-MAX_ROWS:]
    try:
        preferences.set_preference(VERSION_KEY, str(entry["v"]))
        preferences.set_preference(
            HISTORY_KEY, json.dumps(history, ensure_ascii=False)
        )
    except Exception:
        return {}
    return entry


def undo(preferences):
    """撤回最近一次改动，返回被撤回的那笔（含改之前的设定）。"""
    if preferences is None:
        return {"ok": False, "error": "记忆不可用，撤不了"}
    history = _read(preferences, HISTORY_KEY, [])
    if not isinstance(history, list) or not history:
        return {"ok": False, "error": "还没有改过，没有可撤销的"}
    entry = history.pop()
    try:
        preferences.set_preference(
            HISTORY_KEY, json.dumps(history, ensure_ascii=False)
        )
        preferences.set_preference(VERSION_KEY, str(max(0, int(entry.get("v", 1)) - 1)))
    except Exception as exc:
        return {"ok": False, "error": f"撤销没写成：{exc}"}
    return {
        "ok": True,
        "v": entry.get("v"),
        "t": entry.get("t"),
        "restored": entry.get("before") or {},
        "was": entry.get("after") or {},
    }


def clear(preferences):
    """清空历史并归零版本号。只动这两条内部偏好。"""
    if preferences is None:
        return False
    try:
        preferences.set_preference(HISTORY_KEY, "[]")
        preferences.set_preference(VERSION_KEY, "0")
    except Exception:
        return False
    return True