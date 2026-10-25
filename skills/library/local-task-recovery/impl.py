"""Local Task Recovery：把没做完的任务列清楚。

只读 task_unfinished 这一个工具。列出来不等于要跑，重跑哪一步由用户点。
"""


def _limit(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 10
    return max(1, min(50, number))


def run(ctx):
    outcome = ctx.tool("task_unfinished", limit=_limit(ctx.inputs.get("limit")))
    if not getattr(outcome, "ok", False):
        return {
            "ok": False,
            "error": str(getattr(outcome, "error", "") or "读不到任务记录"),
        }
    body = str(getattr(outcome, "result", "") or "").strip()
    return {
        "ok": True,
        "summary": body,
        "detail": "只列出，不自动重跑。要接着做哪一条，点名我再开。",
        "verified": True,
    }
