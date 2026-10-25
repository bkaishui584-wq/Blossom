"""System Assistant：本机时间、运行状态与本地待办。

全部走本地工具，不联网、不需要模型，也不自己造数字。
"""


TIME_WORDS = ("几点", "时间", "日期", "几号", "星期", "礼拜", "时区")
STATUS_WORDS = ("电量", "电池", "内存", "开机", "运行状态", "系统状态", "用了多久")
TODO_WORDS = ("待办", "todo", "要做的事", "清单")


def _pick(request):
    text = str(request or "")
    lowered = text.lower()
    for word in TODO_WORDS:
        if word in lowered:
            return "todo"
    for word in STATUS_WORDS:
        if word in text:
            return "status"
    return "time"


def run(ctx):
    action = str(ctx.inputs.get("action") or "").strip().lower()
    if action not in ("time", "status", "todo"):
        action = _pick(ctx.request)

    if action == "status":
        out = ctx.tool("system_status")
        if not out.ok:
            return {"ok": False, "error": "读不到本机状态：%s" % out.error}
        return {
            "ok": True,
            "summary": str(out.result),
            "detail": "读数来自本机接口，没有联网。",
            "verified": True,
        }

    if action == "time":
        what = str(ctx.inputs.get("what") or "all")
        out = ctx.tool("local_time", what=what)
        if not out.ok:
            return {"ok": False, "error": "读不到本机时间：%s" % out.error}
        return {
            "ok": True,
            "summary": str(out.result),
            "detail": "读数来自本机时钟。",
            "verified": True,
        }

    listing = ctx.tool("todo_list")
    if not listing.ok:
        return {"ok": False, "error": "读不到待办：%s" % listing.error}
    return {
        "ok": True,
        "summary": str(listing.result),
        "detail": "待办存在本地库里，没有上传。",
        "verified": True,
    }