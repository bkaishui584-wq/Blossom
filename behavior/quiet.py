"""静默模式。用户不想被打扰时，桌宠彻底安静下来。

和专注模式分开：专注是「你在忙，我让开」，静默是「我现在不想听你说」。

静默时：
    不发声（连语音一起停）
    不主动聊天
    不提醒（连高优先级的也拦）
但角色动作照常：眨眼、呼吸、小幅晃动都还在，看起来还活着，只是不说话。

由用户在设置里开关，记进偏好，下次启动仍然静默。
只回答「能不能说、能不能主动、能不能动」三个问题，全是程序判断，不经过模型。
"""

QUIET_KEY = "quiet_mode"

_FALSE_LIKE = {"0", "false", "no", "off", ""}


def _truthy(value):
    if value is None:
        return False
    return str(value).strip().lower() not in _FALSE_LIKE


class QuietMode:
    """静默开关。三种能力分开回答，调用方各取所需。"""

    def __init__(self, preferences=None, logger=None, key=QUIET_KEY, initial=False):
        self.preferences = preferences
        self.logger = logger
        self.key = key
        self.initial = bool(initial)
        self.override = None  # 本次运行内的手动开关，优先于偏好

    # ---------- 状态 ----------
    def enabled(self):
        if self.override is not None:
            return self.override
        stored = self._read()
        if stored is None:
            return self.initial
        return _truthy(stored)

    def set(self, on):
        """用户开关：立刻生效，并记进偏好，下次启动还记得。"""
        on = bool(on)
        self.override = on
        self._write("1" if on else "0")
        return on

    def toggle(self):
        return self.set(not self.enabled())

    # ---------- 静默时各让一步 ----------
    def allows_speech(self):
        """静默时连声音一起停。"""
        return not self.enabled()

    def allows_proactive(self):
        """静默时不主动聊天、也不提醒。"""
        return not self.enabled()

    def allows_idle(self):
        """静默只管说不说话，不管动不动；角色照常眨眼呼吸。"""
        return True

    def describe(self):
        """开发者面板用的只读视图。绝不发给模型。"""
        return {
            "enabled": self.enabled(),
            "source": "manual" if self.override is not None else "preference",
            "key": self.key,
        }

    # ---------- 内部 ----------
    def _read(self):
        if self.preferences is None:
            return None
        try:
            return self.preferences.get_preference(self.key)
        except Exception as exc:
            self._warn("quiet preference read failed: %s", exc)
            return None

    def _write(self, value):
        if self.preferences is None:
            return False
        try:
            self.preferences.set_preference(self.key, value)
            return True
        except Exception as exc:
            self._warn("quiet preference write failed: %s", exc)
            return False

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)
