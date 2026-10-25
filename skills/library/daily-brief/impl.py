"""Daily Brief：今天的简报。

全靠本地工具和已有服务拼装，不需要模型。哪一项读不到就跳过并记下来，
不会为了好看编一条数据。
"""


def _read(ctx, name, **arguments):
    """读一项。失败返回 (None, 原因)，不让整份简报挂掉。"""
    try:
        outcome = ctx.tool(name, **arguments)
    except Exception as exc:
        return None, str(exc)
    if not getattr(outcome, "ok", False):
        return None, str(getattr(outcome, "error", "") or "读不到")
    text = str(getattr(outcome, "result", "") or "").strip()
    return (text or None), ""


def run(ctx):
    lines = []
    missing = []

    text, why = _read(ctx, "local_time", what="date")
    if text:
        lines.append(text)
    else:
        missing.append("日期（%s）" % why)

    text, why = _read(ctx, "todo_list", limit=5)
    if text:
        lines.append("待办：" + text)
    else:
        missing.append("待办（%s）" % why)

    text, why = _read(ctx, "list_calendar_events", limit=5)
    if text:
        lines.append("日程：" + text)
    else:
        missing.append("日程（%s）" % why)

    city = str(ctx.inputs.get("city") or "").strip()
    text, why = _read(ctx, "get_weather", city=city)
    if text:
        lines.append("天气：" + text)
    else:
        missing.append("天气（%s）" % why)

    text, why = _read(ctx, "system_status")
    if text:
        lines.append("机器：" + text)
    else:
        missing.append("机器状态（%s）" % why)

    if not lines:
        return {
            "ok": False,
            "error": "今天的几项读数一项都没拿到，简报做不出来。",
            "detail": "；".join(missing),
        }

    detail = "全部来自本机工具和已有服务，没有让模型编内容。"
    if missing:
        detail = detail + " 没读到的：" + "；".join(missing) + "。"

    return {
        "ok": True,
        "summary": "\n".join(lines),
        "detail": detail,
        "verified": True,
    }
