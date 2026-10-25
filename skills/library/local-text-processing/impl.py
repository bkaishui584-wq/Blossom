"""Local Text Processing：把一段文本收拾干净。

纯本地、纯规则，不需要模型。做什么都能复算；文本由用户给，
工具只做确定性的整理，不去读别的文件。

技能本身不获得任何权限：真正动手的是 text_process 这个只读工具，
由执行层按权限表决定放不放行。
"""

LABELS = {"clean": "清理完成", "dedupe": "去重完成", "stats": "统计完成"}


def run(ctx):
    text = str(ctx.inputs.get("text") or "")
    if not text.strip():
        return {"ok": False, "error": "没有要整理的正文。把文本放进 text 再试一次。"}

    what = str(ctx.inputs.get("what") or "clean").strip().lower()
    if what not in LABELS:
        what = "clean"

    outcome = ctx.tool("text_process", what=what, text=text)
    if not getattr(outcome, "ok", False):
        return {"ok": False, "error": str(getattr(outcome, "error", "") or "整理没做成")}

    body = str(getattr(outcome, "result", "") or "").strip()
    if what == "stats":
        return {
            "ok": True,
            "summary": body,
            "detail": "数字直接从正文数出来，没估算。",
            "verified": True,
        }
    return {
        "ok": True,
        "summary": body,
        "detail": "%s。原文里改了行尾空白、连续空行%s。" % (
            LABELS[what],
            "和重复行" if what == "dedupe" else "",
        ),
        "verified": True,
    }
