"""唤醒与麦克风隐私。当前只做接口、文字匹配和状态机。

桌宠到底有没有在听，用户应该一眼看得出来，所以状态是明确的：
麦克风关闭 / 正在监听 / 等待唤醒词 / 正在录音 / 正在识别。

没有唤醒词模型时用文字匹配（「芳菲，今天天气怎么样」这种）也一样能工作，
不依赖任何音频库，也不需要一直采集麦克风。以后接本地或云端唤醒词模型，
实现 detect 即可，Agent Core 不需要改动。
"""

from abc import ABC, abstractmethod


WAKE_WORDS = ("芳菲", "桌宠", "deskpet", "desk pet")

MIC_OFF = "mic_off"
MIC_LISTENING = "mic_listening"
WAKE_DETECTION = "wake_detection"
RECORDING = "recording"
PROCESSING = "processing"

MIC_STATES = (MIC_OFF, MIC_LISTENING, WAKE_DETECTION, RECORDING, PROCESSING)

MIC_LABELS = {
    MIC_OFF: "关闭",
    MIC_LISTENING: "正在监听",
    WAKE_DETECTION: "等待唤醒词",
    RECORDING: "正在录音",
    PROCESSING: "正在识别",
}

_PUNCTUATION = "，,。.、；;：:！!？?~～ \t"


def match_wake_word(text, words=None):
    """看一句话开头有没有唤醒词。

    返回 (命中的词, 剩下的内容)。没命中时第一个是空串，第二个是原话。
    """
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return "", ""
    stripped = cleaned.lstrip(_PUNCTUATION)
    lowered = stripped.lower()
    for word in (words if words is not None else WAKE_WORDS):
        if not word:
            continue
        if lowered.startswith(str(word).lower()):
            rest = stripped[len(str(word)):].lstrip(_PUNCTUATION)
            return str(word), rest.strip()
    return "", cleaned


class WakeWordProvider(ABC):
    """唤醒词检测接入点。本地模型和云端模型都实现这一套方法。"""

    name = "base"

    @abstractmethod
    def is_available(self):
        """现在能不能检测唤醒词。"""

    @abstractmethod
    def detect(self, audio, sample_rate=None):
        """听到唤醒词返回 True，否则返回 False。"""

    @abstractmethod
    def stop(self):
        """停止检测。"""


class NullWakeWordProvider(WakeWordProvider):
    """没有唤醒词模型时的默认实现：永远听不出来，也不报错。"""

    name = "null"

    def is_available(self):
        return False

    def detect(self, audio, sample_rate=None):
        return False

    def stop(self):
        return False

    def describe(self):
        return {
            "provider": self.name,
            "available": False,
            "note": "没有接入唤醒词检测",
        }


class WakeWordDetector:
    """唤醒词总入口。有模型就用模型，没有模型就用文字匹配。"""

    def __init__(self, provider=None, words=None, on_wake=None, logger=None):
        self.provider = provider or NullWakeWordProvider()
        self.words = tuple(words if words is not None else WAKE_WORDS)
        self.on_wake = on_wake
        self.logger = logger
        self.armed = False
        self.woken = 0
        self.ignored = 0
        self.last_word = ""

    # ---------- 等待唤醒 ----------
    def arm(self):
        """进入等唤醒词的状态。"""
        self.armed = True
        return True

    def disarm(self):
        """不再等唤醒词，后面听到的话直接送大脑。"""
        self.armed = False
        return False

    def available(self):
        """有唤醒词模型，或者能用文字匹配，都算可用。"""
        try:
            if self.provider.is_available():
                return True
        except Exception as exc:
            self._warn("wake provider probe failed: %s", exc)
        return bool(self.words)

    # ---------- 检测 ----------
    def detect(self, audio, sample_rate=None):
        """把一段音频交给唤醒词模型。没有模型就返回 False。"""
        try:
            if not self.provider.is_available():
                return False
            return bool(self.provider.detect(audio, sample_rate=sample_rate))
        except Exception as exc:
            self._warn("wake detect failed: %s", exc)
            return False

    def feed(self, text):
        """听到的一句话：命中唤醒词就醒过来，没命中就忽略。返回 (词, 剩余内容)。"""
        word, rest = match_wake_word(text, self.words)
        if not word:
            self.ignored += 1
            return "", ""
        self.woken += 1
        self.last_word = word
        self.armed = False
        self._wake(rest)
        return word, rest

    def stop(self):
        try:
            self.provider.stop()
        except Exception as exc:
            self._warn("wake stop failed: %s", exc)
        self.armed = False

    def describe(self):
        """开发者面板用的只读视图。绝不发给模型。"""
        return {
            "provider": getattr(self.provider, "name", "?"),
            "available": self.available(),
            "armed": self.armed,
            "words": list(self.words),
            "woken": self.woken,
            "ignored": self.ignored,
            "last_word": self.last_word,
        }

    # ---------- 内部 ----------
    def _wake(self, rest):
        if self.on_wake is None:
            return
        try:
            self.on_wake(rest)
        except Exception as exc:
            self._warn("wake delivery failed: %s", exc)

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)


class MicPrivacy:
    """麦克风状态机。桌宠有没有在听，看这一处就够。"""

    def __init__(self, state=MIC_OFF):
        self.state = state if state in MIC_STATES else MIC_OFF
        self.changes = 0

    def set(self, state):
        if state not in MIC_STATES:
            return False
        if state != self.state:
            self.changes += 1
        self.state = state
        return True

    def label(self):
        """给用户看的四个字，不是给模型看的。"""
        return MIC_LABELS.get(self.state, self.state)

    def listening(self):
        """只有关闭才算没在听。"""
        return self.state != MIC_OFF

    # ---------- 常用切换 ----------
    def off(self):
        return self.set(MIC_OFF)

    def start_listening(self):
        return self.set(MIC_LISTENING)

    def await_wake(self):
        return self.set(WAKE_DETECTION)

    def start_recording(self):
        return self.set(RECORDING)

    def start_processing(self):
        return self.set(PROCESSING)

    def push(self, active):
        """按住说话：按下才开始录，松开就停，不按就不听。"""
        return self.set(RECORDING if active else MIC_OFF)

    def describe(self):
        """开发者面板用的只读视图。绝不发给模型。"""
        return {
            "state": self.state,
            "label": self.label(),
            "listening": self.listening(),
            "changes": self.changes,
        }
