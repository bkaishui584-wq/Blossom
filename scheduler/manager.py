"""定时与主动行为。

调度器只按规则发出事件，绝不轮询模型。生成自然语言由 Agent 在事件触发后进行。
"""

import threading
import time
from collections import deque
from dataclasses import dataclass, field

from events import bus as bus_module
from scheduler.jobs import PersistentJobs

PRIORITY_ORDER = {"low": 0, "normal": 1, "high": 2}


@dataclass
class RuleOutcome:
    allowed: bool
    reason: str = ""


@dataclass
class CompanionRule:
    name: str
    situation: str
    interval_seconds: int = 1800
    event: str = bus_module.COMPANION
    priority: str = "normal"
    cooldown_seconds: int = 3600
    max_per_day: int = 4
    quiet_hours: tuple = (23, 7)
    condition: object = None
    payload: dict = field(default_factory=dict)
    trigger: str = ""  # 非空表示这条规则由事件触发，不按间隔检查


def _parse_hours(raw):
    """把 "23-7" 这类偏好解析成 (start, end)。"""
    try:
        start, _, end = str(raw).partition("-")
        return (int(start) % 24, int(end) % 24)
    except (TypeError, ValueError):
        return None


class Scheduler:
    """规则门禁顺序：用户偏好 → 免打扰 → 冷却 → 频率上限 → 条件。"""

    def __init__(
        self,
        bus,
        rules=(),
        preferences=None,
        logger=None,
        clock=time.time,
        tick_seconds=30,
        cooldown_floor=0,
        daily_limit=0,
        max_per_tick=0,
        trace_limit=40,
        store=None,
    ):
        self.bus = bus
        self.rules = list(rules)
        self.preferences = preferences
        self.logger = logger
        self.clock = clock
        self.tick_seconds = tick_seconds
        # 用户设的阀门：只做额外收紧，永远不会让桌宠比规则更吵。
        self.cooldown_floor = cooldown_floor
        self.daily_limit = daily_limit
        # V3：一次最多开口几条；0 表示不限。命中多条时按优先级排队。
        self.max_per_tick = int(max_per_tick or 0)
        # 有持久仓储时，一次性/周期任务落 SQLite；没有时保持旧的内存 Timer 行为。
        self.store = store
        self.jobs = (
            PersistentJobs(store, bus, logger=logger, clock=clock) if store else None
        )
        self.last_jobs = []
        # 为什么开口、为什么跳过，全留在这条缓冲里给开发者面板看。
        self.trace = deque(maxlen=max(5, int(trace_limit or 40)))
        self.last_user_at = None
        self.last_outcome = {}
        self._last_fired = {}
        self._last_checked = {}
        self._fired_today = {}
        self.user_away = False
        self._thread = None
        self._timers = []
        self._stop = threading.Event()
        self._wake = threading.Event()
        bus.subscribe(bus_module.USER_MESSAGE, self._on_user_message)
        bus.subscribe(bus_module.USER_IDLE, self._on_user_idle)
        bus.subscribe(bus_module.USER_RETURNED, self._on_user_returned)

    # ---------- 生命周期 ----------
    def start(self):
        if self._thread and self._thread.is_alive():
            return False
        if self.jobs is not None:
            try:
                self.jobs.recover()
            except Exception as exc:
                if self.logger:
                    self.logger.warning("scheduled job recovery failed: %s", exc)
        self._stop.clear()
        self._wake.clear()
        self._thread = threading.Thread(
            target=self._run, name="deskpet-scheduler", daemon=True
        )
        self._thread.start()
        return True

    def stop(self, timeout=5.0):
        self._stop.set()
        self._wake.set()
        for timer in self._timers:
            timer.cancel()
        self._timers = []
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive())

    def _run(self):
        while not self._stop.is_set():
            self._wake.wait(self._wait_seconds())
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self.tick()
            except Exception as exc:
                if self.logger:
                    self.logger.warning("scheduler tick failed: %s", exc)

    def _wait_seconds(self):
        """不加高频轮询；只在下一个持久任务确实更早时提前醒来。"""
        wait = max(1.0, float(self.tick_seconds or 30))
        if self.jobs is None:
            return wait
        try:
            next_due = self.jobs.next_due()
        except Exception:
            next_due = None
        if next_due is None:
            return wait
        return max(0.2, min(wait, float(next_due) - float(self.clock())))

    # ---------- 一次性定时（本地能力用，不经过模型） ----------
    def schedule_once(
        self,
        seconds,
        event=bus_module.COMPANION,
        *,
        name="",
        idempotency_key="",
        max_attempts=3,
        retryable=True,
        idempotent=True,
        **payload
    ):
        """到点发一个事件。倒计时和提醒用它，不需要模型参与。"""
        if self.jobs is not None:
            job = self.jobs.schedule_once(
                seconds,
                event,
                payload,
                name=name,
                idempotency_key=idempotency_key,
                max_attempts=max_attempts,
                retryable=retryable,
                idempotent=idempotent,
            )
            self._wake.set()
            return job
        timer = threading.Timer(max(1, int(seconds)), self._fire_once, args=(event, payload))
        timer.daemon = True
        self._timers.append(timer)
        timer.start()
        return timer

    def schedule_at(
        self,
        due_at,
        event=bus_module.COMPANION,
        *,
        name="",
        idempotency_key="",
        max_attempts=3,
        retryable=True,
        idempotent=True,
        **payload
    ):
        if self.jobs is None:
            seconds = max(1, float(due_at) - float(self.clock()))
            return self.schedule_once(
                seconds,
                event,
                name=name,
                idempotency_key=idempotency_key,
                max_attempts=max_attempts,
                retryable=retryable,
                idempotent=idempotent,
                **payload
            )
        job = self.jobs.schedule_at(
            due_at,
            event,
            payload,
            name=name,
            idempotency_key=idempotency_key,
            max_attempts=max_attempts,
            retryable=retryable,
            idempotent=idempotent,
        )
        self._wake.set()
        return job

    def schedule_interval(
        self,
        every_seconds,
        event=bus_module.COMPANION,
        *,
        first_delay=None,
        name="",
        idempotency_key="",
        max_attempts=3,
        retryable=True,
        idempotent=True,
        **payload
    ):
        if self.jobs is None:
            raise RuntimeError("周期计划需要持久调度仓储")
        job = self.jobs.schedule_interval(
            every_seconds,
            event,
            payload,
            name=name,
            first_delay=first_delay,
            idempotency_key=idempotency_key,
            max_attempts=max_attempts,
            retryable=retryable,
            idempotent=idempotent,
        )
        self._wake.set()
        return job

    def cancel_job(self, job_id):
        return bool(self.jobs and self.jobs.cancel(job_id))

    def cancel_key_prefix(self, prefix):
        return int(self.jobs.cancel_prefix(prefix)) if self.jobs is not None else 0

    def resume_job(self, job_id):
        ok = bool(self.jobs and self.jobs.resume(job_id))
        if ok:
            self._wake.set()
        return ok

    def job_rows(self, limit=50):
        return self.jobs.rows(limit=limit) if self.jobs is not None else []

    def job_runs(self, job_id=None, limit=50):
        return self.jobs.runs(job_id=job_id, limit=limit) if self.jobs is not None else []

    def job_stats(self):
        if self.jobs is None:
            return {
                "total": 0,
                "pending": 0,
                "running": 0,
                "completed": 0,
                "failed": 0,
                "cancelled": 0,
                "waiting_confirmation": 0,
            }
        return self.jobs.stats()

    def _fire_once(self, event, payload):
        if self._stop.is_set():
            return
        try:
            self.bus.emit(event, **payload)
        except Exception as exc:
            if self.logger:
                self.logger.warning("one-shot timer failed: %s", exc)

    # ---------- 事件触发的主动行为 ----------
    def watch(self, event_name, situation, name=None, **kwargs):
        """订阅一个事件：它来了就按同样的门禁决定要不要开口。

        例如用户回到电脑前打个招呼。事件只触发判断，话术由 Agent 生成。
        """
        rule = CompanionRule(
            name=name or "on_%s" % event_name,
            situation=situation,
            trigger=event_name,
            **kwargs,
        )
        self.rules.append(rule)
        self.bus.subscribe(event_name, lambda event: self._on_trigger(rule, event))
        return rule

    def _on_trigger(self, rule, event):
        now = self.clock()
        outcome = self.evaluate(rule, now)
        self.last_outcome[rule.name] = outcome
        self._record(rule, outcome, now)
        if not outcome.allowed:
            if self.logger:
                self.logger.info("companion rule %s skipped: %s", rule.name, outcome.reason)
            return None
        self._mark_fired(rule, now)
        return self.bus.emit(rule.event, **self._rule_payload(rule, now))

    # ---------- 核心 ----------
    def tick(self, now=None):
        """检查所有规则，返回本轮真正发出的事件。不调用模型。"""
        now = self.clock() if now is None else now
        self.last_jobs = self.jobs.run_due(now) if self.jobs is not None else []
        due = [rule for rule in self.rules if self._is_due(rule, now)]
        due.sort(key=lambda rule: PRIORITY_ORDER.get(rule.priority, 1), reverse=True)

        emitted = []
        for rule in due:
            self._last_checked[rule.name] = now
            outcome = self.evaluate(rule, now)
            self.last_outcome[rule.name] = outcome
            if not outcome.allowed:
                self._record(rule, outcome, now)
                if self.logger:
                    self.logger.info("companion rule %s skipped: %s", rule.name, outcome.reason)
                continue
            if self.max_per_tick and len(emitted) >= self.max_per_tick:
                # 同一轮里挤了好几条：先按优先级放一条，其余留到下一轮。
                outcome = RuleOutcome(False, "本轮已有一条主动消息，先排队")
                self.last_outcome[rule.name] = outcome
                self._record(rule, outcome, now)
                continue
            self._mark_fired(rule, now)
            self._record(rule, outcome, now)
            emitted.append(
                self.bus.emit(rule.event, **self._rule_payload(rule, now))
            )
        return emitted

    def _record(self, rule, outcome, now):
        """记一条判断。只留结论，不记模型内容。"""
        self.trace.append({
            "rule": rule.name,
            "allowed": bool(outcome.allowed),
            "reason": outcome.reason,
            "priority": rule.priority,
            "at": now,
        })

    def trace_rows(self, limit=8):
        rows = list(self.trace)[-max(1, int(limit)):]
        rows.reverse()
        return rows

    def last_skip_reason(self, name):
        outcome = self.last_outcome.get(name)
        return outcome.reason if outcome else ""

    @staticmethod
    def _rule_payload(rule, now):
        return {
            "rule": rule.name,
            "priority": rule.priority,
            "situation": rule.situation,
            "at": now,
            **rule.payload,
        }

    def evaluate(self, rule, now):
        if not self._user_allows():
            return RuleOutcome(False, "用户已关闭主动提醒")
        if self._in_quiet_hours(rule, now):
            return RuleOutcome(False, "处于免打扰时段")
        if self._in_cooldown(rule, now):
            return RuleOutcome(False, "仍在冷却时间")
        if self._over_daily_limit(rule, now):
            return RuleOutcome(False, "已达到今日上限")
        if not self._condition_ok(rule, now):
            return RuleOutcome(False, "触发条件未满足")
        return RuleOutcome(True, "允许打扰")

    # ---------- 各项门禁 ----------
    def _is_due(self, rule, now):
        """首次检查立即执行，之后按 interval_seconds 重新检查。事件规则不走这里。"""
        if rule.trigger:
            return False
        last = self._last_checked.get(rule.name)
        return last is None or (now - last) >= rule.interval_seconds

    def _user_allows(self):
        value = self._preference("proactive_enabled")
        if value is None:
            return True
        return str(value).strip().lower() not in {"0", "false", "no", "off"}

    def _in_quiet_hours(self, rule, now):
        start, end = self._quiet_hours(rule)
        if start == end:
            return False
        hour = time.localtime(now).tm_hour
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def _quiet_hours(self, rule):
        raw = self._preference("quiet_hours")
        parsed = _parse_hours(raw) if raw else None
        return parsed or rule.quiet_hours

    def _in_cooldown(self, rule, now):
        last = self._last_fired.get(rule.name)
        return last is not None and (now - last) < self._cooldown(rule)

    def _cooldown(self, rule):
        """实际冷却时间取规则值与用户设置的较大者：用户只能让它更安静。"""
        user = self._number("proactive.cooldown_seconds", self.cooldown_floor)
        return max(int(rule.cooldown_seconds or 0), int(user or 0))

    def _over_daily_limit(self, rule, now):
        return self._fired_today.get(self._day_key(rule, now), 0) >= self._daily_limit(rule)

    def _daily_limit(self, rule):
        """实际每日上限取规则值与用户设置的小者，同样只能更安静。"""
        rule_limit = max(1, int(rule.max_per_day or 1))
        user = self._number("proactive.max_per_day", self.daily_limit)
        if not user:
            return rule_limit
        return min(rule_limit, max(1, int(user)))

    def _number(self, key, default=0):
        """读一个数字偏好。读不到或格式不对就退回默认值。"""
        raw = self._preference(key)
        if raw is None or raw == "":
            return default
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return default

    def _condition_ok(self, rule, now):
        if rule.condition is None:
            return True
        context = {
            "now": now,
            "rule": rule.name,
            "last_user_at": self.last_user_at,
            "silent_for": (now - self.last_user_at) if self.last_user_at else None,
            "user_away": self.user_away,
            "preferences": self.preferences,
        }
        try:
            return bool(rule.condition(context))
        except Exception as exc:
            # 条件判断出错时保持安静，不打扰用户。
            if self.logger:
                self.logger.warning("rule condition failed for %s: %s", rule.name, exc)
            return False

    # ---------- 状态记录 ----------
    def _mark_fired(self, rule, now):
        self._last_fired[rule.name] = now
        key = self._day_key(rule, now)
        self._fired_today[key] = self._fired_today.get(key, 0) + 1

    @staticmethod
    def _day_key(rule, now):
        return (rule.name, time.strftime("%Y-%m-%d", time.localtime(now)))

    def _on_user_message(self, event):
        self.last_user_at = self.clock()

    def _on_user_idle(self, event):
        self.user_away = True

    def _on_user_returned(self, event):
        self.user_away = False
        self.last_user_at = self.clock()

    def _preference(self, key):
        if not self.preferences:
            return None
        try:
            return self.preferences.get_preference(key)
        except Exception:
            return None
