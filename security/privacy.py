"""隐私中心与权限中心：把「能不能听、能不能看、能不能主动说话」集中到一处。

这里只集中开关，不改变安全规则本身：关掉就是关掉，任何人格都绕不过。
开关存进本地偏好，随时可改；默认值跟当前程序行为一致（还没接的都默认关）。
"""

_FALSE = {"0", "false", "no", "off"}

# 用户可控项：(键, 界面名, 说明, 默认值)
# 默认值取当前程序的真实行为：麦克风、摄像头、文件、真实硬件都还没接，默认关。
CONTROLS = (
    ("listen", "能不能听（麦克风）", "允许桌宠用麦克风听你说话", False),
    (
        "system_audio",
        "能不能采集系统音频",
        "允许桌宠读取电脑正在播放的声音，用于手动开启的实时字幕",
        False,
    ),
    ("see", "能不能看（摄像头）", "允许桌宠用摄像头看你", False),
    ("read_screen", "能不能读屏幕", "允许桌宠读取屏幕尺寸等基本信息", True),
    ("awareness", "看不看你正在用什么软件", "允许桌宠读取前台应用的名称与窗口标题；默认关闭", False),
    (
        "screen_to_model",
        "把屏幕内容交给模型",
        "允许把窗口标题这类屏幕内容发给模型。本地模型不受这条限制；用云端模型时要单独打开",
        False,
    ),
    (
        "subtitle_cloud",
        "字幕能不能发给云端翻译",
        "允许把已经稳定的字幕句子发送到你配置的任务模型做翻译",
        False,
    ),
    ("vision", "能不能截图分析", "允许桌宠在你手动触发时截一张屏幕图，只存本地", False),
    ("proactive", "能不能主动说话", "允许桌宠在你没说话时先开口", True),
    ("run_tasks", "能不能执行任务", "允许桌宠执行需要确认的任务", True),
    ("files", "能不能访问文件", "允许桌宠读写文件", False),
    ("hardware", "能不能控制硬件", "允许桌宠控制真实外部设备", False),
)

# 控件键 -> 偏好键。proactive 复用既有的 proactive_enabled，
# 免得同一种开关散在两个地方。
_PREF_KEYS = {
    "listen": "privacy.listen",
    "system_audio": "privacy.system_audio",
    "see": "privacy.see",
    "read_screen": "privacy.read_screen",
    "awareness": "privacy.awareness",
    "vision": "privacy.vision",
    "screen_to_model": "privacy.screen_to_model",
    "subtitle_cloud": "privacy.subtitle_cloud",
    "proactive": "proactive_enabled",
    "run_tasks": "privacy.run_tasks",
    "files": "privacy.files",
    "hardware": "privacy.hardware",
}

# 权限中心：分类名 -> 控件键。网络模型不是开关，看当前有没有配模型。
PERMISSION_ROWS = (
    ("麦克风", "listen"),
    ("系统音频", "system_audio"),
    ("摄像头", "see"),
    ("屏幕", "read_screen"),
    ("当前应用", "awareness"),
    ("截图分析", "vision"),
    ("字幕云端翻译", "subtitle_cloud"),
    ("文件", "files"),
    ("网络模型", None),
    ("任务执行", "run_tasks"),
    ("外部设备", "hardware"),
)


class PrivacyCenter:
    """把隐私状态和用户开关收在一处。只做读数与开关，不替程序做判断。"""

    def __init__(self, preferences=None, config=None, mic=None, logger=None):
        self.preferences = preferences
        self.config = config
        self.mic = mic
        self.logger = logger

    # ---------- 用户开关 ----------
    def controls(self):
        rows = []
        for key, label, note, default in CONTROLS:
            rows.append(
                {
                    "key": key,
                    "label": label,
                    "note": note,
                    "enabled": self.allows(key),
                    "default": bool(default),
                }
            )
        return rows

    def allows(self, key):
        pref_key, default = self._spec(key)
        value = self._get(pref_key)
        if value is None:
            return bool(default)
        return str(value).strip().lower() not in _FALSE

    def set(self, key, enabled):
        pref_key, _ = self._spec(key)
        if pref_key is None:
            return False
        return self._put(pref_key, "1" if enabled else "0")

    # ---------- 状态视图 ----------
    def describe(self):
        """隐私中心：一句话说清现在能听、能看、能连什么。绝不发给模型。"""
        return {
            "麦克风": "开启" if self._mic_on() else "关闭",
            "系统音频": "开启" if self.allows("system_audio") else "关闭",
            "摄像头": "开启" if self.allows("see") else "关闭",
            "屏幕感知": "开启" if self.allows("read_screen") else "关闭",
            "工作上下文": "开启" if self.allows("awareness") else "关闭",
            "屏幕内容给模型": "允许" if self.allows("screen_to_model") else "不允许",
            "字幕云端翻译": "允许" if self.allows("subtitle_cloud") else "不允许",
            "网络模型": "已连接" if self._has_model() else "未连接",
            "本地记忆": "启用" if self.preferences is not None else "不可用",
        }

    def permission_center(self):
        """权限中心：把七类权限集中成一张清单。"""
        rows = {}
        for label, key in PERMISSION_ROWS:
            rows[label] = self._has_model() if key is None else self.allows(key)
        return rows

    # ---------- 内部 ----------
    def _spec(self, key):
        for item in CONTROLS:
            if item[0] == key:
                return _PREF_KEYS.get(key), item[3]
        return None, False

    def _get(self, pref_key):
        if self.preferences is None or not pref_key:
            return None
        try:
            return self.preferences.get_preference(pref_key)
        except Exception as exc:
            self._warn("privacy read failed for %s: %s", pref_key, exc)
            return None

    def _put(self, pref_key, value):
        if self.preferences is None:
            return False
        try:
            self.preferences.set_preference(pref_key, value)
            return True
        except Exception as exc:
            self._warn("privacy save failed for %s: %s", pref_key, exc)
            return False

    def _mic_on(self):
        if not self.allows("listen"):
            return False
        probe = getattr(self.mic, "listening", None)
        if callable(probe):
            try:
                return bool(probe())
            except Exception:
                return False
        return False

    def _has_model(self):
        config = self.config
        if config is None:
            return False
        for name in ("has_companion", "has_task", "has_llm"):
            probe = getattr(config, name, None)
            if callable(probe):
                try:
                    if probe():
                        return True
                except Exception:
                    continue
        return False

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)
