"""形象接口。默认实现只记录状态，不依赖 Live2D。"""

from abc import ABC, abstractmethod

# 情绪到表情名的默认映射，未来 Live2D 接入沿用同一套名字。
EMOTION_EXPRESSIONS = {
    "neutral": "normal",
    "happy": "smile",
    "sad": "sad",
    "angry": "angry",
    "tired": "sleepy",
    "excited": "excited",
    "thinking": "thinking",
    "curious": "curious",
    "annoyed": "annoyed",
    "calm": "normal",
    "sleepy": "sleepy",
}


class AvatarProvider(ABC):
    """形象接入点。用户准备好的 Live2D 实现这三个方法即可接入。"""

    @abstractmethod
    def set_expression(self, expression):
        """切换表情。"""

    @abstractmethod
    def play_animation(self, animation):
        """播放动作。"""

    @abstractmethod
    def set_state(self, state):
        """接收 Agent 的状态（含情绪）。"""


class DefaultAvatarProvider(AvatarProvider):
    """没有 Live2D 时的占位实现：只记录，不渲染。"""

    name = "default"

    def __init__(self):
        self.expression = "normal"
        self.animation = None
        self.state = {}

    def set_expression(self, expression):
        self.expression = str(expression or "normal")
        return self.expression

    def play_animation(self, animation):
        self.animation = animation
        return self.animation

    def set_state(self, state):
        self.state = dict(state or {})
        emotion = self.state.get("emotion")
        if emotion:
            self.set_expression(EMOTION_EXPRESSIONS.get(emotion, emotion))
        return self.state

    def snapshot(self):
        return {
            "expression": self.expression,
            "animation": self.animation,
            "state": dict(self.state),
        }
