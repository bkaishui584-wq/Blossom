"""设备工具：让 Agent 能在登记过的动作范围内操作设备。

模型只能请求登记过的动作，参数范围由动作注册表校验；能不能做由权限中心决定。
这一层不做任何自己的放行判断，只把结果原样说清楚。
"""

from tools.registry import Tool, ToolError

LIST_TOOL = "list_device_actions"
RUN_TOOL = "run_device_action"
STOP_TOOL = "device_emergency_stop"

STATUS_TEXT = {
    "completed": "设备确认做完了",
    "failed": "设备报告失败",
    "timeout": "设备没有确认，按超时处理",
    "cancelled": "已取消",
    "device_disconnected": "设备断开了",
}


def actions_line(data):
    if not isinstance(data, dict):
        return "读不到设备信息。"
    devices = data.get("devices") or []
    if not devices:
        return "现在没有接入任何设备。"
    lines = []
    for item in devices:
        head = "%s（%s%s）" % (
            item.get("name"),
            "模拟设备" if item.get("simulated") else "真机",
            "，已连接" if item.get("connected") else "，未连接",
        )
        ready = list(item.get("ready") or [])
        blocked = list(item.get("blocked") or [])
        if ready:
            head += "。能做：" + "、".join(str(name) for name in ready)
        if blocked:
            head += "。不能做：" + "、".join(
                "%s（%s）" % (row.get("name"), row.get("why") or "设备不支持") for row in blocked
            )
        lines.append(head)
    return "\n".join(lines)


def task_line(task):
    if not isinstance(task, dict):
        return "没有拿到执行结果。"
    status = STATUS_TEXT.get(task.get("status"), task.get("status_label") or task.get("status") or "")
    text = "%s %s：%s" % (task.get("device"), task.get("action"), status)
    if task.get("result"):
        text += "；结果 %s" % task["result"]
    if task.get("error"):
        text += "；%s" % task["error"]
    return text


def _snapshot(stack):
    """紧凑清单：只带设备名、连接状态和「能做/不能做」，不带全套 schema。"""
    rows = []
    for row in stack.catalog():
        actions = list(row.get("actions") or [])
        rows.append(
            {
                "name": row.get("name"),
                "simulated": bool(row.get("simulated")),
                "connected": bool(row.get("connected")),
                "firmware": row.get("firmware") or "",
                "capabilities": list(row.get("capabilities") or []),
                "ready": [a.get("name") for a in actions if a.get("available")],
                "blocked": [
                    {"name": a.get("name"), "why": a.get("reason") or "设备不支持"}
                    for a in actions
                    if not a.get("available")
                ],
            }
        )
    return {"ok": True, "devices": rows}


def list_device_actions_tool(stack):
    def handler():
        return _snapshot(stack)

    return Tool(
        name=LIST_TOOL,
        description="看当前接入了哪些设备、每台已经连接没有、能做哪些已登记的动作",
        schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        permission="READ",
        risk="只读设备状态与已登记动作",
        summarize=actions_line,
        handler=handler,
        keywords=("设备", "外设", "esp32", "传感器", "led", "舵机", "device"),
    )


def run_device_action_tool(stack):
    def handler(device="", action="", params=None):
        result = stack.tasks.submit(
            device=device, action=action, params=params or {}, source="agent"
        )
        if not result.get("ok"):
            raise ToolError(result.get("error") or "设备动作没有完成")
        return result

    return Tool(
        name=RUN_TOOL,
        description=(
            "让设备执行一个已登记的动作，比如 led.set、servo.set_angle、sensor.read、device.stop。"
            "只有设备确认执行完了才算成功。"
        ),
        schema={
            "type": "object",
            "properties": {
                "device": {"type": "string", "description": "设备名"},
                "action": {"type": "string", "description": "已登记的动作名，例如 led.set"},
                "params": {"type": "object", "description": "动作参数，例如 {\"channel\": 0, \"on\": true}"},
            },
            "required": ["device", "action"],
            "additionalProperties": False,
        },
        permission="LOW",
        risk="会真的驱动设备；单个动作的授权由设备权限服务单独判断，可能需要用户确认",
        summarize=task_line,
        handler=handler,
        keywords=("开关灯", "led", "舵机", "转动", "设备动作", "servo", "device"),
    )


def device_stop_tool(stack):
    def handler():
        return stack.tasks.emergency_stop()

    return Tool(
        name=STOP_TOOL,
        description="让所有连着的设备停下当前动作（固件实现了才有效，不做假的紧急停止）",
        schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        permission="LOW",
        risk="只下发停止，不动其他状态；停不了的原样报出来",
        handler=handler,
        keywords=("停下", "停止", "急停", "别动", "stop"),
    )


def register_device_tools(registry, stack):
    """把设备工具注册进工具表。没有设备栈就什么都不注册，不装作有。"""
    if stack is None:
        return registry
    for tool in (
        list_device_actions_tool(stack),
        run_device_action_tool(stack),
        device_stop_tool(stack),
    ):
        registry.register(tool)
    return registry