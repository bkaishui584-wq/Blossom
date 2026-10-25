"""情绪状态。只是状态，不直接控制形象。"""

from dataclasses import dataclass

# 当前状态。旧名字继续兼容，新增的是 Persona V2 建议的几种。
EMOTIONS = (
    "neutral", "happy", "sad", "angry", "tired", "excited", "thinking",
    "curious", "annoyed", "calm", "sleepy",
)


@dataclass
class EmotionState:
    emotion: str = "neutral"
    intensity: float = 0.5

    def set(self, emotion, intensity=None):
        """非法情绪不生效，返回 False。"""
        name = str(emotion or "").lower()
        if name not in EMOTIONS:
            return False
        self.emotion = name
        if intensity is not None:
            try:
                self.intensity = max(0.0, min(1.0, float(intensity)))
            except (TypeError, ValueError):
                pass
        return True

    def to_dict(self):
        return {"emotion": self.emotion, "intensity": self.intensity}
