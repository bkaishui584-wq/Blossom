"""语音后端适配。

一套接口，多个实现：系统自带的离线语音、可选的本地识别、可选的实时对话框架。

三条硬规则：
1. 麦克风开关是用户的决定。隐私开关关着，谁都不准开麦，扩展也不行。
2. 没有可用模型或引擎时明确降级，不假装听见了。
3. 外部语音服务必须标出「要联网、要密钥」，不能被当成离线能力。
"""


class VoiceProvider:
    """语音后端接口。谁来当后端都得守这个形状。"""

    name = "voice"
    label = "语音"
    offline = True

    def available(self):
        return False

    def describe(self):
        return {
            "name": self.name,
            "label": self.label,
            "available": self.available(),
            "offline": self.offline,
        }

    def listen(self):
        return {"ok": False, "reason": "这个后端不支持开麦"}

    def stop_listening(self):
        return False

    def speak(self, text):
        return {"ok": False, "reason": "这个后端不支持朗读"}


class SystemVoiceProvider(VoiceProvider):
    """系统自带的朗读与本地识别。默认后端，不联网。"""

    name = "system"
    label = "系统语音（离线）"
    offline = True

    def __init__(self, speaker=None, stt=None, mic=None):
        self.speaker = speaker
        self.stt = stt
        self.mic = mic

    def available(self):
        return any(item is not None for item in (self.speaker, self.stt, self.mic))

    def listen(self):
        if self.mic is None:
            return {"ok": False, "reason": "这台机器上没有可用的麦克风"}
        try:
            started = self.mic.start()
        except Exception as exc:
            return {"ok": False, "reason": "开麦失败：%s" % exc}
        return {"ok": bool(started), "reason": "" if started else "麦克风没起来"}

    def stop_listening(self):
        if self.mic is None:
            return False
        try:
            return bool(self.mic.stop())
        except Exception:
            return False

    def speak(self, text):
        if self.speaker is None:
            return {"ok": False, "reason": "没有可用的朗读引擎"}
        try:
            said = self.speaker.say(str(text or ""))
        except Exception as exc:
            return {"ok": False, "reason": "朗读失败：%s" % exc}
        return {"ok": bool(said) if said is not None else True, "reason": ""}


class WhisperCppProvider(VoiceProvider):
    """whisper.cpp 的本地识别。装了可执行文件和模型才用得上，全程不联网。"""

    name = "whisper.cpp"
    label = "本地语音识别（whisper.cpp）"
    offline = True

    def __init__(self, binary="whisper-cli", model_path="", runner=None):
        self.binary = binary
        self.model_path = model_path
        self.runner = runner

    def available(self):
        import shutil

        if not shutil.which(self.binary):
            return False
        return bool(str(self.model_path or "").strip())

    def describe(self):
        data = super().describe()
        data["model"] = str(self.model_path or "")
        data["note"] = "模型文件要用户自己下，不随包分发"
        return data

    def listen(self):
        return {"ok": False, "reason": "本地识别只负责转写，开麦还是走现有麦克风通道"}

    def transcribe(self, audio_path):
        if not self.available():
            return {"ok": False, "reason": "whisper.cpp 或模型不在，转写用不了", "text": ""}
        if self.runner is None:
            return {"ok": False, "reason": "没有接执行器", "text": ""}
        try:
            text = self.runner([self.binary, "-m", str(self.model_path), "-f", str(audio_path)])
        except Exception as exc:
            return {"ok": False, "reason": "转写失败：%s" % exc, "text": ""}
        return {"ok": bool(text), "text": str(text or ""), "reason": "" if text else "没有识别出内容"}


class TenVoiceProvider(VoiceProvider):
    """TEN Framework 的可选实时语音。

    示例环境依赖外部语音服务和密钥，所以它是「要联网、要密钥」的能力，
    绝不能标成离线。许可证也还没逐项审过，默认不启用。
    """

    name = "ten"
    label = "TEN Framework（实时语音）"
    offline = False

    def __init__(self, endpoint="", api_key="", sdk=None, logger=None):
        self.endpoint = endpoint
        self.api_key = api_key
        self.sdk = sdk
        self.logger = logger
        self.last_error = ""

    def available(self):
        if not self.sdk:
            self.last_error = "还没装 TEN 的运行时"
            return False
        if not (self.endpoint and self.api_key):
            self.last_error = "缺少外部语音服务地址或密钥"
            return False
        return True

    def describe(self):
        data = super().describe()
        data["requires_network"] = True
        data["requires_api_key"] = True
        data["license_status"] = "review-needed"
        data["error"] = self.last_error
        return data

    def listen(self):
        return {"ok": False, "reason": "TEN 后端还没接入，先用系统语音"}

    def speak(self, text):
        return {"ok": False, "reason": "TEN 后端还没接入，先用系统语音"}


class NullVoiceProvider(VoiceProvider):
    """什么都没接时的兜底：明确说没有说话能力。"""

    name = "none"
    label = "没有说话能力"

    def describe(self):
        data = super().describe()
        data["reason"] = "这台机器上没有可用的语音后端"
        return data


def build_voice_provider(registry=None, speaker=None, stt=None, mic=None, privacy=None):
    """挑一个语音后端。

    privacy 是麦克风隐私开关的可调用对象：返回 False 就谁都不许开麦。
    """
    if registry is not None and registry.is_enabled("ten-framework"):
        ten = TenVoiceProvider()
        if ten.available():
            return ten
    if privacy is not None:
        try:
            if not privacy():
                return NullVoiceProvider()
        except Exception:
            return NullVoiceProvider()
    system = SystemVoiceProvider(speaker=speaker, stt=stt, mic=mic)
    if system.available():
        return system
    if registry is not None and registry.is_enabled("whisper-cpp"):
        local = WhisperCppProvider()
        if local.available():
            return local
    return NullVoiceProvider()
