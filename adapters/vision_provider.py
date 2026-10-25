"""视觉后端适配。

屏幕感知的顺序是「先语义、后像素」：
1. 先用 Windows UI Automation 读公开的界面语义（应用名、窗口标题、控件文字）。
2. OCR 或视觉模型只当按需补充，默认关着。

Lite.AI.ToolKit 是 GPL-3.0，和商业分发冲突。这个适配器只留接口，
不会去加载它的代码，也不会把它复制进发行包。
"""


class VisionProvider:
    name = "vision"
    label = "视觉"
    offline = True
    needs_screen = True

    def available(self):
        return False

    def describe(self):
        return {
            "name": self.name,
            "label": self.label,
            "available": self.available(),
            "offline": self.offline,
            "needs_screen": self.needs_screen,
        }

    def snapshot(self):
        """拿一份当前屏幕的语义信息。只读，不截图。"""
        return {"ok": False, "reason": "这个后端没有实现语义读取"}


class NullVisionProvider(VisionProvider):
    name = "none"
    label = "没有屏幕感知"
    needs_screen = False

    def describe(self):
        data = super().describe()
        data["reason"] = "屏幕感知默认关闭，或者这台机器上没有可用的实现"
        return data


class UiaVisionProvider(VisionProvider):
    """Windows 可访问性语义。开发阶段不引入额外依赖，拿得到就用，拿不到就说没有。"""

    name = "uia"
    label = "屏幕语义（UI Automation）"
    offline = True

    def __init__(self, reader=None, privacy=None):
        self.reader = reader
        self.privacy = privacy
        self.last_error = ""

    def available(self):
        return self.reader is not None and self._privacy_on()

    def _privacy_on(self):
        if self.privacy is None:
            return True
        try:
            return bool(self.privacy())
        except Exception:
            return False

    def describe(self):
        data = super().describe()
        data["note"] = "只读公开的界面语义，不截图、不录键盘、不做身份推断"
        return data

    def snapshot(self):
        if not self._privacy_on():
            return {"ok": False, "reason": "用户把屏幕感知关着"}
        if self.reader is None:
            return {"ok": False, "reason": "没有接可用的语义读取实现"}
        try:
            data = self.reader()
        except Exception as exc:
            self.last_error = str(exc)
            return {"ok": False, "reason": "读取失败：%s" % exc}
        return {"ok": True, "data": data or {}}


class LiteAiVisionProvider(VisionProvider):
    """Lite.AI.ToolKit 的占位适配。许可证没过审之前它一步都不能走。"""

    name = "lite.ai.toolkit"
    label = "Lite.AI.ToolKit（本地视觉）"
    offline = True
    license = "GPL-3.0"
    license_status = "blocked"

    def __init__(self, module=None, logger=None):
        self.module = module
        self.logger = logger
        self.last_error = ""

    def available(self):
        return False

    def describe(self):
        data = super().describe()
        data["license"] = self.license
        data["license_status"] = self.license_status
        data["reason"] = "GPL-3.0 与商业分发冲突，审查通过前不得链接或复制进发行包"
        return data

    def snapshot(self):
        return {"ok": False, "reason": "许可证未过审阅，这个后端不会加载"}


def build_vision_provider(registry=None, reader=None, privacy=None):
    """挑一个屏幕感知后端：默认先看语义，视觉模型只在明确许可后才考虑。"""
    if privacy is not None:
        try:
            if not privacy():
                return NullVisionProvider()
        except Exception:
            return NullVisionProvider()
    semantic = UiaVisionProvider(reader=reader, privacy=privacy)
    if semantic.available():
        return semantic
    return NullVisionProvider()
