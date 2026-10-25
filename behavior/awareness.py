"""屏幕感知：先知道用户在用什么软件，再谈理解他在做什么。

分级，一级一级往上加，每级都要用户自己开：

1. 活动应用元信息：前台窗口属于哪个进程、窗口标题叫什么。
   只问 Windows 要这三个数，不读画面，不读输入，成本极低。
2. 界面语义：用 Windows UI Automation 读应用公开的可访问性树。
   不同软件暴露的内容差别很大，读不到就说读不到，不编。
3. 画面：复用 behavior/vision.py，用户单独开截图分析才走这一级。

默认全关。聊天、会议这类页面本身就带私人内容，分类判到这类应用就只报应用名，
不往下读语义。这里只产出结构化摘要，不落地原始画面，不记键盘和剪贴板。
换平台只改 provider，界面和 Agent Core 都不用动。
"""

import ctypes
import ctypes.wintypes
import os
import time
from abc import ABC, abstractmethod
from datetime import datetime

LEVELS = ("off", "metadata", "semantic", "vision")
LEVEL_LABELS = {
    "off": "关闭",
    "metadata": "只看当前应用",
    "semantic": "读界面语义",
    "vision": "读画面（截图分析）",
}
LEVEL_PREFERENCE = "awareness.level"
PAUSE_PREFERENCE = "awareness.paused"
EXCLUDE_PREFERENCE = "awareness.exclude"

# 进程名与窗口标题里的关键词 -> 应用类别。命中越多越靠前。
CATEGORY_RULES = (
    ("ide", "代码编辑器", (
        "code", "code - insiders", "pycharm", "idea", "devenv", "sublime_text",
        "notepad++", "goland", "webstorm", "clion", "rider", "cursor",
    ), (".py", ".cpp", ".h", ".rs", ".ts", ".js", ".json", "visual studio")),
    ("terminal", "终端", (
        "cmd", "powershell", "pwsh", "wt", "windowsterminal", "conhost",
        "mintty", "git-bash", "bash", "alacritty", "wezterm",
    ), ("终端", "命令提示符", "powershell")),
    ("browser", "浏览器", (
        "chrome", "msedge", "firefox", "brave", "iexplore", "opera", "vivaldi", "360se", "qqbrowser",
    ), ()),
    ("document", "文档与表格", (
        "winword", "excel", "powerpnt", "wps", "et", "wpp", "onenote",
        "acrobat", "acroread", "sumatrapdf", "foxitreader", "notepad",
    ), (".doc", ".docx", ".xlsx", ".xls", ".pptx", ".pdf", ".md", ".txt")),
    ("design", "设计与图像", (
        "photoshop", "illustrator", "figma", "blender", "krita", "sai", "clipstudiopaint",
        "inkscape", "gimp", "lightroom", "premiere", "afterfx",
    ), (".psd", ".ai", ".sketch", ".blend")),
    ("chat", "聊天与会议", (
        "wechat", "weixin", "qq", "tim", "telegram", "discord", "dingtalk", "feishu",
        "lark", "slack", "zoom", "teams", "tencentmeeting", "wemeet", "skype",
    ), ()),
    ("media", "影音", (
        "potplayer", "vlc", "mpc-hc", "mpv", "bilibili", "cloudmusic", "spotify",
        "netease", "music", "player",
    ), ()),
    ("game", "游戏", (), ("游戏", "game")),
    ("file", "文件管理", ("explorer", "totalcmd", "everything"), ()),
    ("system", "系统与设置", ("systemsettings", "taskmgr", "mmc", "control", "regedit"), ()),
)

# 这些类别的页面本身就带私人内容，只报应用名，不继续往下读。
PRIVATE_CATEGORIES = ("chat",)

CONFIDENCE_LABELS = {
    "confirmed": "已确认",
    "likely": "可能是",
    "unknown": "无法确定",
}


def category_label(category):
    for key, label, _apps, _titles in CATEGORY_RULES:
        if key == category:
            return label
    return "未分类"


def classify(app, title=""):
    """按进程名和窗口标题猜应用类别。

    进程名比窗口标题可靠：窗口标题里常带着别的软件名（比如微信窗口里贴了一段代码），
    所以先把进程名过一遍，全都对不上再看标题。
    """
    app = str(app or "").strip().lower()
    stem = app[:-4] if app.endswith(".exe") else app
    if stem:
        for key, _label, apps, _titles in CATEGORY_RULES:
            if stem in apps:
                return (key, "confirmed")
        for key, _label, apps, _titles in CATEGORY_RULES:
            if any(token in stem for token in apps):
                return (key, "likely")
    text = str(title or "").strip().lower()
    if text:
        for key, _label, _apps, titles in CATEGORY_RULES:
            if any(token in text for token in titles):
                return (key, "likely")
    return ("", "unknown")


class ActivityProvider(ABC):
    """前台应用元信息。只给进程名、标题、进程号。"""

    name = "base"

    @abstractmethod
    def is_available(self):
        """读不读得到当前前台窗口。"""

    @abstractmethod
    def current(self):
        """返回 {"app", "title", "pid"}；读不到就给空字典。"""

    def describe(self):
        """自报家门。自己实现的 provider 不写这一条也能用。"""
        try:
            available = bool(self.is_available())
        except Exception:
            available = False
        return {"provider": self.name, "available": available, "note": ""}


class NullActivityProvider(ActivityProvider):
    """没有元信息来源时的默认实现：读不到，也不影响运行。"""

    name = "null"

    def is_available(self):
        return False

    def current(self):
        return {}

    def describe(self):
        return {"provider": self.name, "available": False, "note": "没有接入活动应用读取"}


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class WindowsActivityProvider(ActivityProvider):
    """Windows 前台窗口。只读窗口标题、进程号和进程名，不读窗口内容。"""

    name = "windows"

    def __init__(self):
        self._user32 = None
        self._kernel32 = None

    def is_available(self):
        return os.name == "nt" and self._api() is not None

    def _api(self):
        if self._user32 is None and os.name == "nt":
            try:
                self._user32 = ctypes.windll.user32
                self._kernel32 = ctypes.windll.kernel32
            except Exception:
                self._user32 = None
        return self._user32

    def current(self):
        user32 = self._api()
        if user32 is None:
            return {}
        try:
            hwnd = user32.GetForegroundWindow()
        except Exception:
            return {}
        if not hwnd:
            return {}
        title = ""
        try:
            length = int(user32.GetWindowTextLengthW(hwnd))
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value or ""
        except Exception:
            title = ""
        pid = 0
        try:
            holder = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(holder))
            pid = int(holder.value)
        except Exception:
            pid = 0
        return {"app": self._process_name(pid), "title": title, "pid": pid}

    def _process_name(self, pid):
        if not pid or self._kernel32 is None:
            return ""
        handle = None
        try:
            handle = self._kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, ctypes.c_ulong(pid)
            )
            if not handle:
                return ""
            size = ctypes.wintypes.DWORD(260)
            buffer = ctypes.create_unicode_buffer(260)
            if not self._kernel32.QueryFullProcessImageNameW(
                handle, 0, buffer, ctypes.byref(size)
            ):
                return ""
            return os.path.basename(buffer.value or "")
        except Exception:
            return ""
        finally:
            if handle:
                try:
                    self._kernel32.CloseHandle(handle)
                except Exception:
                    pass

    def describe(self):
        return {
            "provider": self.name,
            "available": self.is_available(),
            "note": "读取前台应用的进程名与窗口标题",
        }


class SemanticProvider(ABC):
    """界面语义。读应用公开的可访问性树，不读像素。"""

    name = "base"

    @abstractmethod
    def is_available(self):
        """有没有可用的语义读取引擎。"""

    @abstractmethod
    def read(self, limit=8):
        """返回 {"page", "controls": [...]}；读不到就给空。"""

    def describe(self):
        """自报家门。自己实现的 provider 不写这一条也能用。"""
        try:
            available = bool(self.is_available())
        except Exception:
            available = False
        return {"provider": self.name, "available": available, "note": ""}


class NullSemanticProvider(SemanticProvider):
    """没有语义引擎时的默认实现。能力不足就直说。"""

    name = "null"

    def __init__(self, note="没有接入界面语义读取"):
        self.note = note

    def is_available(self):
        return False

    def read(self, limit=8):
        return {}

    def describe(self):
        return {"provider": self.name, "available": False, "note": self.note}


# 这些控件一律不读：名字或角色里带这些词的，当私密字段处理。
_PRIVATE_HINTS = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "api key",
    "apikey",
    "token",
    "密码",
    "口令",
    "密钥",
    "银行卡",
    "信用卡",
)
# 角色名带这些的控件直接丢，连名字都不看。
_PRIVATE_ROLES = ("password",)


class UiAutomationProvider(SemanticProvider):
    """Windows UI Automation。需要本机装了 pywinauto 或 uiautomation。

    只读应用公开的可访问性树，不读像素、不读输入内容。密码框、密钥一类的控件
    连名字都不要。前台窗口先用系统句柄定位：有些应用（模拟器、游戏）不接受
    「找当前活动窗口」这种写法，但按句柄能正常读。
    """

    name = "uia"

    def __init__(self):
        self._engine = None
        self._module = ""
        self._last_error = ""
        self._load()

    def _load(self):
        try:
            import uiautomation as auto  # type: ignore

            self._engine = auto
            self._module = "uiautomation"
            return
        except Exception:
            pass
        try:
            from pywinauto import Desktop  # type: ignore

            self._engine = Desktop
            self._module = "pywinauto"
        except Exception:
            self._engine = None
            self._module = ""

    def is_available(self):
        return self._engine is not None

    # ---------- 私密过滤 ----------
    @staticmethod
    def keep(role, name, is_password=False):
        """这一条能不能进结果。密码框和私密字段一律不要。"""
        if is_password:
            return False
        role_text = str(role or "").lower()
        for token in _PRIVATE_ROLES:
            if token in role_text:
                return False
        blob = ("%s %s" % (role_text, str(name or "").lower()))
        for hint in _PRIVATE_HINTS:
            if hint in blob:
                return False
        return True

    @staticmethod
    def _password_flag(element):
        """控件自己标了是密码框就认。读不到属性就当不是，但不猜内容。"""
        for holder in (element, getattr(element, "element_info", None)):
            if holder is None:
                continue
            for attr in ("IsPassword", "is_password", "ispassword"):
                try:
                    value = getattr(holder, attr, None)
                except Exception:
                    continue
                if callable(value):
                    try:
                        value = value()
                    except Exception:
                        continue
                if value is True:
                    return True
        return False

    @staticmethod
    def _foreground_handle():
        try:
            return int(ctypes.windll.user32.GetForegroundWindow() or 0)
        except Exception:
            return 0

    # ---------- 读取 ----------
    def read(self, limit=8):
        if self._engine is None:
            return {}
        try:
            if self._module == "uiautomation":
                return self._read_uiautomation(limit)
            return self._read_pywinauto(limit)
        except Exception as exc:
            self._last_error = str(exc)
            return {}

    def _read_uiautomation(self, limit):
        window = self._engine.GetForegroundControl()
        if window is None:
            self._last_error = "系统没给出前台窗口"
            return {}
        controls = []
        try:
            children = window.GetChildren()
        except Exception:
            children = []
        for child in children:
            if len(controls) >= max(1, int(limit or 1)):
                break
            try:
                name = str(child.Name or "").strip()
            except Exception:
                name = ""
            if not name:
                continue
            try:
                role = str(child.ControlTypeName or "")
            except Exception:
                role = ""
            if not self.keep(role, name, self._password_flag(child)):
                continue
            controls.append({"role": role, "name": name[:60]})
        try:
            page = str(window.Name or "")
        except Exception:
            page = ""
        return {"page": page[:120], "controls": controls}

    def _read_pywinauto(self, limit):
        window = self._pywinauto_window()
        if window is None:
            return {}
        controls = []
        try:
            found = window.descendants()
        except Exception as exc:
            self._last_error = str(exc)
            found = []
        for child in found:
            if len(controls) >= max(1, int(limit or 1)):
                break
            try:
                name = str(child.window_text() or "").strip()
            except Exception:
                name = ""
            if not name:
                continue
            try:
                role = str(child.element_info.control_type or "")
            except Exception:
                role = ""
            if not self.keep(role, name, self._password_flag(child)):
                continue
            controls.append({"role": role, "name": name[:60]})
        try:
            page = str(window.window_text() or "")
        except Exception:
            page = ""
        return {"page": page[:120], "controls": controls}

    def _pywinauto_window(self):
        """先按系统句柄拿前台窗口，拿不到再退回「找当前活动窗口」。"""
        desktop = self._engine(backend="uia")
        handle = self._foreground_handle()
        if handle:
            try:
                window = desktop.window(handle=handle)
                window.window_text()
                return window
            except Exception as exc:
                self._last_error = str(exc)
        try:
            window = desktop.window(active_only=True)
            window.window_text()
            return window
        except Exception as exc:
            self._last_error = str(exc)
            return None

    def describe(self):
        if not self._module:
            note = "没装 pywinauto，也没装 uiautomation，读不了界面语义"
        elif self._last_error:
            note = "用 %s 读界面语义；上一次读失败：%s" % (self._module, self._last_error[:80])
        else:
            note = "用 %s 读界面语义（只读可访问性树，不读输入内容，不读密码框）" % self._module
        return {"provider": self.name, "available": self.is_available(), "note": note}


class AwarenessService:
    """屏幕感知的门面：管分级、管开关、管排除名单，只产出结构化摘要。"""

    EVENT_KEY = "awareness"

    def __init__(
        self,
        preferences=None,
        privacy=None,
        bus=None,
        logger=None,
        activity=None,
        semantic=None,
        vision=None,
        clock=time.time,
        cache_seconds=3.0,
    ):
        self.preferences = preferences
        self.privacy = privacy
        self.bus = bus
        self.logger = logger
        self.activity = activity or NullActivityProvider()
        self.semantic = semantic or NullSemanticProvider()
        self.vision = vision
        self.clock = clock
        self.cache_seconds = float(cache_seconds or 0)
        self._last = {}
        self._last_key = None
        self._last_at = 0.0

    # ---------- 分级与开关 ----------
    def level(self):
        stored = self._get(LEVEL_PREFERENCE)
        stored = str(stored or "").strip().lower()
        return stored if stored in LEVELS else "off"

    def set_level(self, level):
        wanted = str(level or "").strip().lower()
        if wanted not in LEVELS:
            wanted = "off"
        self._put(LEVEL_PREFERENCE, wanted)
        # 换了层级，之前那一条快照就不算数了。
        self.clear()
        return self.level()

    def paused(self):
        return str(self._get(PAUSE_PREFERENCE) or "0").strip() == "1"

    def set_paused(self, flag):
        self._put(PAUSE_PREFERENCE, "1" if flag else "0")
        return self.paused()

    def privacy_ok(self):
        """隐私中心那一层总开关。没接隐私中心就不额外拦。"""
        if self.privacy is None:
            return True
        try:
            return bool(self.privacy.allows("awareness"))
        except Exception:
            return False

    def active(self):
        """现在到底会不会去看。三个条件都满足才算开。"""
        return self.level() != "off" and not self.paused() and self.privacy_ok()

    def allows_metadata(self):
        return self.active() and self.level() in ("metadata", "semantic", "vision")

    def allows_semantic(self):
        return self.active() and self.level() in ("semantic", "vision")

    def allows_vision(self):
        return self.active() and self.level() == "vision"

    # ---------- 排除名单 ----------
    def exclusions(self):
        raw = str(self._get(EXCLUDE_PREFERENCE) or "")
        items = [part.strip().lower() for part in raw.split(",") if part.strip()]
        seen = []
        for item in items:
            if item not in seen:
                seen.append(item)
        return tuple(seen)

    def exclude(self, app):
        name = str(app or "").strip().lower()
        if not name:
            return self.exclusions()
        items = list(self.exclusions())
        if name not in items:
            items.append(name)
        self._put(EXCLUDE_PREFERENCE, ",".join(items))
        self.clear()
        return self.exclusions()

    def include(self, app):
        name = str(app or "").strip().lower()
        items = [item for item in self.exclusions() if item != name]
        self._put(EXCLUDE_PREFERENCE, ",".join(items))
        self.clear()
        return self.exclusions()

    def is_excluded(self, app):
        name = str(app or "").strip().lower()
        if not name:
            return False
        stem = name[:-4] if name.endswith(".exe") else name
        for item in self.exclusions():
            if item == name or item == stem:
                return True
        return False

    # ---------- 观察 ----------
    def observe(self, force=False):
        """看一眼现在在用什么软件。没开或读不到就给一个说得清的结果。"""
        if not self.active():
            return self._blocked("屏幕感知没开")
        if not self.activity.is_available():
            return self._blocked("这个平台读不到当前应用")
        now = self.clock()
        if not force and self._last and (now - self._last_at) < self.cache_seconds:
            return dict(self._last)
        raw = {}
        try:
            raw = self.activity.current() or {}
        except Exception as exc:
            self._warn("activity read failed: %s", exc)
            raw = {}
        app = str(raw.get("app") or "").strip()
        title = str(raw.get("title") or "").strip()
        if not app and not title:
            return self._blocked("现在读不到前台应用")
        if self.is_excluded(app):
            return self._blocked("这个应用在你排除了的名单里", app=app, title="")
        category, confidence = classify(app, title)
        sensitive = category in PRIVATE_CATEGORIES
        note = ""
        if sensitive:
            note = "这类页面可能带私人内容，只记应用名，不读里面的内容"
        elif confidence == "unknown":
            note = "这个应用不在已知名单里，只能确定它开着"
        payload = {
            "ok": True,
            "app": app,
            "title": title,
            "pid": int(raw.get("pid") or 0),
            "category": category,
            "category_label": category_label(category),
            "confidence": confidence,
            "confidence_label": CONFIDENCE_LABELS.get(confidence, "无法确定"),
            "sensitive": sensitive,
            "level": self.level(),
            "at": self._stamp(now),
            "semantic": None,
            "note": note,
        }
        if self.allows_semantic() and not sensitive:
            read = {}
            try:
                read = self.semantic.read() or {}
            except Exception as exc:
                self._warn("semantic read failed: %s", exc)
                read = {}
            if read:
                payload["semantic"] = read
            else:
                payload["note"] = (note + "；" if note else "") + self._semantic_note()
        self._last = payload
        self._last_at = now
        self._publish(payload)
        return dict(payload)

    def clear(self):
        """清掉这一层缓存下来的临时上下文。"""
        had = bool(self._last)
        self._last = {}
        self._last_key = None
        self._last_at = 0.0
        if had:
            self._emit("awareness.cleared")
        return had

    def status(self):
        """给界面用的一行状态：开没开、在哪一级、现在看到什么。"""
        if self.level() == "off":
            return "关着"
        if not self.privacy_ok():
            return "被隐私开关拦住了"
        if self.paused():
            return "暂停中"
        last = self._last
        if not last or not last.get("ok"):
            return "开着，还没读到"
        return "%s · %s" % (last.get("app") or "未知应用", last.get("category_label") or "")

    def describe(self):
        """设置页与诊断用的一张清单。只说真实能力，不吹。"""
        return {
            "level": self.level(),
            "level_label": LEVEL_LABELS.get(self.level(), "关闭"),
            "paused": self.paused(),
            "privacy_ok": self.privacy_ok(),
            "active": self.active(),
            "status": self.status(),
            "exclusions": self.exclusions(),
            "activity": self.activity.describe(),
            "semantic": self.semantic.describe(),
            "vision": bool(self.vision is not None),
            "private_categories": [category_label(item) for item in PRIVATE_CATEGORIES],
        }

    def _semantic_note(self):
        try:
            return str(self.semantic.describe().get("note") or "读不到界面语义")
        except Exception:
            return "读不到界面语义"

    def _blocked(self, note, app="", title=""):
        return {
            "ok": False,
            "app": app,
            "title": title,
            "pid": 0,
            "category": "",
            "category_label": "",
            "confidence": "unknown",
            "confidence_label": CONFIDENCE_LABELS["unknown"],
            "sensitive": False,
            "level": self.level(),
            "at": self._stamp(self.clock()),
            "semantic": None,
            "note": note,
        }

    def _stamp(self, now):
        try:
            return datetime.fromtimestamp(now).strftime("%H:%M:%S")
        except Exception:
            return ""

    def _publish(self, payload):
        key = (payload.get("app"), payload.get("title"))
        if key == self._last_key:
            return False
        self._last_key = key
        self._emit("awareness.changed", **{
            "app": payload.get("app", ""),
            "title": payload.get("title", ""),
            "category": payload.get("category", ""),
            "confidence": payload.get("confidence", ""),
        })
        return True

    def _emit(self, name, **payload):
        if self.bus is None:
            return None
        try:
            return self.bus.emit(name, **payload)
        except Exception as exc:
            self._warn("awareness event failed: %s", exc)
            return None

    def _get(self, key):
        if self.preferences is None:
            return None
        try:
            return self.preferences.get_preference(key)
        except Exception as exc:
            self._warn("awareness read failed for %s: %s", key, exc)
            return None

    def _put(self, key, value):
        if self.preferences is None:
            return False
        try:
            self.preferences.set_preference(key, value)
            return True
        except Exception as exc:
            self._warn("awareness write failed for %s: %s", key, exc)
            return False

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)


def build_service(
    preferences=None,
    privacy=None,
    bus=None,
    logger=None,
    activity=None,
    semantic=None,
    vision=None,
    clock=time.time,
):
    """按当前平台挑实现。挑不到就用「读不到」的实现，绝不假装看得见。"""
    if activity is None:
        activity = WindowsActivityProvider()
        if not activity.is_available():
            activity = NullActivityProvider()
    if semantic is None:
        semantic = UiAutomationProvider()
        if not semantic.is_available():
            semantic = NullSemanticProvider("没装 pywinauto 或 uiautomation，读不了界面语义")
    return AwarenessService(
        preferences=preferences,
        privacy=privacy,
        bus=bus,
        logger=logger,
        activity=activity,
        semantic=semantic,
        vision=vision,
        clock=clock,
    )
