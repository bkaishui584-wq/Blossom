"""语音输入接入点。当前只定义接口与默认实现，不接真实麦克风，也不做语音训练。

以后用户准备好的语音包（或用本机麦克风 + STT）实现这几个方法就能接进来，
Agent Core 不需要任何改动。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class VoiceInput:
    """一次语音识别的结果。可以是中间结果，也可以是最终结果。"""

    text: str
    final: bool = True
    confidence: float = 1.0
    source: str = ""
    data: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "text": self.text,
            "final": bool(self.final),
            "confidence": self.confidence,
            "source": self.source,
            "data": dict(self.data),
        }


class VoiceProvider(ABC):
    """语音接入点。没有麦克风或没有语音包时保持不可用，不报错。"""

    name = "base"

    @abstractmethod
    def is_available(self):
        """现在能不能听到声音。"""

    @abstractmethod
    def start(self):
        """开始监听，返回是否真的开始。"""

    @abstractmethod
    def stop(self):
        """停止监听。"""

    @abstractmethod
    def listen(self, timeout=None):
        """取一条语音，返回 VoiceInput；这段时间没有语音就返回 None。"""


class NullVoiceProvider(VoiceProvider):
    """没有语音包时的默认实现：永远听不到，但也永远不添乱。"""

    name = "null"

    def is_available(self):
        return False

    def start(self):
        return False

    def stop(self):
        return False

    def listen(self, timeout=None):
        return None

    def describe(self):
        return {
            "provider": self.name,
            "available": False,
            "listening": False,
            "note": "没有接入语音输入",
        }
