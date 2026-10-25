"""本地基础工具：时间、系统状态、计时器、待办。

只读本机数据，不联网也不需要模型。读不到就如实说读不到，不编。
计时器交给 Scheduler，不靠模型轮询；没有桌面程序时就直说现在计不了。
"""

import ctypes
import time
from datetime import datetime

from agent.capabilities import battery_text, memory_text
from memory.todo import TodoStore
from tools.registry import Tool, ToolError

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def uptime_seconds():
    """开机到现在多少秒。读不到返回 None，不编一个数。"""
    if not hasattr(ctypes, "windll"):
        return None
    try:
        return int(ctypes.windll.kernel32.GetTickCount64() / 1000)
    except Exception:
        return None


def uptime_text(seconds=None):
    total = uptime_seconds() if seconds is None else seconds
    if total is None:
        return "读不到开机时长。"
    total = int(total)
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = []
    if days:
        parts.append(f"{days} 天")
    if hours or days:
        parts.append(f"{hours} 小时")
    parts.append(f"{minutes} 分钟")
    return "开机 " + " ".join(parts) + "。"


def timezone_text(now=None):
    moment = now or datetime.now().astimezone()
    offset = moment.utcoffset()
    hours = offset.total_seconds() / 3600 if offset else 0
    name = moment.tzname() or time.tzname[0]
    return f"{name}，UTC{hours:+g}"


def local_time_text(what="all", clock=datetime.now):
    moment = clock()
    what = str(what or "all").strip().lower()
    if what in ("time", "时刻"):
        return f"现在是 {moment.strftime('%H:%M:%S')}。"
    if what in ("date", "日期"):
        return f"今天是 {moment.strftime('%Y-%m-%d')}，{WEEKDAYS[moment.weekday()]}。"
    if what in ("weekday", "星期"):
        return f"今天是 {WEEKDAYS[moment.weekday()]}。"
    if what in ("timezone", "时区"):
        return "时区是 " + timezone_text(moment.astimezone()) + "。"
    if what in ("uptime", "开机"):
        return uptime_text()
    return (
        f"现在是 {moment.strftime('%Y-%m-%d %H:%M:%S')}，"
        f"{WEEKDAYS[moment.weekday()]}，{timezone_text(moment.astimezone())}。"
    )


def system_status_text():
    return " ".join([uptime_text(), battery_text(), memory_text()])


def _schedule(timer, seconds, subject, capability="timer"):
    """把一次性提醒交给 Scheduler。没有 scheduler 就直说做不了。"""
    scheduler = None
    if timer is not None:
        try:
            scheduler = timer() if callable(timer) else timer
        except Exception:
            scheduler = None
    if scheduler is None:
        raise ToolError("计时器要桌面程序在跑才能计时，现在没法计时。")
    scheduler.schedule_once(
        max(1, int(seconds)),
        situation=f"提醒用户：{subject}",
        local_text=f"时间到了：{subject}",
        emotion="happy",
        capability=capability,
    )
    return scheduler


def _duration_label(seconds):
    seconds = max(1, int(seconds))
    if seconds % 3600 == 0:
        return f"{seconds // 3600} 小时"
    if seconds % 60 == 0:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"


def _seconds_of(minutes, seconds):
    total = 0
    if minutes not in (None, ""):
        total += int(float(minutes) * 60)
    if seconds not in (None, ""):
        total += int(float(seconds))
    return total


def register_local_tools(
    registry,
    database=None,
    config=None,
    timer=None,
    clock=datetime.now,
    logger=None,
    todos=None,
    on_todo_added=None,
):
    """装上本地工具。

    timer 传一个返回 scheduler 的函数即可。todos 可以直接传一个待办库；
    on_todo_added 在新增待办后回调，用来马上挂上到点提醒。
    """
    if todos is None and database is not None:
        todos = TodoStore(database, clock=clock, logger=logger)

    def time_handler(what="all"):
        return local_time_text(what, clock)

    def status_handler():
        return system_status_text()

    def timer_handler(minutes=None, seconds=None, subject=""):
        total = _seconds_of(minutes, seconds)
        if total <= 0:
            raise ToolError("要告诉我多久，比如 5 分钟或者 90 秒。")
        subject = str(subject or "").strip() or "时间到了"
        _schedule(timer, total, subject)
        return f"好，{_duration_label(total)}后提醒你：{subject}。"

    def pomodoro_handler(minutes=25, break_minutes=5, subject="专注"):
        focus = max(1, int(float(minutes or 25))) * 60
        rest = max(0, int(float(break_minutes or 0))) * 60
        subject = str(subject or "专注").strip() or "专注"
        _schedule(timer, focus, f"{subject}结束，起来活动一下", capability="timer")
        if rest:
            _schedule(timer, focus + rest, "休息结束，可以继续了", capability="timer")
        if not rest:
            return f"好，{_duration_label(focus)}的{subject}开始，结束我叫你。"
        return (
            f"好，{_duration_label(focus)}的{subject}开始，"
            f"结束后再休息 {_duration_label(rest)}，两头我都会提醒。"
        )

    def _need_todos():
        if todos is None:
            raise ToolError("待办要本地数据库才能记，现在读不到。")
        return todos

    def todo_add(title, due="", detail="", priority=0):
        result = _need_todos().add(title, due_at=due, detail=detail, priority=priority)
        if not result.get("ok"):
            raise ToolError(result.get("error") or "待办没记上")
        data = result["data"]
        if callable(on_todo_added):
            try:
                on_todo_added(data.get("id"))
            except Exception as exc:
                if logger:
                    logger.warning("todo reminder hook failed: %s", exc)
        if data.get("due_at"):
            return f"记下了：{data['title']}，{data['due_at']} 之前做。（编号 {data['id']}）"
        return f"记下了：{data['title']}。（编号 {data['id']}）"

    def todo_list(include_done=False, limit=20):
        result = _need_todos().list(include_done=bool(include_done), limit=limit)
        if not result.get("ok"):
            raise ToolError(result.get("error") or "待办读不出来")
        rows = result["data"]["todos"]
        if not rows:
            return "现在没有待办。"
        lines = [f"有 {len(rows)} 条："]
        for row in rows:
            mark = "（已过期）" if row.get("overdue") else ""
            when = row.get("due_at") or "没定时间"
            lines.append(f"- #{row['id']} {row['title']}（{when}）{mark}")
        return " ".join(lines)

    def todo_done(todo_id):
        result = _need_todos().done(todo_id)
        if not result.get("ok"):
            raise ToolError(result.get("error") or "待办没更新")
        data = result["data"]
        head = "这条本来就是完成状态：" if data.get("already") else "标成完成了："
        return head + str(data.get("title") or "")

    registry.register(
        Tool(
            name="local_time",
            description="本机当前时间、日期、星期、时区或开机时长。不联网，不需要模型。",
            schema={
                "type": "object",
                "properties": {
                    "what": {
                        "type": "string",
                        "enum": ["all", "time", "date", "weekday", "timezone", "uptime"],
                        "description": "想只看哪一项，默认全部",
                    }
                },
            },
            permission="READ",
            risk="",
            handler=time_handler,
            keywords=("时间", "日期", "星期", "时区", "开机", "几点"),
            readonly=True,
        )
    )
    registry.register(
        Tool(
            name="system_status",
            description="本机开机时长、电池、内存的真实状态。读不到就说读不到。",
            schema={"type": "object", "properties": {}},
            permission="READ",
            risk="",
            handler=status_handler,
            keywords=("系统状态", "电量", "电池", "内存", "开机时长"),
            readonly=True,
        )
    )
    registry.register(
        Tool(
            name="timer_start",
            description="设一个倒计时提醒，时间到了由桌宠主动开口。不用模型。",
            schema={
                "type": "object",
                "properties": {
                    "minutes": {"type": "number", "description": "多少分钟"},
                    "seconds": {"type": "number", "description": "多少秒，和分钟可叠加"},
                    "subject": {"type": "string", "description": "提醒什么事"},
                },
            },
            permission="LOW",
            risk="",
            handler=timer_handler,
            keywords=("倒计时", "计时器", "提醒我", "几分钟后"),
            timeout=2.0,
            idempotent=False,
        )
    )
    registry.register(
        Tool(
            name="pomodoro",
            description="番茄钟：一段专注加一段休息，两头都提醒。",
            schema={
                "type": "object",
                "properties": {
                    "minutes": {"type": "number", "description": "专注多少分钟，默认 25"},
                    "break_minutes": {"type": "number", "description": "休息多少分钟，默认 5"},
                    "subject": {"type": "string", "description": "在专注什么"},
                },
            },
            permission="LOW",
            risk="",
            handler=pomodoro_handler,
            keywords=("番茄钟", "番茄", "专注"),
            timeout=2.0,
            idempotent=False,
        )
    )
    registry.register(
        Tool(
            name="todo_add",
            description="把一件要做的事记成本地待办，可以带截止时间。",
            schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "要做什么"},
                    "due": {"type": "string", "description": "可选的截止时间，例如 2026-10-12 09:00"},
                    "detail": {"type": "string", "description": "补充说明"},
                    "priority": {"type": "integer", "description": "0 到 9，越大越靠前"},
                },
                "required": ["title"],
            },
            permission="LOW",
            risk="",
            handler=todo_add,
            keywords=("待办", "记一下", "要做", "todo"),
            timeout=3.0,
            idempotent=False,
        )
    )
    registry.register(
        Tool(
            name="todo_list",
            description="按时间顺序列出本地待办，过期的会标出来。",
            schema={
                "type": "object",
                "properties": {
                    "include_done": {"type": "boolean", "description": "是否连已完成的也列出来"},
                    "limit": {"type": "integer", "description": "最多列几条，默认 20"},
                },
            },
            permission="READ",
            risk="",
            handler=todo_list,
            keywords=("待办", "要做的事", "清单", "todo"),
            readonly=True,
        )
    )
    registry.register(
        Tool(
            name="todo_done",
            description="把一条待办标记成完成。只改状态，不删记录。",
            schema={
                "type": "object",
                "properties": {"todo_id": {"type": "integer", "description": "待办编号"}},
                "required": ["todo_id"],
            },
            permission="LOW",
            risk="",
            handler=todo_done,
            keywords=("完成", "做完了", "待办"),
            timeout=3.0,
        )
    )
    return todos
