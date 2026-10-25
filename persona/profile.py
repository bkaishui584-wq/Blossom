"""人格文本：用户自己写的人格说明，以及草稿、版本与生效范围。

原文怎么写的就怎么存，程序不替用户改写。发给模型时按预算截断，
截断只影响那一轮的提示词，不影响存下来的文本，也绝不挤掉安全边界。
所有内容都放在内部偏好里（SQLite），属于用户数据，不进代码、不进日志。
"""

import json
import re
import time

from persona.interface import build_persona_note

TEXT_KEY = "persona.text"
DRAFT_KEY = "persona.text.draft"
VERSIONS_KEY = "persona.versions"
BUDGET_KEY = "persona.note.budget"
# 名字单存一条：导入角色卡时它跟文本一起换，界面上也能单独改回来。
NAME_KEY = "persona.name"

# 存得下长文本，但也不是无上限，免得一条偏好把库撑爆。
MAX_TEXT_CHARS = 4000
VERSION_LIMIT = 20
MAX_NAME_CHARS = 24

# 每轮真正发给模型的上限。人格越长，每轮花的 token 越多，所以要能调。
DEFAULT_NOTE_BUDGET = 900
MIN_NOTE_BUDGET = 300
MAX_NOTE_BUDGET = 3000

# 起手模板。用户想用就用，不想用就直接写，没有强制。
TEMPLATES = (
    (
        "陪伴型",
        "你叫小蝶。说话轻松，句子短，偶尔调侃一句，但不说空话。"
        "用户累了会提醒他歇一会儿，用户忙久了会问一句进度。"
        "有自己的看法，不同意会直接说，说完还是照用户的决定做。",
    ),
    (
        "助手型",
        "你是一个做事利落的助手。先给结论，再给理由，能一次说清就不分两次。"
        "不确定的地方直说不确定，不猜。做完一件事主动说做到了哪一步。",
    ),
    (
        "学习搭子",
        "你陪用户学东西。先问他已经懂到哪一步，再决定从哪里讲起。"
        "多用小例子，一次只讲一个点，讲完问他懂没懂，不一口气灌完。",
    ),
)
TEMPLATE_NAMES = tuple(name for name, _ in TEMPLATES)


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


def _write(preferences, key, value):
    if preferences is None:
        return False
    try:
        preferences.set_preference(key, json.dumps(value, ensure_ascii=False))
        return True
    except Exception:
        return False


def clean(body):
    """只去首尾空白和控制字符，不改用户写的字。超长就截到上限。"""
    text = str(body or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.strip()
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
    return text


def clean_name(value):
    """名字只留一行、去掉控制字符。空字符串表示"没设过"。"""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(r"[\x00-\x1f\x7f]", "", text)
    return text[:MAX_NAME_CHARS]


def name(preferences, default=""):
    """生效的名字：用户或角色卡设过的优先，否则用传进来的默认值（配置里的名字）。"""
    stored = clean_name(_read(preferences, NAME_KEY, ""))
    return stored or clean_name(default)


def set_name(preferences, value):
    """设置名字并返回生效值。空名字拒绝，避免把桌宠改成无名氏。"""
    cleaned = clean_name(value)
    if not cleaned:
        return {"ok": False, "error": "名字不能是空的"}
    if not _write(preferences, NAME_KEY, cleaned):
        return {"ok": False, "error": "名字没能写进去"}
    return {"ok": True, "name": cleaned}


def clear_name(preferences):
    """抹掉单独设的名字，回到配置里的默认名字。"""
    if preferences is None:
        return False
    return _write(preferences, NAME_KEY, "")


def text(preferences):
    """当前生效的人格文本。没写过就是空串。"""
    return clean(_read(preferences, TEXT_KEY, ""))


def draft(preferences):
    """编辑器里的草稿。没存过草稿就返回生效文本。"""
    body = clean(_read(preferences, DRAFT_KEY, ""))
    if body:
        return body
    return text(preferences)


def is_dirty(preferences):
    """草稿和生效文本不一样，说明有没应用的改动。"""
    return draft(preferences) != text(preferences)


def budget(preferences):
    """每轮发给模型的人格文本上限，单位字符。"""
    raw = _read(preferences, BUDGET_KEY, DEFAULT_NOTE_BUDGET)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_NOTE_BUDGET
    return max(MIN_NOTE_BUDGET, min(MAX_NOTE_BUDGET, value))


def set_budget(preferences, value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return budget(preferences)
    value = max(MIN_NOTE_BUDGET, min(MAX_NOTE_BUDGET, value))
    if not _write(preferences, BUDGET_KEY, value):
        return budget(preferences)
    return value


def save_draft(preferences, body):
    """只存草稿，不生成版本、不影响当前生效的人格。"""
    cleaned = clean(body)
    if not _write(preferences, DRAFT_KEY, cleaned):
        return {"ok": False, "error": "草稿没能写进去"}
    return {"ok": True, "chars": len(cleaned)}


def versions(preferences, limit=VERSION_LIMIT):
    """历史版本，新的在前。坏记录直接跳过。"""
    raw = _read(preferences, VERSIONS_KEY, [])
    if not isinstance(raw, list):
        return []
    rows = [row for row in raw if isinstance(row, dict) and isinstance(row.get("text"), str)]
    rows.sort(key=lambda row: row.get("v", 0), reverse=True)
    try:
        count = max(1, int(limit))
    except (TypeError, ValueError):
        count = VERSION_LIMIT
    return rows[:count]


def version(preferences):
    """当前版本号。没存过就是 0。"""
    rows = versions(preferences, limit=VERSION_LIMIT)
    if not rows:
        return 0
    try:
        return max(0, int(rows[0].get("v", 0)))
    except (TypeError, ValueError):
        return 0


def version_text(preferences, number):
    """按版本号取回原文。找不到返回空串。"""
    try:
        wanted = int(number)
    except (TypeError, ValueError):
        return ""
    for row in versions(preferences, limit=VERSION_LIMIT):
        try:
            if int(row.get("v", 0)) == wanted:
                return str(row.get("text") or "")
        except (TypeError, ValueError):
            continue
    return ""


def save_text(preferences, body, note=""):
    """保存并生效：写正文、存草稿、留一个版本快照。

    内容没变就不重复留版本，也不推进版本号。
    """
    cleaned = clean(body)
    if not cleaned:
        return {"ok": False, "error": "人格文本是空的，写点东西再保存"}
    current = text(preferences)
    if cleaned == current:
        save_draft(preferences, cleaned)
        return {"ok": True, "changed": False, "version": version(preferences),
                "chars": len(cleaned)}
    rows = _read(preferences, VERSIONS_KEY, [])
    if not isinstance(rows, list):
        rows = []
    entry = {
        "v": version(preferences) + 1,
        "t": time.strftime("%Y-%m-%d %H:%M:%S"),
        "chars": len(cleaned),
        "note": str(note or ""),
        "text": cleaned,
    }
    rows.append(entry)
    rows = [row for row in rows if isinstance(row, dict)][-VERSION_LIMIT:]
    if not _write(preferences, TEXT_KEY, cleaned):
        return {"ok": False, "error": "人格文本没能写进去"}
    _write(preferences, DRAFT_KEY, cleaned)
    _write(preferences, VERSIONS_KEY, rows)
    return {
        "ok": True,
        "changed": True,
        "version": entry["v"],
        "chars": entry["chars"],
        "time": entry["t"],
    }


def revert_to(preferences, number):
    """把历史版本写回生效位置，并在版本表里留下这一次回滚。

    不回滚就悄悄改掉用户现在写的东西，所以这一步要用户自己点。
    """
    body = version_text(preferences, number)
    if not body:
        return {"ok": False, "error": "找不到这个版本"}
    result = save_text(preferences, body, note="回滚到版本 %s" % number)
    if result.get("ok"):
        result["restored"] = body
    return result


def delete_all(preferences):
    """清掉草稿和版本记录，生效文本也一起清空。只动这几条内部偏好。"""
    if preferences is None:
        return False
    for key in (TEXT_KEY, DRAFT_KEY, VERSIONS_KEY):
        if not _write(preferences, key, ""):
            return False
    return True


def holder(persona):
    """找到真正存人格文本的对象。角色包只是壳，文本永远跟着默认人格。"""
    seen = set()
    node = persona
    while node is not None and id(node) not in seen:
        if hasattr(node, "set_note"):
            return node
        seen.add(id(node))
        node = getattr(node, "default", None)
    return None


def apply_to(persona, preferences, default_name=""):
    """把存好的人格文本推给当前人格对象，下次对话就按它来。

    拿不到能设置的对象就如实返回 ok=False，不假装已经生效。
    default_name 是配置里的名字：用户清掉单独设的名字时，回到这一层。
    """
    target = holder(persona)
    if target is None:
        return {"ok": False, "error": "当前人格不支持自定义文本"}
    body = text(preferences)
    habits = {}
    try:
        from persona import habits as habits_module

        habits = habits_module.load(preferences)
    except Exception:
        habits = {}
    applied = False
    try:
        target.set_note(body)
        applied = True
    except Exception as exc:
        return {"ok": False, "error": "设置人格文本失败：%s" % exc}
    stored_name = clean_name(_read(preferences, NAME_KEY, ""))
    effective_name = stored_name or clean_name(default_name)
    if effective_name:
        try:
            target.name = effective_name
        except Exception:
            # 改不了名字不算调用失败：文本已经生效，名字保持原样。
            pass
    setter = getattr(target, "set_habits", None)
    if setter is not None:
        try:
            setter(habits)
        except Exception:
            applied = applied and True
    try:
        target.note_budget = budget(preferences)
    except Exception:
        pass
    return {
        "ok": applied,
        "name": clean_name(getattr(target, "name", "")) or effective_name,
        "chars": len(body),
        "habits": len([value for value in habits.values() if value]),
        "budget": budget(preferences),
    }


def describe(preferences, persona=None):
    """界面用的一行摘要。sent 是这一轮真正会发出去的字数，拿不到就不猜。"""
    body = text(preferences)
    rows = versions(preferences, limit=VERSION_LIMIT)
    info = {
        "name": name(preferences),
        "chars": len(body),
        "version": version(preferences),
        "versions": len(rows),
        "budget": budget(preferences),
        "dirty": is_dirty(preferences),
        "sent": None,
    }
    if persona is not None:
        try:
            info["sent"] = len(build_persona_note(persona))
        except Exception:
            info["sent"] = None
    return info


def templates():
    """起手模板，(名字, 正文)。

    角色卡不放在这里：它们有自己的「角色卡」页，能预览、应用、删除，
    还能从文件导入。模板只留三段短的人格草稿。
    """
    return tuple((str(item_name), str(body)) for item_name, body in TEMPLATES)
