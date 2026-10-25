"""安全工具直连：只读、不用参数、能自己说人话的工具，由程序直接跑。

省掉「模型先决定要不要调工具」这一轮；没有模型时也能如实回答。
高风险工具走不到这里（等级过滤 + 再校验一次）。
"""

import json

from tools.permission import SAFE_LEVELS, normalize_level


def candidate(registry, text):
    """挑出唯一一个可以直接跑的只读工具；不唯一或不合格就返回 None。"""
    if registry is None:
        return None
    try:
        names = registry.relevant(text, levels=SAFE_LEVELS)
    except Exception:
        return None
    if len(names) != 1:
        return None
    tool = registry.get(names[0])
    if tool is None or not callable(getattr(tool, "summarize", None)):
        return None
    if normalize_level(getattr(tool, "permission", "")) not in SAFE_LEVELS:
        return None
    if (getattr(tool, "schema", None) or {}).get("required"):
        # 缺参数的工具得让模型去理解用户的话，不能瞎填。
        return None
    return tool


def note(tool, outcome):
    """把真实结果写成一句话；失败就如实说失败，绝不编造。"""
    if not outcome.ok:
        # 执行层会给失败加「执行失败：」前缀，这里直接说原因，别叠两层。
        return (outcome.error or "这个查询没成功。").removeprefix("执行失败：")
    payload = outcome.result
    if isinstance(payload, str):
        # 执行层的 result 是给模型看的 JSON 文本，这里解回来给人话函数用。
        try:
            payload = json.loads(payload)
        except ValueError:
            pass
    try:
        text = tool.summarize(payload)
    except Exception:
        return None
    text = (text or "").strip()
    return text or None
