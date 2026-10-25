"""Local Abilities：本机组件状态，只读。

不装、不下载、不改配置。探测不到就说探测不到。
"""


def run(ctx):
    what = str(ctx.inputs.get("what") or "all").strip().lower()
    outcome = ctx.tool("local_ability_status", what=what or "all")
    if not getattr(outcome, "ok", False):
        return {
            "ok": False,
            "error": str(getattr(outcome, "error", "") or "读不到本机组件状态"),
        }
    body = str(getattr(outcome, "result", "") or "").strip()
    return {
        "ok": True,
        "summary": body,
        "detail": "这些是探测结果，没有自动安装任何东西。要装哪一项由你决定。",
        "verified": True,
    }
