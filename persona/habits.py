"""用户习惯：用户明确设置的称呼、回复长度、解释深度这些。

显式设置永远优先于系统推断。这里只负责存和读，以及把它们写成几句短提示。
习惯改不了安全规则，也不会给自己加权限。
"""

import json

SPEC = (
    ("address", "怎么称呼你", "text", (), "留空就正常说话，不硬加称呼"),
    ("length", "回复长度", "choice", ("", "简短", "适中", "详细"), "管长短，不管内容"),
    ("depth", "解释深度", "choice", ("", "只给结论", "说清理由", "从头讲起"), "管讲多深"),
    ("tone", "语言风格", "choice", ("", "随意", "专业", "轻松幽默", "温和耐心"), "管口气"),
    ("remind", "提醒频率", "choice", ("", "很少", "正常", "多一点"), "和免打扰时段一起生效"),
    ("proactive", "主动程度", "choice", ("", "不主动", "偶尔主动", "经常主动"), "管它会不会先开口"),
    ("collab", "协作方式", "choice", ("", "先问清再做", "直接给方案", "边做边报进度"), "管怎么配合你"),
    ("avoid", "不喜欢的行为", "text", (), "例如：不要用表格、不要一次列很多条"),
)

FIELDS = tuple(item[0] for item in SPEC)
LABELS = {item[0]: item[1] for item in SPEC}
HINTS = {item[0]: item[4] for item in SPEC}
KINDS = {item[0]: item[2] for item in SPEC}
CHOICES = {item[0]: tuple(item[3]) for item in SPEC}

MAX_VALUE_CHARS = 60
PREFERENCE_KEY = "persona.habits"
LEARNING_KEY = "persona.habits.learning"

# 这些字段会进提示词，写成一句短话；太长的截断，免得挤掉别的内容。
_LINE_TEMPLATES = {
    "address": "称呼用户为「%s」。",
    "length": "回复长度：%s。",
    "depth": "解释深度：%s。",
    "tone": "语言风格：%s。",
    "remind": "提醒频率：%s。",
    "proactive": "主动程度：%s。",
    "collab": "协作方式：%s。",
    "avoid": "用户不喜欢：%s。",
}


def empty():
    """所有字段都空的一份设置。"""
    return {name: "" for name in FIELDS}


def normalize(raw):
    """只留下认识的字段与合法的取值。空值一律丢掉。"""
    out = empty()
    if not isinstance(raw, dict):
        return out
    for name in FIELDS:
        value = " ".join(str(raw.get(name) or "").split())[:MAX_VALUE_CHARS]
        if not value:
            continue
        allowed = CHOICES[name]
        if allowed and value not in allowed:
            continue
        out[name] = value
    return out


def load(preferences):
    """读回用户设置。没设过或读不出来就是全空，不影响对话。"""
    if preferences is None:
        return empty()
    try:
        raw = preferences.get_preference(PREFERENCE_KEY)
    except Exception:
        return empty()
    if not raw:
        return empty()
    try:
        data = json.loads(raw)
    except Exception:
        return empty()
    return normalize(data)


def save(preferences, values):
    """保存用户设置。保存失败返回失败，界面据此如实提示。"""
    cleaned = normalize(values)
    if preferences is None:
        return {"ok": False, "error": "记忆不可用，存不了", "values": cleaned}
    try:
        preferences.set_preference(
            PREFERENCE_KEY, json.dumps(cleaned, ensure_ascii=False)
        )
    except Exception as exc:
        return {"ok": False, "error": "保存失败：%s" % exc, "values": cleaned}
    return {"ok": True, "values": cleaned}


def reset(preferences):
    """清空用户设置，回到不设任何习惯。"""
    return save(preferences, empty())


def learning_enabled(preferences):
    """习惯学习开关。默认开着；用户关掉就一直是关的。"""
    if preferences is None:
        return False
    try:
        raw = preferences.get_preference(LEARNING_KEY)
    except Exception:
        return False
    if raw is None or raw == "":
        return True
    return str(raw).strip() != "0"


def set_learning_enabled(preferences, flag):
    if preferences is None:
        return False
    try:
        preferences.set_preference(LEARNING_KEY, "1" if flag else "0")
    except Exception:
        return False
    return True


def lines(values, limit=8):
    """把用户设置写成给模型看的短句。空字段不写，省 token。"""
    cleaned = normalize(values)
    out = []
    for name in FIELDS:
        value = cleaned.get(name)
        if not value:
            continue
        template = _LINE_TEMPLATES.get(name)
        if not template:
            continue
        out.append(template % value)
        if len(out) >= limit:
            break
    return out


def summary(values):
    """界面用的一行话。没设置就说没设置，不编。"""
    cleaned = normalize(values)
    parts = ["%s：%s" % (LABELS[name], cleaned[name]) for name in FIELDS if cleaned.get(name)]
    return "；".join(parts) if parts else "还没有设置习惯"
