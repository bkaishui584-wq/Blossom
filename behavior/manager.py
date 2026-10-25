"""待机与互动行为。全部由程序规则判断，绝不轮询模型。

链路：
    调度器 / 界面定时 -> BehaviorManager.idle()          -> PetAction -> AvatarProvider
    界面互动事件      -> BehaviorManager.on_interaction() -> PetAction -> AvatarProvider
    主动提醒事件      -> BehaviorManager.should_interact() -> 值不值得开口

LLM 只负责「说什么」，不负责「要不要动、要不要待机」。
待机是程序行为，不需要模型参与，也不允许模型参与。
"""

import random
import time
from abc import ABC, abstractmethod

from behavior import actions
from events import bus as bus_module
from persona.policy import PersonaPolicy

# should_interact 的三种结论
INTERACT = "interact"
WAIT = "wait"
SILENT = "silent"

PRIORITY_ORDER = {"low": 0, "normal": 1, "high": 2}

DEFAULT_IDLE_GAP = (45, 180)
DEFAULT_QUIET_HOURS = (23, 7)
DEFAULT_ACTIVE_WINDOW = 600
DEFAULT_SLEEP_AFTER = 1800
DEFAULT_COOLDOWN = 300


def _default_hour(timestamp):
    return time.localtime(timestamp).tm_hour


class WindowProvider(ABC):
    """窗口状态接入点（预留）。

    第一阶段不读真实窗口，只留接口：以后要判断「用户是不是全屏、在不在专注」，
    实现 is_busy() 换进来就行，BehaviorManager 不用改。
    """

    @abstractmethod
    def is_busy(self, now=None):
        """用户当前是否在忙（全屏、演示、专注模式）。"""

    def is_available(self):
        """现在能不能读到窗口状态。默认当作能读。"""
        return True


class NullWindowProvider(WindowProvider):
    """没有窗口探测时的默认实现：永远当作不忙。"""

    def is_busy(self, now=None):
        return False


def _parse_hours(raw):
    try:
        start, _, end = str(raw).partition("-")
        return (int(start) % 24, int(end) % 24)
    except (TypeError, ValueError):
        return None


def _in_hours(hours, hour):
    start, end = hours
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


class BehaviorManager:
    """决定桌宠什么时候动、动什么、什么时候该安静。全部程序判断。"""

    def __init__(
        self,
        emotion=None,
        avatar=None,
        persona=None,
        preferences=None,
        bus=None,
        logger=None,
        window=None,
        clock=time.time,
        hour_of=None,
        idle_gap=DEFAULT_IDLE_GAP,
        quiet_hours=DEFAULT_QUIET_HOURS,
        active_window=DEFAULT_ACTIVE_WINDOW,
        sleep_after=DEFAULT_SLEEP_AFTER,
        cooldown=DEFAULT_COOLDOWN,
        random_source=None,
        quiet=None,
        policy=None,
    ):
        self.emotion = emotion
        self.avatar = avatar
        self.persona = persona
        self.preferences = preferences
        self.bus = bus
        self.logger = logger
        self.window = window or NullWindowProvider()
        self.clock = clock
        self.hour_of = hour_of or _default_hour
        self.idle_gap = idle_gap
        self.quiet_hours = quiet_hours
        self.active_window = active_window
        self.sleep_after = sleep_after
        self.cooldown = cooldown
        self.random = random_source or random
        self.quiet = quiet
        self.policy = policy or PersonaPolicy(preferences)
        self.last_activity = None
        self.last_idle_at = None
        self.last_proactive_at = None
        self.last_action = None
        self.idle_count = 0
        self.interaction_count = 0
        self.sleeping = False
        self.away = False
        self._gap = self._next_gap()

    # ---------- 活动记录 ----------
    def note_activity(self, now=None):
        """用户有动作（发消息、点桌宠）时调用，用来判断「多久没互动」。"""
        now = self.clock() if now is None else now
        self.last_activity = now
        self.sleeping = False
        return now

    def silent_for(self, now=None):
        now = self.clock() if now is None else now
        if self.last_activity is None:
            return None
        return max(0.0, now - self.last_activity)

    # ---------- 值不值得打扰 ----------
    def should_interact(self, priority="normal", now=None, urgent=False):
        """程序评分，不调用模型，也不生成话术。

        返回 INTERACT（可以开口）/ WAIT（先等等）/ SILENT（保持安静）。
        高优先级或 urgent 的提醒可以穿过免打扰和冷却，仍不穿过用户总开关。
        """
        now = self.clock() if now is None else now
        high = urgent or PRIORITY_ORDER.get(
            str(priority).lower(), PRIORITY_ORDER["normal"]
        ) >= PRIORITY_ORDER["high"]
        if not self._user_allows():
            return SILENT
        if not self._quiet_allows():
            return SILENT
        policy = self._policy_decision(priority, urgent)
        if policy["verdict"] == "silent":
            return SILENT
        if policy["verdict"] == "wait":
            return WAIT
        if high:
            self.last_proactive_at = now
            return INTERACT
        if self._in_quiet_hours(now):
            return SILENT
        if self._busy(now):
            return WAIT
        if self._in_cooldown(now):
            return WAIT
        silent = self.silent_for(now)
        if silent is not None and silent < self._active_window():
            return WAIT
        self.last_proactive_at = now
        return INTERACT

    # ---------- 待机 ----------
    def idle(self, now=None):
        """界面或调度器定时调用。不弹窗、不说话、不调用模型。

        多数时候返回 None。深夜且很久没人理才会去睡觉。
        """
        now = self.clock() if now is None else now
        if self._should_sleep(now):
            return self._emit(actions.SLEEP_ACTION, now, sleeping=True)
        if self.away or self.sleeping or self._in_quiet_hours(now):
            return None
        if not self._idle_due(now):
            return None
        return self._emit(self._pick_motion(), now)

    # ---------- 互动 ----------
    def on_interaction(self, name, now=None):
        """用户点 / 双击 / 拖 / 靠近桌宠时的反应。程序映射，不问模型。"""
        now = self.clock() if now is None else now
        self.note_activity(now)
        self.interaction_count += 1
        return self.apply(actions.interaction_action(name))

    # ---------- 用户活动（外部键盘鼠标感知） ----------
    def user_away(self, now=None):
        """用户离开电脑：停下待机小动作，静静等着。"""
        self.away = True
        return self.away

    def user_back(self, now=None):
        """用户回到电脑：恢复互动节奏。"""
        self.away = False
        self.note_activity(now)
        return self.away

    # ---------- 下发 ----------
    def apply(self, action):
        """把动作变成 EmotionState + AvatarCommand。任何失败都不影响主流程。"""
        if action is None:
            return None
        state = None
        if action.emotion:
            if self.emotion is not None:
                try:
                    self.emotion.set(action.emotion)
                    state = self.emotion.to_dict()
                except Exception as exc:
                    self._warn("behavior emotion update failed: %s", exc)
            if state is None:
                state = {"emotion": action.emotion}
        if self.avatar is not None:
            try:
                if action.emotion:
                    self.avatar.set_state(state)
                if action.expression:
                    self.avatar.set_expression(action.expression)
                if action.animation:
                    self.avatar.play_animation(action.animation)
            except Exception as exc:
                self._warn("behavior avatar update failed: %s", exc)
        self.last_action = action
        self._publish(action)
        return action

    def describe(self):
        """开发者面板用的只读视图。绝不发给模型。"""
        return {
            "sleeping": self.sleeping,
            "user_away": self.away,
            "daypart": self.daypart(),
            "silent_for": self.silent_for(),
            "last_action": self.last_action.to_dict() if self.last_action else None,
            "idle_count": self.idle_count,
            "interaction_count": self.interaction_count,
            "quiet_hours": list(self._quiet_hours()),
            "window_busy": self._busy(),
            "proactive_allowed": self._user_allows(),
            "context_mode": self.policy.mode(),
            "policy_decision": self._policy_decision(),
            "quiet": bool(self.quiet.enabled()) if self.quiet is not None else False,
        }

    # ---------- 内部 ----------
    def _emit(self, action, now, sleeping=False):
        self.last_idle_at = now
        self._gap = self._next_gap()
        self.idle_count += 1
        if sleeping:
            self.sleeping = True
        return self.apply(action)

    def _pick_motion(self):
        motions = actions.IDLE_MOTIONS
        if not motions:
            return None
        return self.random.choice(list(motions))

    def _idle_due(self, now):
        if self.last_idle_at is None:
            return True
        return (now - self.last_idle_at) >= self._gap

    def _next_gap(self, now=None):
        low, high = self.idle_gap
        factor = self._persona_factor() * self._daypart_factor(now)
        low = max(5, int(low * factor))
        high = max(low, int(high * factor))
        return self.random.randint(low, high)

    def _daypart_factor(self, now=None):
        """晚上更安静：同样的待机节奏按时间段拉长。"""
        return actions.DAYPART_IDLE_FACTOR.get(self.daypart(now), 1.0)

    def daypart(self, now=None):
        """桌宠自己的时间感：早上 / 白天 / 傍晚 / 夜里 / 深夜。"""
        now = self.clock() if now is None else now
        try:
            hour = self.hour_of(now)
        except Exception as exc:
            self._warn("clock read failed: %s", exc)
            hour = 12
        return actions.daypart_of(hour)

    def _persona_factor(self):
        """活泼的桌宠动得多，安静的桌宠动得少；读不到人格就按默认节奏。"""
        traits = {}
        if self.persona is not None:
            try:
                traits = (self.persona.get_profile() or {}).get("traits") or {}
            except Exception:
                traits = {}
        try:
            if float(traits.get("活泼", 0) or 0) >= 60:
                return 0.6
            if float(traits.get("安静", 0) or 0) >= 60:
                return 1.8
        except (TypeError, ValueError):
            pass
        return 1.0

    def _should_sleep(self, now):
        if self.sleeping or not self._in_quiet_hours(now):
            return False
        silent = self.silent_for(now)
        return silent is not None and silent >= self.sleep_after

    def _in_cooldown(self, now):
        if self.last_proactive_at is None:
            return False
        return (now - self.last_proactive_at) < self._cooldown()

    def _cooldown(self):
        try:
            factor = float(self.policy.cooldown_factor())
        except Exception:
            factor = 1.0
        return max(0.0, float(self.cooldown) * factor)

    def _active_window(self):
        try:
            factor = float(self.policy.cooldown_factor())
        except Exception:
            factor = 1.0
        return max(0.0, float(self.active_window) * factor)

    def _policy_decision(self, priority="normal", urgent=False):
        try:
            return self.policy.decision(priority=priority, urgent=urgent)
        except Exception as exc:
            self._warn("persona policy failed: %s", exc)
            return {"verdict": "interact", "reason": "", "mode": "companion"}

    def _in_quiet_hours(self, now):
        return _in_hours(self._quiet_hours(), self.hour_of(now))

    def _quiet_hours(self):
        raw = self._preference("quiet_hours")
        parsed = _parse_hours(raw) if raw else None
        return parsed or self.quiet_hours

    def _busy(self, now=None):
        try:
            return bool(self.window.is_busy(now))
        except Exception as exc:
            self._warn("window probe failed: %s", exc)
            return False

    def _quiet_allows(self):
        """静默模式：连高优先级提醒也拦，读不到就当作没静默。"""
        if self.quiet is None:
            return True
        try:
            return bool(self.quiet.allows_proactive())
        except Exception as exc:
            self._warn("quiet check failed: %s", exc)
            return True

    def _user_allows(self):
        value = self._preference("proactive_enabled")
        if value is None:
            return True
        return str(value).strip().lower() not in {"0", "false", "no", "off"}

    def _preference(self, key):
        if not self.preferences:
            return None
        try:
            return self.preferences.get_preference(key)
        except Exception as exc:
            self._warn("preference read failed for %s: %s", key, exc)
            return None

    def _publish(self, action):
        if not self.bus:
            return
        try:
            self.bus.emit(bus_module.BEHAVIOR, **action.to_dict())
        except Exception as exc:
            self._warn("behavior publish failed: %s", exc)

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)
