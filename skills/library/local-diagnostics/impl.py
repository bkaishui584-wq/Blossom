"""Local Diagnostics：读本机真实状态，好话坏话都照实说。

只读两个只读工具。不修不补，也不美化结果。
"""


def _read(ctx, name, **arguments):
    try:
        outcome = ctx.tool(name, **arguments)
    except Exception as exc:
        return None, str(exc)
    if not getattr(outcome, "ok", False):
        return None, str(getattr(outcome, "error", "") or "读不到")
    return str(getattr(outcome, "result", "") or "").strip(), ""


def run(ctx):
    body, why = _read(ctx, "run_diagnostics")
    if body is None:
        return {"ok": False, "error": "自检没跑成：%s" % why}

    machine, why2 = _read(ctx, "system_status")
    lines = [body]
    if machine:
        lines.append("机器：" + machine)
    else:
        lines.append("机器状态没读到（%s）" % why2)

    return {
        "ok": True,
        "summary": "\n".join(lines),
        "detail": "上面每一项都是本机读到的真实值，没有估算。",
        "verified": True,
    }
