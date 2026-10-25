"""主动陪伴的规则表。

每条规则只回答「现在值不值得开口」，命中之后才交给 Agent 生成一句人话。
调度器不会为了这件事去轮询模型。

两类规则：
    定时规则：按间隔检查（连续工作太久、早上打个招呼、晚上收个尾、日程要开始了）
    事件规则：由 EventBus 上的事件触发（用户刚回到电脑前）

用户关掉主动提醒时，一条都不装；本地倒计时和提醒不走这里，不受影响。
"""

import time
from datetime import datetime, timedelta

from behavior import actions
from events import bus as bus_module
from scheduler.manager import CompanionRule
from services.calendar import parse_when

MORNING_HOURS = (5, 9)
EVENING_HOURS = (21, 23)
SUMMARY_HOURS = (18, 23)
WEATHER_HOURS = (7, 10)
CALENDAR_LEAD_MINUTES = 10

# 用户回到电脑前的问候。同一小时内只打一次招呼。
WELCOME_BACK = {
    "name": "welcome_back",
    "situation": "用户刚回到电脑前，跟他打个招呼，一两句就行，别啰嗦。",
    "priority": "low",
    "cooldown_seconds": 3600,
    "max_per_day": 3,
}


# 电量低了只说一句程序写好的话，不需要模型润色
LOW_BATTERY = {
    "name": "battery_low",
    "situation": "用户电脑电量很低了，提醒他插上电源，一句话就够。",
    "priority": "high",
    "cooldown_seconds": 1800,
    "max_per_day": 3,
    "payload": {"local_text": "你电脑快没电了，记得插上电源。", "emotion": "tired"},
}


def _hour(now):
    return time.localtime(now).tm_hour


def _around(ctx):
    """用户人不在电脑前时，不主动开口。"""
    return not ctx.get("user_away", False)


def _in_hours(start, end):
    def condition(ctx):
        return start <= _hour(ctx["now"]) < end

    return condition


def work_long_rule(cooldown_seconds, max_per_day=4):
    """连续工作太久，提醒起来活动一下。"""
    return CompanionRule(
        name="work_long",
        situation="用户已经连续工作很久没有休息，提醒他起来活动一下。",
        interval_seconds=300,
        cooldown_seconds=cooldown_seconds,
        max_per_day=max_per_day,
        priority="normal",
        condition=lambda ctx: (ctx.get("silent_for") or 0) >= cooldown_seconds,
    )


def morning_rule():
    """早上第一次见到用户，打个招呼。一天只打一次。"""
    return CompanionRule(
        name="morning",
        situation="早上好。跟用户打个招呼，可以顺带问一句今天要忙什么。",
        interval_seconds=900,
        cooldown_seconds=12 * 3600,
        max_per_day=1,
        priority="low",
        condition=lambda ctx: _around(ctx) and _in_hours(*MORNING_HOURS)(ctx),
    )


def evening_rule():
    """晚上关心一句，提醒收尾。一天只提一次。"""
    return CompanionRule(
        name="evening",
        situation="晚上关心一下用户，提醒他别太累，可以准备收尾了。",
        interval_seconds=1800,
        cooldown_seconds=12 * 3600,
        max_per_day=1,
        priority="low",
        condition=lambda ctx: _around(ctx) and _in_hours(*EVENING_HOURS)(ctx),
    )


def calendar_soon_rule(calendar, lead_minutes=CALENDAR_LEAD_MINUTES):
    """日程要开始了，提前提醒一句。读不出日程就当没有，保持安静。"""
    found = {}  # 条件算出来的日程名，随事件一起带给 Agent

    def condition(ctx):
        found.clear()
        title = _soon_title(calendar, ctx["now"], lead_minutes)
        if not title:
            return False
        found["event_title"] = title
        return True

    return CompanionRule(
        name="calendar_soon",
        situation="用户有个日程快要开始了，提醒他一下。",
        interval_seconds=120,
        cooldown_seconds=600,
        max_per_day=8,
        priority="normal",
        condition=condition,
        payload=found,
    )


def _soon_title(calendar, now, lead_minutes):
    """N 分钟内要开始的日程标题。读不到就返回空字符串。"""
    if calendar is None:
        return ""
    try:
        result = calendar.list_events(limit=20)
        if not result or not result.get("ok"):
            return ""
        moment = datetime.fromtimestamp(now)
        soon = moment + timedelta(minutes=max(1, int(lead_minutes)))
        for row in result["data"]["events"]:
            when = parse_when(row.get("when_at"))
            if not when:
                continue
            at = datetime.strptime(when, "%Y-%m-%d %H:%M")
            if moment <= at <= soon:
                return str(row.get("title") or "").strip()
    except Exception:
        return ""
    return ""


def topic_rule(topics, interval_seconds=3600):
    """自主话题：只拿用户允许的信息（项目进展、日程、最近聊过的主题）。

    拿不到真实素材就什么都不说，绝不编一条观察出来。
    """
    found = {}

    def condition(ctx):
        found.clear()
        if not callable(topics):
            return False
        try:
            topic = topics(ctx)
        except Exception:
            return False
        if not topic:
            return False
        found.update(topic)
        return True

    return CompanionRule(
        name="topic",
        situation="想找个话题聊两句。用给定素材说一句，没有素材就别开口。",
        interval_seconds=interval_seconds,
        cooldown_seconds=6 * 3600,
        max_per_day=1,
        priority="low",
        condition=condition,
        payload=found,
    )


def daily_summary_rule(todos, tasks=None):
    """晚上收个尾：还剩几条待办、今天做完了几件事。只用本地数据，不调模型。"""
    found = {}

    def condition(ctx):
        found.clear()
        lines = []
        rows = _pending_todos(todos)
        if rows:
            lines.append("还剩 %d 条待办，最近一条是「%s」" % (len(rows), rows[0].get("title")))
        done = _done_today(tasks, ctx["now"])
        if done:
            lines.append("今天做完了 %d 件事" % done)
        if not lines:
            return False
        found["local_text"] = "今天先到这儿：" + "；".join(lines) + "。"
        found["situation"] = "晚上收个尾，跟用户说一句今天还剩什么。"
        found["emotion"] = "calm"
        return True

    return CompanionRule(
        name="daily_summary",
        situation="晚上收个尾，跟用户说一句今天还剩什么。",
        interval_seconds=1800,
        cooldown_seconds=12 * 3600,
        max_per_day=1,
        priority="low",
        condition=lambda ctx: _in_hours(*SUMMARY_HOURS)(ctx) and condition(ctx),
        payload=found,
    )


def weather_rule(weather, city=""):
    """早上顺口说一句天气。取不到就闭嘴，绝不编一个数出来。"""
    found = {}

    def read_weather(ctx):
        found.clear()
        if weather is None:
            return False
        try:
            result = weather.current(city)
        except Exception:
            return False
        if not result or not result.get("ok"):
            return False
        data = result.get("data") or {}
        if data.get("temperature") is None:
            return False
        where = data.get("city") or city or "当地"
        note = "%s现在 %s°C，%s。" % (
            where,
            data["temperature"],
            data.get("weather") or "天气未知",
        )
        if data.get("stale"):
            note += "（这是上一次的记录）"
        found["local_text"] = note
        found["situation"] = "早上跟用户说一句今天的天气。"
        found["emotion"] = "neutral"
        return True

    return CompanionRule(
        name="weather_morning",
        situation="早上跟用户说一句今天的天气。",
        interval_seconds=1800,
        cooldown_seconds=12 * 3600,
        max_per_day=1,
        priority="low",
        condition=lambda ctx: _around(ctx)
        and _in_hours(*WEATHER_HOURS)(ctx)
        and read_weather(ctx),
        payload=found,
    )


def _pending_todos(todos):
    """还没做完的待办。读不到就当没有，保持安静。"""
    if todos is None:
        return []
    try:
        result = todos.list(limit=5)
    except Exception:
        return []
    if not result or not result.get("ok"):
        return []
    return list((result.get("data") or {}).get("todos") or [])


def _done_today(tasks, now):
    """今天完成的任务条数。读不到就是 0，不猜。"""
    if tasks is None:
        return 0
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    count = 0
    try:
        for record in tasks.recent(limit=20):
            if getattr(record, "state", "") != "completed":
                continue
            stamp = str(getattr(record, "updated_at", "") or getattr(record, "created_at", ""))
            if stamp.startswith(day):
                count += 1
    except Exception:
        return 0
    return count


def timed_rules(config, calendar=None, topics=None, todos=None, weather=None, tasks=None):
    """按间隔检查的主动规则。没接上的能力不装规则，保持安静。"""
    rules = [work_long_rule(config.work_reminder_seconds), morning_rule(), evening_rule()]
    if calendar is not None:
        rules.append(calendar_soon_rule(calendar))
    if topics is not None and getattr(config, "proactive_topics", False):
        rules.append(topic_rule(topics))
    if todos is not None:
        rules.append(daily_summary_rule(todos, tasks))
    if weather is not None:
        rules.append(weather_rule(weather))
    return rules


def install(scheduler, config, calendar=None, topics=None, todos=None, tasks=None, weather=None):
    """把主动陪伴装到调度器上。用户关掉主动提醒时一条都不装。"""
    if not config.proactive:
        return scheduler
    scheduler.rules.extend(
        timed_rules(config, calendar, topics, todos, weather, tasks)
    )
    scheduler.watch(bus_module.USER_RETURNED, **WELCOME_BACK)
    scheduler.watch(bus_module.BATTERY_LOW, **LOW_BATTERY)
    return scheduler
