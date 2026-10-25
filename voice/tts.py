"""语音输出接入点（TTS）。当前只定义接口与默认实现。

不接真实语音合成，也不训练声音。以后用户准备好的语音包实现这三个方法就能接进来：
语音怎么发出来由语音包决定，什么时候说、说什么由程序决定。

和形象一样：情绪只能由程序从 EmotionState 传进来，模型不能直接控制发声。
"""

from abc import ABC, abstractmethod


class SpeechProvider(ABC):
    """语音合成接入点。没有语音包时保持不可用，不报错。"""

    name = "base"

    @abstractmethod
    def is_available(self):
        """现在能不能发声。"""

    @abstractmethod
    def speak(self, text, emotion=None):
        """把一句话说出来，返回是否真的开始发声。"""

    @abstractmethod
    def stop(self):
        """停止发声。"""


class NullSpeechProvider(SpeechProvider):
    """没有语音包时的默认实现：不发声，也不添乱。"""

    name = "null"

    def is_available(self):
        return False

    def speak(self, text, emotion=None):
        return False

    def stop(self):
        return False

    def describe(self):
        return {
            "provider": self.name,
            "available": False,
            "note": "没有接入语音合成",
        }
