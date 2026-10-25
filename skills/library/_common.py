"""Skill 实现共用的小工具。只做数据整形，不做任何动作。"""

import json

from skills.runtime import SkillError


def payload(outcome):
    """把工具结果解成 python 对象。解不出来就原样返回。"""
    text = getattr(outcome, "result", None)
    if not isinstance(text, str):
        return text
    try:
        return json.loads(text)
    except ValueError:
        return text


def need(outcome, what):
    """工具失败直接抛错。半成品不能当结果。"""
    if not getattr(outcome, "ok", False):
        raise SkillError("%s没成功：%s" % (what, getattr(outcome, "error", "") or "未知原因"))
    return payload(outcome)


def mapping(value):
    """把 payload 结果当字典用。不是字典就返回空字典，不让 Skill 崩掉。"""
    return value if isinstance(value, dict) else {}


def as_dict(value, key):
    if isinstance(value, dict):
        return value.get(key)
    return None


def clip(text, limit=600):
    return str(text or "").strip()[:limit]
