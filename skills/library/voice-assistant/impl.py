"""Voice Assistant：如实报语音状态。

只读。不开麦、不朗读、不改设置。要开开关由用户自己在设置里点。
"""

PACK_WORDS = ("语音包", "声音包", "换声音", "voice pack")


def _pick(request):
    text = str(request or "").lower()
    for word in PACK_WORDS:
        if word.lower() in text:
            return "pack"
    return "status"


def run(ctx):
    action = str(ctx.inputs.get("what") or "").strip().lower()
    if action not in ("status", "pack"):
        action = _pick(ctx.request)

    if action == "pack":
        out = ctx.tool("voice_pack_list")
        if not getattr(out, "ok", False):
            return {"ok": False, "error": "读不到语音包清单：%s" % getattr(out, "error", "")}
        return {
            "ok": True,
            "summary": str(out.result),
            "detail": "语音包只读元数据，不会执行包里的任何东西。",
            "verified": True,
        }

    out = ctx.tool("voice_status")
    if not getattr(out, "ok", False):
        return {"ok": False, "error": "读不到语音状态：%s" % getattr(out, "error", "")}
    return {
        "ok": True,
        "summary": str(out.result),
        "detail": "状态来自本机配置，没有开麦也没有朗读。",
        "verified": True,
    }
