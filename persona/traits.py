"""基础人格模块与强度。人格只影响「怎么说」，不影响「能不能做」。"""

import json

TRAITS = {
    "温柔": "先照顾对方情绪，说话体贴",
    "理性": "先讲清事实和理由，再给结论",
    "活泼": "语气轻快，愿意多说话",
    "安静": "话少，只在该开口的时候说",
    "幽默": "偶尔开个小玩笑，也会自嘲",
    "好奇": "对用户在做什么感兴趣，会多问一句",
    "认真": "答应的事跟到底，不敷衍",
    "独立": "有自己的看法，会直接说不同意见",
    "傲娇": "嘴上不认，实际会照做",
}

DEFAULT_INTENSITY = 50
MIN_ACTIVE = 20
MAX_ACTIVE = 4


def normalize(raw):
    """只保留已知性格，强度限制在 0-100。"""
    level_map = {}
    for name, value in dict(raw or {}).items():
        key = str(name).strip()
        if key not in TRAITS:
            continue
        try:
            level = int(float(value))
        except (TypeError, ValueError):
            continue
        level_map[key] = max(0, min(100, level))
    return level_map


def parse(text):
    """解析「理性:70,温柔:50」这样的配置文本。"""
    raw = {}
    for chunk in str(text or "").replace("，", ",").split(","):
        name, sep, value = chunk.partition(":")
        if not sep:
            name, sep, value = chunk.partition("：")
        if sep:
            raw[name.strip()] = value.strip()
    return normalize(raw)


def active(traits, floor=MIN_ACTIVE):
    """生效的性格，强度高的排前面。"""
    items = [(name, level) for name, level in normalize(traits).items() if level >= floor]
    items.sort(key=lambda item: (-item[1], item[0]))
    return items


def describe(traits, limit=MAX_ACTIVE):
    """把性格写成一小段给模型看的话。强度低的不写，省 token。"""
    items = active(traits)[: max(0, int(limit))]
    if not items:
        return ""
    head = "性格：" + "、".join(f"{name} {level}" for name, level in items) + "。"
    return head + "；".join(TRAITS[name] for name, _ in items) + "。"


# 用户调过的性格强度存在偏好里，重启后照旧生效；这是内部设置，不进模型。
PREFERENCE_KEY = "persona.traits"


def load(preferences):
    """读回上次保存的性格强度。没存过或读不出来就返回空，交给调用方用默认值。"""
    if preferences is None:
        return {}
    try:
        raw = preferences.get_preference(PREFERENCE_KEY)
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return normalize(data if isinstance(data, dict) else {})


def save(preferences, traits):
    """保存性格强度。保存失败返回 False，界面据此如实提示。"""
    if preferences is None:
        return False
    try:
        preferences.set_preference(
            PREFERENCE_KEY, json.dumps(normalize(traits), ensure_ascii=False)
        )
        return True
    except Exception:
        return False


# 会互相打架的性格对：(一, 二, 为什么)。
CONFLICTS = (
    ("活泼", "安静", "一个愿意多说，一个话少，同一段话里只能偏一边"),
    ("幽默", "认真", "开玩笑和跟到底，口气不好同时兼顾"),
    ("温柔", "独立", "先照顾情绪和直接说不同意见，口气会互相抵消"),
)


def conflicts(traits, floor=MIN_ACTIVE):
    """同时生效但风格相反的性格。只提示，不替用户改。"""
    names = {name for name, _ in active(traits, floor)}
    return [
        (left, right, why)
        for left, right, why in CONFLICTS
        if left in names and right in names
    ]


def conflict_note(traits):
    """给界面用的一句话。没有冲突就返回空串。"""
    rows = conflicts(traits)
    if not rows:
        return ""
    parts = [f"{left} 与 {right}（{why}）" for left, right, why in rows]
    return "性格里有互相打架的组合：" + "；".join(parts) + "。"
