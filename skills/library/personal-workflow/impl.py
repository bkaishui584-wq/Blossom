"""Personal Workflow：本地日程与提醒。时间说不清就先问。"""

import datetime as dt
import re

from skills.library._common import mapping, payload

WHEN_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})")
CLOCK_RE = re.compile(r"(\d{1,2})[:：点](\d{1,2})?")
DAY_WORDS = (("今天", 0), ("today", 0), ("明天", 1), ("tomorrow", 1), ("后天", 2))


def _now():
    return dt.datetime.now()


def parse_when(text, now=None):
    """把「明天 9:00」这类说法转成时间戳。解析不了返回空串。"""
    now = now or _now()
    text = str(text or "")
    explicit = WHEN_RE.search(text)
    if explicit:
        year, month, day, hour, minute = (int(value) for value in explicit.groups())
        return "%04d-%02d-%02d %02d:%02d" % (year, month, day, hour, minute)
    day_offset = None
    lowered = text.lower()
    for word, offset in DAY_WORDS:
        if word in lowered:
            day_offset = offset
            break
    clock = CLOCK_RE.search(text)
    if day_offset is None and clock is None:
        return ""
    when = now + dt.timedelta(days=day_offset or 0)
    hour, minute = 9, 0
    if clock:
        hour = max(0, min(23, int(clock.group(1))))
        minute = max(0, min(59, int(clock.group(2) or 0)))
    return "%04d-%02d-%02d %02d:%02d" % (when.year, when.month, when.day, hour, minute)


def _title_from(text):
    cleaned = str(text or "")
    for word in ("提醒我", "提醒", "记一下", "待办", "日程", "安排", "添加", "加一个"):
        cleaned = cleaned.replace(word, " ")
    cleaned = WHEN_RE.sub(" ", cleaned)
    cleaned = CLOCK_RE.sub(" ", cleaned)
    for word, _ in DAY_WORDS:
        cleaned = cleaned.replace(word, " ")
    cleaned = " ".join(cleaned.split())
    return cleaned[:40] or "待办"


def run(ctx):
    action = str(ctx.inputs.get("action") or "").strip().lower()
    request = str(ctx.request or "")
    if not action:
        if any(word in request for word in ("提醒", "记一下", "待办", "加一个", "添加")):
            action = "add"
        elif any(word in request for word in ("总结", "今天做了", "回顾")):
            action = "summary"
        else:
            action = "list"

    if action == "add":
        when = str(ctx.inputs.get("when") or "") or parse_when(request)
        title = str(ctx.inputs.get("title") or "") or _title_from(request)
        if not when:
            return {
                "ok": False,
                "error": "时间我没看懂。写成「明天 9:00」或「2026-10-12 09:00」这样的格式。",
            }
        out = ctx.tool("add_calendar_event", title=title, when=when, note="")
        if not out.ok:
            return {"ok": False, "error": "日程没写进去：%s" % out.error}
        verified = bool(out.verified)
        check = ctx.tool("list_calendar_events", limit=50)
        if check.ok:
            events = mapping(payload(check)).get("events") or []
            verified = any(str(item.get("title")) == title for item in events)
        return {
            "ok": True,
            "summary": "记下了：%s %s。" % (when, title),
            "detail": "写入%s。" % ("已从库中读回，确认在" if verified else "后没能读回复核"),
            "verified": verified,
        }

    listing = ctx.tool("list_calendar_events", limit=20)
    if not listing.ok:
        return {"ok": False, "error": "读不到日程：%s" % listing.error}
    events = mapping(payload(listing)).get("events") or []
    today = _now().strftime("%Y-%m-%d")
    if action == "summary":
        upcoming = [item for item in events if str(item.get("when_at") or "").startswith(today)]
        lines = ["今天 %d 条安排。" % len(upcoming)]
        lines.extend("- %s %s" % (item.get("when_at"), item.get("title")) for item in upcoming[:8])
        memory_note = ""
        if ctx.memory is not None:
            try:
                recent = ctx.memory.recent_memories(limit=5) or []
                if recent:
                    memory_note = "\n最近的记忆：" + "；".join(
                        str(row.get("content"))[:40] for row in recent[:3]
                    )
            except Exception:
                memory_note = ""
        return {
            "ok": True,
            "summary": "今天的安排和最近记忆整理好了。",
            "detail": "\n".join(lines) + memory_note,
            "verified": True,
        }
    if not events:
        return {
            "ok": True,
            "summary": "本地日程里没有安排。",
            "detail": "要加一条就说「提醒我明天 9 点开会」。",
            "verified": True,
        }
    detail = "\n".join(
        "- %s %s" % (item.get("when_at"), item.get("title")) for item in events[:12]
    )
    return {
        "ok": True,
        "summary": "最近的安排有 %d 条。" % len(events),
        "detail": detail,
        "verified": True,
    }
