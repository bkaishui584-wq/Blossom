"""桌宠行为动作表。纯数据，不含逻辑，也不调用模型。

Agent 只决定 EmotionState；具体表现由 BehaviorManager 转成这里的 PetAction，
再通过 AvatarProvider 下发。未来 Live2D 只要认这些动作名就能接入。
"""

from dataclasses import dataclass, field, replace

# 动作种类：行为层只发这几类，界面与形象层按自己的能力实现。
IDLE = "idle"
SPEAK = "speak"
ANIMATE = "animate"
SLEEP = "sleep"
REMIND = "remind"
INTERACT = "interact"

KINDS = (IDLE, SPEAK, ANIMATE, SLEEP, REMIND, INTERACT)

# 桌宠自己的时间感：一天分成几段，用来决定更安静还是更活跃。
MORNING = "morning"
DAY = "day"
EVENING = "evening"
NIGHT = "night"
SLEEP = "sleep"

DAYPARTS = (MORNING, DAY, EVENING, NIGHT, SLEEP)

DAYPART_LABELS = {
    MORNING: "早上",
    DAY: "白天",
    EVENING: "傍晚",
    NIGHT: "夜里",
    SLEEP: "深夜",
}

# 待机间隔倍率：越晚动得越少。
DAYPART_IDLE_FACTOR = {
    MORNING: 1.0,
    DAY: 1.0,
    EVENING: 1.2,
    NIGHT: 1.8,
    SLEEP: 3.0,
}


def daypart_of(hour):
    """几点算哪个时间段。纯规则，不读时钟也不问模型。"""
    try:
        hour = int(hour) % 24
    except (TypeError, ValueError):
        hour = 12
    if hour < 5:
        return SLEEP
    if hour < 9:
        return MORNING
    if hour < 18:
        return DAY
    if hour < 22:
        return EVENING
    return NIGHT


@dataclass
class PetAction:
    kind: str
    emotion: str = ""
    expression: str = ""
    animation: str = ""
    reason: str = ""
    data: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "kind": self.kind,
            "emotion": self.emotion,
            "expression": self.expression,
            "animation": self.animation,
            "reason": self.reason,
            "data": dict(self.data),
        }


# 随机待机：都是小动作，不弹窗、不说话、不调模型。
IDLE_MOTIONS = (
    PetAction(kind=IDLE, emotion="calm", animation="blink", reason="偶尔眨眼"),
    PetAction(kind=IDLE, emotion="curious", animation="look_around", reason="随便看看"),
    PetAction(kind=IDLE, emotion="neutral", animation="breathe", reason="安静待机"),
    PetAction(kind=IDLE, emotion="happy", animation="sway", reason="轻轻晃一下"),
)

SLEEP_ACTION = PetAction(
    kind=SLEEP, emotion="sleepy", animation="sleep", reason="深夜且很久没有互动"
)

# 用户直接操作桌宠时的反应。由程序映射，不经过模型。
INTERACTIONS = {
    "click": PetAction(
        kind=INTERACT, emotion="happy", animation="nod", reason="被点了一下"
    ),
    "double_click": PetAction(
        kind=INTERACT, emotion="excited", animation="jump", reason="被双击"
    ),
    "drag": PetAction(
        kind=INTERACT, emotion="curious", animation="dangle", reason="被拖动"
    ),
    "long_press": PetAction(
        kind=INTERACT, emotion="annoyed", animation="puff", reason="被长按"
    ),
    "right_click": PetAction(
        kind=INTERACT, emotion="neutral", animation="wave", reason="被右键"
    ),
    "pointer_near": PetAction(
        kind=INTERACT, emotion="curious", animation="look_at", reason="鼠标靠近"
    ),
    "pointer_away": PetAction(
        kind=INTERACT, emotion="calm", animation="look_away", reason="鼠标离开"
    ),
}


def interaction_action(name, **data):
    """按名字取互动动作；不认识的名字也不报错，只是没有专属表现。"""
    key = str(name or "")
    action = INTERACTIONS.get(key)
    if action is None:
        return PetAction(kind=INTERACT, reason="未知互动", data={"interaction": key})
    extra = dict(action.data)
    extra["interaction"] = key
    extra.update(data)
    return replace(action, data=extra)


def speak_action(text, emotion="happy", reason="程序触发的提醒"):
    return PetAction(
        kind=SPEAK, emotion=emotion, reason=reason, data={"text": str(text)}
    )
