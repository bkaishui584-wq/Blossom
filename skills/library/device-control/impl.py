"""Device Control：把一句话变成设备动作。

动作只能从已登记的那几个里挑，能不能做由设备和权限决定。这一层不自己放行，
也不把「发出去」当成「做成了」。
"""

import json

STATUS_OK = "completed"


def _load(text):
    """工具回的是文字，能解析就解析。解析不了就返回空，不猜。"""
    try:
        data = json.loads(str(text or ""))
    except Exception:
        return {}
    if not isinstance(data, dict) or data.get("truncated"):
        return {}
    return data


def _pick(devices, wanted):
    if wanted:
        for item in devices:
            if str(item.get("name")) == wanted:
                return item
        return None
    live = [item for item in devices if item.get("connected")]
    if len(live) == 1:
        return live[0]
    return None


def _ready_names(item):
    """能做的动作名。清单给的是名字列表，兼容一下带说明的写法。"""
    names = []
    for row in item.get("ready") or []:
        name = row.get("name") if isinstance(row, dict) else row
        if name:
            names.append(str(name))
    return names


def _listing(devices):
    lines = []
    for item in devices:
        ready = _ready_names(item)
        lines.append(
            "%s（%s）：%s"
            % (
                item.get("name"),
                "已连接" if item.get("connected") else "未连接",
                "能做 " + "、".join(ready) if ready else "没有能做的动作",
            )
        )
    return "；".join(lines)


def _describe(task):
    if not isinstance(task, dict) or not task:
        return ""
    result = task.get("result") or {}
    text = "%s %s" % (task.get("device"), task.get("action"))
    if result:
        text += "：" + json.dumps(result, ensure_ascii=False)
    return text


def run(ctx):
    wanted = str(ctx.inputs.get("device") or "").strip()
    action = str(ctx.inputs.get("action") or "").strip()
    params = ctx.inputs.get("params")
    if not isinstance(params, dict):
        params = {}

    listing = ctx.tool("list_device_actions")
    if not listing.ok:
        return {"ok": False, "error": "读不到设备状态：%s" % listing.error}
    data = _load(getattr(listing, "result", ""))
    devices = list(data.get("devices") or [])
    if not devices:
        return {
            "ok": False,
            "error": "现在没有接入任何设备。先到设备中心连一台，或者用模拟设备试。",
            "detail": "设备中心在左侧导航里，连上之后再说这句话。",
        }

    if not action:
        return {
            "ok": True,
            "summary": _listing(devices),
            "detail": "要执行哪个动作，说一声就行；动作名必须是设备已经登记的那些。",
            "verified": True,
        }

    target = _pick(devices, wanted)
    if target is None:
        return {"ok": False, "error": "不确定是哪台设备，先说清楚设备名。现在有：%s" % _listing(devices)}
    if not target.get("connected"):
        return {"ok": False, "error": "%s 没有连上，先到设备中心把它连起来。" % target.get("name")}

    outcome = ctx.tool(
        "run_device_action", device=target.get("name"), action=action, params=params
    )
    if not outcome.ok:
        return {"ok": False, "error": str(outcome.error or "这一步没做成")}

    task = _load(getattr(outcome, "result", ""))
    if not task:
        return {"ok": True, "summary": str(outcome.result), "verified": False}
    if str(task.get("status")) != STATUS_OK:
        return {
            "ok": False,
            "error": "%s 没做成：%s" % (action, task.get("error") or task.get("status") or "设备没有确认"),
        }
    result = task.get("result") or {}
    simulated = bool(result.get("simulated"))
    summary = _describe(task)
    detail = "结果来自设备的确认。" if not simulated else "这是模拟设备的结果，没有真硬件被动过。"
    return {"ok": True, "summary": summary, "detail": detail, "verified": True}