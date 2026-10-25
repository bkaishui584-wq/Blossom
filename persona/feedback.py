"""人格反馈：喜欢 / 不喜欢 / 以后这样 / 以后不要这样。

只把用户说过的话整理成短句交给人格。反馈改不了安全规则，也改不了权限判断。
"""

KINDS = ("like", "keep", "dislike", "stop")

LABELS = {
    "like": "喜欢这样",
    "keep": "以后就这样",
    "dislike": "不喜欢这样",
    "stop": "以后不要这样",
}

# 只有说到「桌宠的说话方式」才算人格反馈，普通喜好不算。
_MANNER = (
    "语气", "说话", "表达", "风格", "态度", "性格", "啰嗦", "太长", "句子",
    "这么正式", "这么严肃", "这么冷淡", "这么客气",
)
_SIGNALS = (
    ("stop", ("以后别", "以后不要", "别再", "别用", "少用", "不用这么", "不要这么")),
    ("keep", ("以后这样", "以后就", "保持", "就这样说", "继续说")),
    ("dislike", ("不喜欢", "讨厌", "烦", "受不了")),
    ("like", ("喜欢", "爱听", "好听")),
)

_VERB_PREFIX = ("记住", "记一下", "记下来", "别忘了", "别忘", "记着")


def classify(text):
    """判断这句话是不是人格反馈，返回 like / keep / dislike / stop。"""
    body = str(text or "")
    if not any(word in body for word in _MANNER):
        return None
    for kind, words in _SIGNALS:
        if any(word in body for word in words):
            return kind
    return None


def tidy(text, clip=40):
    """去掉「记住」这类前缀，留一句读得通的话。"""
    body = " ".join(str(text or "").split())
    for word in _VERB_PREFIX:
        if body.startswith(word):
            body = body[len(word) :].lstrip("，,：: ")
    return body[:clip]


def organize(rows, limit=4, clip=40):
    """把真实反馈整理成两组短句，不编造。"""
    keep, avoid = [], []
    for row in rows or ():
        content = row.get("content") if isinstance(row, dict) else row
        text = tidy(content, clip)
        if not text:
            continue
        kind = classify(content)
        if kind in ("like", "keep"):
            keep.append(text)
        elif kind in ("dislike", "stop"):
            avoid.append(text)
    return {"keep": _unique(keep)[:limit], "avoid": _unique(avoid)[:limit]}


def lines(buckets, limit=4):
    """把整理结果写成两句给模型看的话。"""
    buckets = buckets or {}
    out = []
    keep = list(buckets.get("keep") or ())[:limit]
    avoid = list(buckets.get("avoid") or ())[:limit]
    if keep:
        out.append("用户喜欢的表达：" + "；".join(keep))
    if avoid:
        out.append("用户不喜欢的表达：" + "；".join(avoid))
    return out


def _unique(items):
    seen, out = set(), []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
