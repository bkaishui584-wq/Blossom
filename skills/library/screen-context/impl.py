"""Screen & Context：只读当前前台窗口。

屏幕感知关着就如实说关着。这一层只读，不做任何写入和联网动作。
"""


def run(ctx):
    what = str(ctx.inputs.get("what") or "").strip().lower()
    if what not in ("window", "semantics", "all"):
        text = ctx.request
        if "文字" in text or "内容" in text:
            what = "semantics"
        else:
            what = "all"

    out = ctx.tool("screen_context", what=what)
    if not getattr(out, "ok", False):
        error = str(getattr(out, "error", "") or "读不到当前窗口")
        if "关着" in error:
            return {
                "ok": False,
                "error": error,
                "detail": "屏幕感知由用户自己决定开不开，我不会去动这个开关。",
            }
        return {"ok": False, "error": error}

    return {
        "ok": True,
        "summary": str(out.result),
        "detail": "读数来自本机窗口接口，没有截图，也没有联网。",
        "verified": True,
    }
