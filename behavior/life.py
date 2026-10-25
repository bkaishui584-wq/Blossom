"""桌宠的生命状态。

八种状态：待机、活跃、在想、在说话、工作、休息、睡觉、出错。状态由程序按事件推进，
模型不参与，也不能凭空改状态。每次切换都发一条 LIFE_STATE 事件给界面。

这三种短状态对应的都是真事件，不是拿模型轮询凑出来的"生命感"：
    在想   模型调用开始
    在说话 回复已经交出去（界面上要显示了 / 要念出来了）
    出错   模型或工具报了错
"""

import time

from events import bus as bus_module

IDLE = "idle"
ACTIVE = "active"
THINKING = "thinking"
TALKING = "talking"
WORKING = "working"
RESTING = "resting"
SLEEPING = "sleeping"
ERROR = "error"

STATES = (IDLE, ACTIVE, THINKING, TALKING, WORKING, RESTING, SLEEPING, ERROR)

LABELS = {
    IDLE: "待机",
    ACTIVE: "活跃",
    THINKING: "在想",
    TALKING: "在说话",
    WORKING: "工作中",
    RESTING: "休息",
    SLEEPING: "睡觉",
    ERROR: "出错",
}

# 允许的转换。睡觉只能从待机或休息进入，也要能醒过来。
# 在想 / 在说话是瞬态，进出都要自由；出错可以从任何状态进入，也可以自己恢复。
TRANSITIONS = {
    IDLE: {ACTIVE, THINKING, TALKING, WORKING, RESTING, SLEEPING, ERROR},
    ACTIVE: {IDLE, THINKING, TALKING, WORKING, RESTING, SLEEPING, ERROR},
    THINKING: {IDLE, ACTIVE, TALKING, WORKING, ERROR},
    TALKING: {IDLE, ACTIVE, THINKING, WORKING, ERROR},
    WORKING: {IDLE, ACTIVE, THINKING, TALKING, RESTING, SLEEPING, ERROR},
    RESTING: {IDLE, ACTIVE, THINKING, TALKING, WORKING, SLEEPING, ERROR},
    SLEEPING: {IDLE, ACTIVE, ERROR},
    ERROR: {IDLE, ACTIVE, THINKING, WORKING},
}


class LifeStateMachine:
    """把用户活动和任务进度变成桌宠的生命状态。非法转换直接拒绝。"""

    def __init__(
        self, bus=None, logger=None, clock=time.time, initial=IDLE, preferences=None
    ):
        self.bus = bus
        self.logger = logger
        self.clock = clock
        self.preferences = preferences
        self.state = initial if initial in STATES else IDLE
        self.reason = "启动"
        self.changed_at = clock()
        self.history = []

    def attach(self, bus):
        """订阅用户活动事件。没有总线时保持手动驱动。"""
        self.bus = bus
        if bus is None:
            return self
        bus.subscribe(bus_module.USER_MESSAGE, lambda event: self.note_user_message())
        bus.subscribe(bus_module.USER_IDLE, lambda event: self.transition(IDLE, "用户离开"))
        bus.subscribe(
            bus_module.USER_RETURNED, lambda event: self.transition(ACTIVE, "用户回来了")
        )
        bus.subscribe(
            bus_module.TOOL_COMPLETED, lambda event: self.note_work("刚跑完一步工具")
        )
        return self

    def can(self, state):
        return state in STATES and state in TRANSITIONS.get(self.state, set())

    def transition(self, state, reason=""):
        """切到新状态。非法转换返回 False，不硬改。"""
        state = str(state or "").strip().lower()
        if state == self.state:
            return False
        if not self.can(state):
            if self.logger:
                self.logger.info("life state %s -> %s refused", self.state, state)
            return False
        previous = self.state
        self.state = state
        self.reason = str(reason or "")
        self.changed_at = self.clock()
        self.history.append(
            {"from": previous, "to": state, "reason": self.reason, "at": self.changed_at}
        )
        del self.history[:-20]
        self._emit(previous)
        return True

    # ---------- 事件驱动的入口 ----------
    def note_user_message(self):
        # 用户开口，就说明它刚才那句话已经说完了。
        return self.transition(ACTIVE, "用户说话了")

    def note_thinking(self, reason="在想怎么回"):
        """模型调用开始。这是真事件，不是拿随机数装的。"""
        return self.transition(THINKING, reason)

    def note_talking(self, reason="把话说出来"):
        """回复已经交出去：要么显示在界面上，要么交给语音念。"""
        return self.transition(TALKING, reason)

    def note_error(self, reason="刚才那一步出错了"):
        return self.transition(ERROR, reason)

    def note_settled(self, reason="没在忙了"):
        """从瞬态回到常态。出错也好、说完也好，都可以走这一步。"""
        if self.state in (THINKING, TALKING, ERROR):
            return self.transition(ACTIVE, reason)
        return False

    def note_work(self, reason="开始处理任务"):
        return self.transition(WORKING, reason)

    def note_rest(self, reason="用户让休息"):
        return self.transition(RESTING, reason)

    def note_sleep(self, reason="进入睡眠时段"):
        return self.transition(SLEEPING, reason)

    def note_wake(self, reason="用户回来了"):
        return self.transition(ACTIVE, reason)

    def note_idle(self, reason="用户离开"):
        return self.transition(IDLE, reason)

    # ---------- 只读 ----------
    def label(self):
        return LABELS.get(self.state, self.state)

    def describe(self):
        return {
            "state": self.state,
            "label": self.label(),
            "reason": self.reason,
            "changed_at": self.changed_at,
            "allowed": sorted(TRANSITIONS.get(self.state, set())),
        }

    def _emit(self, previous):
        if self.bus is None:
            return
        try:
            self.bus.emit(
                bus_module.LIFE_STATE,
                state=self.state,
                previous=previous,
                label=self.label(),
                reason=self.reason,
            )
        except Exception as exc:
            if self.logger:
                self.logger.warning("life state publish failed: %s", exc)
