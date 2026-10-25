"""语音管理：听到的话怎么进大脑，要说的话怎么交给语音包。

只做串联，不碰模型、工具和数据库。

输入链路：VoiceProvider.listen() -> VoiceInput -> on_text(text) -> AgentCore.respond()
识别链路：音频 -> TranscribePipeline.transcribe() -> 文字 -> on_text(text)
输出链路：程序决定要说的话 -> SpeechPipeline.say(text, emotion) -> SpeechProvider.speak()

语音包只负责「听到什么」和「怎么发声」，Agent Core 只负责「听懂之后做什么」，
所以换语音包不需要改 Agent Core，Agent Core 也不知道语音的存在。
没有麦克风、没有语音包或没有识别引擎时各条管线都保持安静，不影响桌面桌宠的其它功能。
"""

from voice.interface import NullVoiceProvider, VoiceInput
from voice.stt import NullSttProvider
from voice.tts import NullSpeechProvider


class VoicePipeline:
    """语音到 Agent 的单向通道。"""

    def __init__(self, provider=None, on_text=None, logger=None, min_confidence=0.0):
        self.provider = provider or NullVoiceProvider()
        self.on_text = on_text
        self.logger = logger
        self.min_confidence = float(min_confidence or 0.0)
        self.listening = False
        self.last_input = None
        self.accepted = 0
        self.rejected = 0

    # ---------- 状态 ----------
    def available(self):
        try:
            return bool(self.provider.is_available())
        except Exception as exc:
            self._warn("voice provider probe failed: %s", exc)
            return False

    def start(self):
        if not self.available():
            self.listening = False
            return False
        try:
            self.listening = bool(self.provider.start())
        except Exception as exc:
            self._warn("voice start failed: %s", exc)
            self.listening = False
        return self.listening

    def stop(self):
        try:
            self.provider.stop()
        except Exception as exc:
            self._warn("voice stop failed: %s", exc)
        self.listening = False

    # ---------- 取一条 ----------
    def poll(self, timeout=None):
        """取一条语音交给 Agent。没在听、或者这段没声音，都返回 None。"""
        if not self.listening:
            return None
        try:
            heard = self.provider.listen(timeout=timeout)
        except Exception as exc:
            self._warn("voice listen failed: %s", exc)
            return None
        if heard is None:
            return None
        return self.handle(heard)

    def handle(self, heard):
        """处理一条语音结果。中间结果只更新状态，不打扰 Agent。"""
        if isinstance(heard, str):
            heard = VoiceInput(text=heard)
        if not isinstance(heard, VoiceInput):
            self.rejected += 1
            return None
        self.last_input = heard
        if not heard.final:
            self.rejected += 1
            return None
        text = (heard.text or "").strip()
        if not text:
            self.rejected += 1
            return None
        try:
            confidence = float(heard.confidence)
        except (TypeError, ValueError):
            confidence = 1.0
        if confidence < self.min_confidence:
            self.rejected += 1
            return None
        self.accepted += 1
        self._deliver(text, heard)
        return heard

    def feed(self, text, **data):
        """不经过麦克风，直接把一句话当成语音输入。测试和以后的回灌都用它。"""
        return self.handle(
            VoiceInput(text=str(text or ""), source="manual", data=dict(data))
        )

    def describe(self):
        """开发者面板用的只读视图。绝不发给模型。"""
        return {
            "provider": getattr(self.provider, "name", "?"),
            "available": self.available(),
            "listening": self.listening,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "last_input": self.last_input.to_dict() if self.last_input else None,
        }

    # ---------- 内部 ----------
    def _deliver(self, text, heard):
        if self.on_text is None:
            return
        try:
            self.on_text(text)
        except Exception as exc:
            self._warn("voice delivery failed: %s", exc)

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)


class SpeechPipeline:
    """要说的话到语音包的单向通道。程序决定说什么，语音包只负责发声。"""

    def __init__(self, provider=None, logger=None, max_chars=200):
        self.provider = provider or NullSpeechProvider()
        self.logger = logger
        self.max_chars = max(1, int(max_chars or 1))
        self.spoken = 0
        self.skipped = 0
        self.last_text = ""

    def available(self):
        try:
            return bool(self.provider.is_available())
        except Exception as exc:
            self._warn("speech provider probe failed: %s", exc)
            return False

    def say(self, text, emotion=None):
        """说一句。没有语音包、或者内容为空，就什么都不做并返回 False。"""
        prepared = self._prepare(text)
        if not prepared:
            self.skipped += 1
            return False
        if not self.available():
            self.skipped += 1
            return False
        try:
            started = self.provider.speak(prepared, emotion=emotion)
        except Exception as exc:
            self._warn("speech failed: %s", exc)
            self.skipped += 1
            return False
        if not started:
            self.skipped += 1
            return False
        self.spoken += 1
        self.last_text = prepared
        return True

    def stop(self):
        try:
            self.provider.stop()
        except Exception as exc:
            self._warn("speech stop failed: %s", exc)

    def describe(self):
        """开发者面板用的只读视图。绝不发给模型。"""
        return {
            "provider": getattr(self.provider, "name", "?"),
            "available": self.available(),
            "spoken": self.spoken,
            "skipped": self.skipped,
            "last_text": self.last_text,
        }

    # ---------- 内部 ----------
    def _prepare(self, text):
        """说出来的内容限长，避免一句提示念个没完。"""
        cleaned = " ".join(str(text or "").split())
        return cleaned[: self.max_chars]

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)


class TranscribePipeline:
    """音频到文字的单向通道。本地 STT 和云端 STT 只要实现 transcribe 就能互换。"""

    def __init__(self, provider=None, on_text=None, logger=None, max_chars=500):
        self.provider = provider or NullSttProvider()
        self.on_text = on_text
        self.logger = logger
        self.max_chars = max(1, int(max_chars or 1))
        self.transcribed = 0
        self.skipped = 0
        self.last_text = ""

    # ---------- 状态 ----------
    def available(self):
        try:
            return bool(self.provider.is_available())
        except Exception as exc:
            self._warn("stt provider probe failed: %s", exc)
            return False

    # ---------- 一段音频 ----------
    def transcribe(self, audio, sample_rate=None, language=None):
        """把一段音频转成文字。没有识别引擎、或者没听出内容，都返回空字符串。"""
        if not self.available():
            self.skipped += 1
            return ""
        try:
            heard = self.provider.transcribe(
                audio, sample_rate=sample_rate, language=language
            )
        except Exception as exc:
            self._warn("stt transcribe failed: %s", exc)
            self.skipped += 1
            return ""
        text = " ".join(str(heard or "").split())[: self.max_chars]
        if not text:
            self.skipped += 1
            return ""
        self.transcribed += 1
        self.last_text = text
        return text

    def feed(self, audio, **kwargs):
        """识别一段音频并把文字交给下游。下游和打字走同一条聊天通道。"""
        text = self.transcribe(audio, **kwargs)
        if not text:
            return ""
        self._deliver(text)
        return text

    def stop(self):
        try:
            self.provider.stop()
        except Exception as exc:
            self._warn("stt stop failed: %s", exc)

    def describe(self):
        """开发者面板用的只读视图。绝不发给模型。"""
        return {
            "provider": getattr(self.provider, "name", "?"),
            "available": self.available(),
            "transcribed": self.transcribed,
            "skipped": self.skipped,
            "last_text": self.last_text,
        }

    # ---------- 内部 ----------
    def _deliver(self, text):
        if self.on_text is None:
            return
        try:
            self.on_text(text)
        except Exception as exc:
            self._warn("stt delivery failed: %s", exc)

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)
