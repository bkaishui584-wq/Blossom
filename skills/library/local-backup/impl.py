"""Local Backup：备份看得到、建得出，恢复要用户点头。

三个工具的等级不同：列表只读、建备份是普通操作、恢复是高风险。
技能不给工具提权，恢复那一步照样由权限中心拦。
"""

ACTIONS = ("list", "create", "restore")


def _pick(ctx):
    action = str(ctx.inputs.get("action") or "").strip().lower()
    if action in ACTIONS:
        return action
    text = str(ctx.request or "").lower()
    if any(word in text for word in ("恢复", "还原", "restore")):
        return "restore"
    if "备份" in text and any(word in text for word in ("建", "做", "存", "创建", "来一份", "一个")):
        return "create"
    return "list"


def run(ctx):
    action = _pick(ctx)

    if action == "list":
        outcome = ctx.tool("backup_list")
        if not getattr(outcome, "ok", False):
            return {"ok": False, "error": str(getattr(outcome, "error", "") or "读不到备份目录")}
        return {
            "ok": True,
            "summary": str(getattr(outcome, "result", "") or "").strip(),
            "detail": "只读，没有动任何数据。",
            "verified": True,
        }

    if action == "create":
        outcome = ctx.tool("backup_create")
        if not getattr(outcome, "ok", False):
            return {"ok": False, "error": str(getattr(outcome, "error", "") or "备份没做成")}
        return {
            "ok": True,
            "summary": str(getattr(outcome, "result", "") or "").strip(),
            "detail": "备份是整库复制，现有数据一个字节没动。",
            "verified": True,
        }

    path = str(ctx.inputs.get("path") or "").strip()
    if not path:
        return {
            "ok": False,
            "error": "恢复要先说用哪一份备份。列一下备份再说文件名。",
        }
    if str(ctx.inputs.get("confirm") or "").strip().lower() not in ("1", "true", "yes", "确认"):
        return {
            "ok": False,
            "error": "恢复会覆盖现在的记忆库，需要你明确确认。确认后我会先给当前库留一份副本。",
        }
    outcome = ctx.tool("restore_backup", path=path, confirm=True)
    if not getattr(outcome, "ok", False):
        return {"ok": False, "error": str(getattr(outcome, "error", "") or "恢复没做成")}
    return {
        "ok": True,
        "summary": str(getattr(outcome, "result", "") or "").strip(),
        "detail": "覆盖前的库已另存，需要回退就用那份。",
        "verified": True,
    }
