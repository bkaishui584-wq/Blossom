"""内置工具。工具只做参数整理与错误转换，业务逻辑在 services 层。"""

from services.calendar import CalendarService
from services.cache import TTLCache
from services.news import NewsService
from services.weather import WeatherService
from tools.registry import ASK_USER_TOOL, Tool, ToolError


def unwrap(result):
    """服务失败时抛错，由执行层返回真实错误，而不是伪造成功。"""
    if not result or not result.get("ok"):
        raise ToolError((result or {}).get("error") or "服务返回失败")
    return result.get("data")


def unwrap_ok(result):
    """设备接口用 ok 字段表示成功，失败时抛真实错误。"""
    if not result or not result.get("ok"):
        raise ToolError((result or {}).get("error") or "设备操作失败")
    return result


def weather_line(data):
    city = data.get("city") or "当地"
    temperature = data.get("temperature")
    if temperature is None:
        return f"{city}的天气暂时读不到温度。"
    line = f"{city}现在 {temperature}°C，{data.get('weather') or '天气未知'}。"
    if data.get("stale"):
        minutes = max(1, int((data.get("age_seconds") or 0) // 60))
        line += f"（取不到最新数据，这是 {minutes} 分钟前的记录）"
    return line


def news_line(data):
    titles = [str(item.get("title") or "").strip() for item in (data.get("items") or [])[:3]]
    titles = [title for title in titles if title]
    if not titles:
        return "暂时没有拿到新闻标题。"
    line = f"{data.get('topic') or '新闻'}：" + "；".join(titles) + "。"
    if data.get("stale"):
        minutes = max(1, int((data.get("age_seconds") or 0) // 60))
        line += f"（取不到最新数据，这是 {minutes} 分钟前的记录）"
    return line


def calendar_line(data):
    events = data.get("events") or []
    if not events:
        return "本地日程里没有安排。"
    items = [f"{event.get('when_at')} {event.get('title')}" for event in events[:3]]
    return "最近的安排：" + "；".join(items) + "。"


def devices_line(data):
    devices = data.get("devices") or []
    if not devices:
        return "现在没有接入任何设备。"
    items = [
        f"{device.get('device')}（{'已连接' if device.get('connected') else '未连接'}）"
        for device in devices
    ]
    return "已接入：" + "；".join(items) + "。"


def weather_tool(service):
    return Tool(
        name="get_weather",
        description="查询某个城市当前的天气",
        schema={
            "type": "object",
            "properties": {"city": {"type": "string", "description": "城市名，例如 北京"}},
            "required": [],
            "additionalProperties": False,
        },
        permission="READ",
        risk="只读公开天气数据",
        summarize=weather_line,
        handler=lambda city="": unwrap(service.current(city)),
        keywords=("天气", "气温", "温度", "下雨", "冷不冷", "热不热", "weather"),
        requires="network",
    )


def news_tool(service):
    return Tool(
        name="get_news",
        description="获取新闻头条，或按关键词搜索新闻",
        schema={
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "关键词，留空表示头条"},
                "limit": {"type": "integer", "description": "条数，默认 3"},
            },
            "required": [],
            "additionalProperties": False,
        },
        permission="READ",
        risk="只读公开新闻标题",
        summarize=news_line,
        handler=lambda topic="", limit=3: unwrap(service.latest(topic, limit)),
        keywords=("新闻", "资讯", "头条", "热点", "news"),
        requires="network",
    )


def list_calendar_tool(service):
    return Tool(
        name="list_calendar_events",
        description="查看已保存的日程安排",
        schema={
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "条数，默认 10"}},
            "required": [],
            "additionalProperties": False,
        },
        permission="READ",
        risk="只读本地日程",
        summarize=calendar_line,
        handler=lambda limit=10: unwrap(service.list_events(limit)),
        keywords=("日程", "安排", "待办", "计划", "calendar"),
    )


def _calendar_verify(service, done):
    """写完之后真去库里看一趟：读到才回报已复核，读不到就什么都不说。

    只做确认，不改数据；确认不了就如实留成「未校验」，不假装成功。
    """
    if not isinstance(done, dict) or not done.get("id"):
        return None
    try:
        result = service.list_events(limit=50)
    except Exception:
        return None
    events = ((result or {}).get("data") or {}).get("events") or []
    for row in events:
        if str(row.get("id")) == str(done["id"]):
            return "已复核：日程 #%s 确实写进库里了" % done["id"]
    return None


def add_calendar_tool(service):
    return Tool(
        name="add_calendar_event",
        description="新建一条日程或提醒，时间为 2026-10-07 09:00 这类写法",
        schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "日程标题"},
                "when": {"type": "string", "description": "时间，例如 2026-10-07 09:00"},
                "note": {"type": "string", "description": "备注"},
                "end": {"type": "string", "description": "结束时间，可留空"},
                "priority": {"type": "integer", "description": "0 到 9"},
                "repeat_rule": {
                    "type": "string",
                    "enum": ["", "daily", "weekly", "monthly"],
                    "description": "重复规则",
                },
                "remind_minutes": {"type": "integer", "description": "提前多少分钟提醒"},
            },
            "required": ["title", "when"],
            "additionalProperties": False,
        },
        permission="LOW",
        risk="只在本地数据库新增一条日程",
        handler=lambda title, when, note="", end="", priority=1, repeat_rule="", remind_minutes=10: unwrap(
            service.add_event(
                title,
                when,
                note,
                end=end,
                priority=priority,
                repeat_rule=repeat_rule,
                remind_minutes=remind_minutes,
            )
        ),
        verify=lambda done: _calendar_verify(service, done),
        keywords=(
            "提醒",
            "日程",
            "安排",
            "会议",
            "记一下",
            "记个",
            "新建日程",
            "加个日程",
            "calendar",
        ),
    )


def update_calendar_tool(service):
    def handler(
        event_id,
        title=None,
        when=None,
        end=None,
        note=None,
        priority=None,
        repeat_rule=None,
        remind_minutes=None,
    ):
        fields = {}
        for key, value in (
            ("title", title),
            ("when", when),
            ("end", end),
            ("note", note),
            ("priority", priority),
            ("repeat_rule", repeat_rule),
            ("remind_minutes", remind_minutes),
        ):
            if value is not None:
                fields[key] = value
        return unwrap(service.update_event(event_id, **fields))

    return Tool(
        name="update_calendar_event",
        description="修改本地日程的标题、时间、结束时间、优先级、重复规则或提醒时间",
        schema={
            "type": "object",
            "properties": {
                "event_id": {"type": "integer", "description": "日程编号"},
                "title": {"type": "string"},
                "when": {"type": "string"},
                "end": {"type": "string"},
                "note": {"type": "string"},
                "priority": {"type": "integer"},
                "repeat_rule": {"type": "string", "enum": ["", "daily", "weekly", "monthly"]},
                "remind_minutes": {"type": "integer"},
            },
            "required": ["event_id"],
            "additionalProperties": False,
        },
        permission="LOW",
        risk="会修改本地日程",
        handler=handler,
        verify=lambda done: _calendar_verify(service, done),
        keywords=("修改日程", "改日程", "调整时间", "日程"),
    )


def complete_calendar_tool(service):
    return Tool(
        name="complete_calendar_event",
        description="把一条本地日程标记为完成，不删除记录",
        schema={
            "type": "object",
            "properties": {"event_id": {"type": "integer"}},
            "required": ["event_id"],
            "additionalProperties": False,
        },
        permission="LOW",
        risk="只改本地日程状态",
        handler=lambda event_id: unwrap(service.complete_event(event_id)),
        keywords=("完成日程", "做完了", "日程完成"),
    )


def calendar_free_slots_tool(service):
    return Tool(
        name="calendar_free_slots",
        description="计算某天工作时间内可用的空闲时段",
        schema={
            "type": "object",
            "properties": {
                "day": {"type": "string", "description": "YYYY-MM-DD"},
                "work_start": {"type": "string", "description": "默认 09:00"},
                "work_end": {"type": "string", "description": "默认 18:00"},
                "min_minutes": {"type": "integer", "description": "最短空闲分钟数"},
            },
            "required": ["day"],
            "additionalProperties": False,
        },
        permission="READ",
        risk="只读本地日程并计算空闲时段",
        readonly=True,
        handler=lambda day, work_start="09:00", work_end="18:00", min_minutes=30: unwrap(
            service.free_slots(day, work_start, work_end, min_minutes)
        ),
        keywords=("空闲", "有空", "排期", "时间空档", "日程"),
    )


def calendar_conflicts_tool(service):
    return Tool(
        name="calendar_conflicts",
        description="检查一个时间段是否和已有日程冲突",
        schema={
            "type": "object",
            "properties": {
                "start": {"type": "string"},
                "end": {"type": "string"},
            },
            "required": ["start", "end"],
            "additionalProperties": False,
        },
        permission="READ",
        risk="只读检查日程冲突",
        readonly=True,
        handler=lambda start, end: unwrap(service.conflicts(start, end)),
        keywords=("冲突", "撞车", "日程冲突", "时间冲突"),
    )


def ask_user_tool():
    """任务缺少必要信息时向用户提问。真正的暂停由 Agent Core 处理。"""
    return Tool(
        name=ASK_USER_TOOL,
        description="任务缺少必要信息时，向用户问一个问题，拿到回答后再继续",
        schema={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "要问用户的问题，尽量具体"}
            },
            "required": ["question"],
            "additionalProperties": False,
        },
        permission="READ",
        risk="只是提问，不执行任何操作",
        handler=lambda question: {"question": question, "note": "已向用户提问，等待回答"},
        keywords=(),
    )


def list_devices_tool(manager):
    return Tool(
        name="list_devices",
        description="查看已接入的外部设备与它们的状态",
        schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        permission="READ",
        risk="只读设备状态",
        summarize=devices_line,
        handler=lambda: unwrap_ok(manager.get_state()),
        keywords=("设备", "外设", "传感器", "device"),
    )


def get_device_state_tool(manager):
    return Tool(
        name="get_device_state",
        description="查看某个设备的详细状态",
        schema={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "设备名"}},
            "required": ["name"],
            "additionalProperties": False,
        },
        permission="READ",
        risk="只读设备状态",
        handler=lambda name: unwrap_ok(manager.get_state(name)),
        keywords=("设备状态", "device", "传感器"),
    )


def send_device_command_tool(manager):
    return Tool(
        name="send_device_command",
        description="给外部设备发一条指令，比如舵机转到某个角度，当前只发给模拟设备。",
        schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "设备名"},
                "command": {"type": "object", "description": "指令内容，例如 {\"cmd\": \"servo\", \"angle\": 90}"},
            },
            "required": ["name", "command"],
            "additionalProperties": False,
        },
        permission="LOW",
        risk="只把指令交给设备适配层校验，不驱动真实硬件",
        handler=lambda name, command: unwrap_ok(manager.execute(name, command)),
        keywords=("舵机", "指令", "控制设备", "servo", "device"),
    )


def register_default_tools(
    registry,
    config,
    database,
    devices=None,
    skills=None,
    workspace=False,
    extra_roots=None,
    apps=None,
    calendar=None,
):
    """装配内置工具。工具通过 services 访问外部数据，Agent Core 不直接调用服务。

    workspace=True 时额外装工作区工具（读写文件、跑测试、看 Git）。
    默认不装，老调用方拿到的工具集合与以前完全一致。
    """
    cache = TTLCache(ttl_seconds=config.service_cache_ttl)
    weather = WeatherService(cache=cache, default_city=config.weather_city)
    news = NewsService(
        api_key=config.news_api_key, base_url=config.news_base_url, cache=cache
    )
    calendar = calendar or CalendarService(database)
    for tool in (
        weather_tool(weather),
        news_tool(news),
        list_calendar_tool(calendar),
        add_calendar_tool(calendar),
        update_calendar_tool(calendar),
        complete_calendar_tool(calendar),
        calendar_free_slots_tool(calendar),
        calendar_conflicts_tool(calendar),
        ask_user_tool(),
    ):
        registry.register(tool)
    if devices is not None:
        for tool in (
            list_devices_tool(devices),
            get_device_state_tool(devices),
            send_device_command_tool(devices),
        ):
            registry.register(tool)
    if workspace:
        from tools.workspace import register_workspace_tools

        register_workspace_tools(registry, config, extra_roots=extra_roots)
    # 打开网址 / 文件 / 已登记应用。都是程序执行，权限中心先过一遍。
    from tools.apps import register_app_tools

    register_app_tools(registry, config, apps=apps)
    if skills is not None:
        from tools.skills import skills_tool

        registry.register(skills_tool(skills))
    return registry
