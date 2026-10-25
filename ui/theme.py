"""界面视觉规范：颜色、字体、间距、控件外观与主题。

只描述样子，不含业务逻辑。换配色、换字号、切深浅色都只动这一个文件。
Tkinter 自己不会画圆角；工作台外层走 Windows 原生窗口裁切，卡片四角由
ui/glass_native 用背景像素遮罩。RADIUS 是这套遮罩的统一尺寸。

主题分三档：dark / light / system。system 去问 Windows 的浅色开关，
问不到就当深色，绝不因为读主题失败而影响启动。
"""

import sys
import tkinter as tk
from tkinter import ttk

from ui import tcl_safety  # noqa: F401  装上 Tk 跨线程收尾的保护，见模块注释

THEME_PREFERENCE = "ui_theme"
THEME_NAMES = ("dark", "light", "system")

# 减少动态效果：不摆动、不呼吸，眨眼保留（不然看着像卡住了）。
MOTION_PREFERENCE = "ui_motion"
MOTION_NAMES = ("full", "reduced")

# 强调色只换按钮、高亮和系统提示的配色，其余颜色仍跟深浅色走。
ACCENT_PREFERENCE = "ui_accent"
ACCENT_LABELS = {
    "brand": "樱粉",
    "cyan": "浅青",
    "sky": "天蓝",
    "mint": "薄荷",
    "violet": "紫罗兰",
    "rose": "玫瑰",
    "amber": "琥珀",
}
# 名字 -> (深色主题下的颜色, 浅色主题下的颜色)
ACCENTS = {
    "brand": ("#e8a0ba", "#c4608c"),
    "cyan": ("#7fe3d6", "#0d8f82"),
    "sky": ("#6cc7ff", "#2a86f0"),
    "mint": ("#6fe0c0", "#0f9d76"),
    "violet": ("#b79cff", "#6f4fe6"),
    "rose": ("#ff9cc0", "#e0518c"),
    "amber": ("#f5c46b", "#c98a1e"),
}
ACCENT_NAMES = tuple(ACCENTS)
# 品牌强调色按定稿规范，默认就是这一支粉。
DEFAULT_ACCENT = "brand"

# 液态玻璃：背景是渐变（或用户图片），面板是"磨砂玻璃"色块。
# 色阶保持中性，靠层次和发丝边区分；彩色只来自主题光晕和强调色。
# 夜：深靛蓝底；日：冷白底。两套都过了 4.5 对比度。
DARK = {
    "BG": "#0d0c16",
    "PANEL": "#191727",
    # 嵌在外壳里的分区、输入框底色：比面板再暗一档，形成凹槽感。
    "PANEL_2": "#131120",
    "FIELD": "#171525",
    "TEXT": "#f5f3fa",
    "MUTED": "#b0acc2",
    "PLACEHOLDER": "#8a869c",
    "ACCENT": "#e8a0ba",
    "LILAC": "#c9b6ff",
    "ON_ACCENT": "#12101c",
    "ERROR": "#e58b91",
    "OK": "#82c9a5",
    "WARN": "#e9b66d",
    "BORDER": "#332f47",
    "CODE_BG": "#100e1a",
    "BUBBLE_ME": "#2b2547",
    "BUBBLE_PET": "#1b1930",
    "BUBBLE_SYS": "#141220",
    "SIDEBAR_BG": "#100e1a",
    "SIDEBAR_ACTIVE": "#221d33",
    "SELECT_BG": "#3a3556",
    # 语义别名，和上面的键同值，方便按用途写代码。
    "CARD": "#191727",
    "INPUT_BG": "#171525",
    "SIDEBAR": "#100e1a",
    "TOPBAR_BG": "#100e1a",
    "SUCCESS": "#82c9a5",
    "DANGER": "#e58b91",
    # 四支点缀色：天蓝、浅青、粉红、白。
    "SKY": "#6cc7ff",
    "CYAN": "#7fe3d6",
    "PINK": "#f2a6c8",
    "WHITE": "#ffffff",
    # 主题光晕色，切预设时会被覆盖。
    "GLOW": "#7c5cff",
}

# 浅色不是把深色反过来，另起一套，正文、边框、输入框、禁用态都单独给值。
LIGHT = {
    "BG": "#f5f7fc",
    "PANEL": "#ffffff",
    "PANEL_2": "#eef1f8",
    "FIELD": "#ffffff",
    "TEXT": "#14141d",
    "MUTED": "#5d6070",
    # 浅色底上的占位提示不能太白：4.5 是底线。
    "PLACEHOLDER": "#6c7080",
    "ACCENT": "#c4608c",
    "LILAC": "#7b5cf0",
    # 浅色主题的默认强调色偏亮，按钮字用深色才够 4.5；换强调色时会按对比度重算。
    "ON_ACCENT": "#12101c",
    "ERROR": "#c24c55",
    "OK": "#277553",
    "WARN": "#87621d",
    "BORDER": "#d9dde9",
    "CODE_BG": "#f4f6fb",
    "BUBBLE_ME": "#dfe7ff",
    "BUBBLE_PET": "#eef1f8",
    "BUBBLE_SYS": "#f0f2f8",
    "SIDEBAR_BG": "#ffffff",
    "SIDEBAR_ACTIVE": "#eef1f8",
    "SELECT_BG": "#dbe4ff",
    "CARD": "#ffffff",
    "INPUT_BG": "#ffffff",
    "SIDEBAR": "#ffffff",
    "TOPBAR_BG": "#ffffff",
    "SUCCESS": "#277553",
    "DANGER": "#c24c55",
    "SKY": "#1f7ae0",
    "CYAN": "#0d8f82",
    "PINK": "#b8477a",
    # 同一支「白」在浅色底上等于隐形，浅色主题里换成中灰色，观感一致还能看清。
    "WHITE": "#6b7080",
    "GLOW": "#8b74ff",
}

# 渐变主题预设：只决定背景渐变与强调色；文字和面板仍走上面的中性玻璃色阶，
# 这样换主题不会出现"好看但读不清"的对比度问题。
# 每项：(渐变起点, 渐变终点, 光晕色)。
PRESETS = {
    "aurora": {
        "label": "极光 · Aurora",
        "accent": "violet",
        "night": ("#12102a", "#241b3d", "#7c5cff"),
        "day": ("#eef2ff", "#e4ddff", "#b9a6ff"),
    },
    "ocean": {
        "label": "晴空流光 · Ocean",
        "accent": "sky",
        "night": ("#081820", "#0d2b3a", "#2fd0ff"),
        "day": ("#eaf7ff", "#d9edff", "#6cc7ff"),
    },
    "sakura": {
        "label": "花漾梦境 · Sakura",
        "accent": "rose",
        "night": ("#1a0f1a", "#2b1626", "#ff9cc8"),
        "day": ("#fdf3ff", "#efe4ff", "#ffb3d1"),
    },
    "emerald": {
        "label": "森林薄雾 · Emerald",
        "accent": "mint",
        "night": ("#06150f", "#0d2a1e", "#4fe0a8"),
        "day": ("#ecfdf5", "#d9f5e8", "#6fe0c0"),
    },
    "sunset": {
        "label": "琥珀晨光 · Sunset",
        "accent": "amber",
        "night": ("#1a0d12", "#2b1410", "#ff8a5c"),
        "day": ("#fff5ec", "#ffe6d5", "#ffb27a"),
    },
    "lavender": {
        "label": "薰衣草 · Lavender",
        "accent": "violet",
        "night": ("#120f1e", "#211a33", "#c9b6ff"),
        "day": ("#f6f3ff", "#e9e2ff", "#c9b6ff"),
    },
    "custom": {
        "label": "自定义",
        "accent": "brand",
        "night": ("#101018", "#201a2c", "#e8a0ba"),
        "day": ("#f7f5fb", "#eee6f4", "#e8a0ba"),
    },
}
PRESET_NAMES = tuple(PRESETS)
DEFAULT_PRESET = "sakura"
# 用户选的渐变主题持久化在这里；日夜模式沿用 ui_theme 那一套。
PRESET_PREFERENCE = "ui_preset"
# 背景：图片路径、显示方式（cover/contain）、缩放、动态强度。
BACKGROUND_PREFERENCE = "ui_background_path"
BACKGROUND_MODE_PREFERENCE = "ui_background_mode"
BACKGROUND_ZOOM_PREFERENCE = "ui_background_zoom"
BACKGROUND_X_PREFERENCE = "ui_background_x"
BACKGROUND_Y_PREFERENCE = "ui_background_y"
BACKGROUND_BLUR_PREFERENCE = "ui_background_blur"
BACKGROUND_DIM_PREFERENCE = "ui_background_dim"
# 背景动效单独一套，别和角色的"减少动态效果"混用。
BG_MOTION_PREFERENCE = "ui_bg_motion"
BG_MOTION_NAMES = ("off", "low", "standard")
BG_MOTION_LABELS = {"off": "关", "low": "低", "standard": "标准"}

PALETTES = {"dark": DARK, "light": LIGHT}

# 当前生效的颜色。切主题时这些名字会被就地改写。
BG = DARK["BG"]
PANEL = DARK["PANEL"]
FIELD = DARK["FIELD"]
TEXT = DARK["TEXT"]
MUTED = DARK["MUTED"]
ACCENT = DARK["ACCENT"]
ON_ACCENT = DARK["ON_ACCENT"]
ERROR = DARK["ERROR"]
OK = DARK["OK"]
WARN = DARK["WARN"]
BORDER = DARK["BORDER"]
CODE_BG = DARK["CODE_BG"]
BUBBLE_ME = DARK["BUBBLE_ME"]
BUBBLE_PET = DARK["BUBBLE_PET"]
BUBBLE_SYS = DARK["BUBBLE_SYS"]
SIDEBAR_BG = DARK["SIDEBAR_BG"]
SIDEBAR_ACTIVE = DARK["SIDEBAR_ACTIVE"]
SELECT_BG = DARK["SELECT_BG"]
PLACEHOLDER = DARK["PLACEHOLDER"]
CARD = DARK["CARD"]
PANEL_2 = DARK["PANEL_2"]
LILAC = DARK["LILAC"]
INPUT_BG = DARK["INPUT_BG"]
SIDEBAR = DARK["SIDEBAR"]
TOPBAR_BG = DARK["TOPBAR_BG"]
SUCCESS = DARK["SUCCESS"]
DANGER = DARK["DANGER"]
SKY = DARK["SKY"]
CYAN = DARK["CYAN"]
PINK = DARK["PINK"]
WHITE = DARK["WHITE"]
GLOW = DARK["GLOW"]

_ACTIVE = "light"
_ACCENT = DEFAULT_ACCENT
_PRESET = DEFAULT_PRESET

# 消息标签 -> (前景色, 背景色, 对齐方式)
MESSAGE_STYLES = {}

# 界面状态 -> 提示色
STATE_COLORS = {}

_BUTTON_KINDS = {}

# ---------- 字体 ----------
# ---------- 显示缩放 ----------
# 高分屏上 Windows 会按显示比例放大窗口。进程要是不声明 DPI 感知，系统就把
# 整块窗口当位图拉伸，字自然糊。这里先声明感知，再自己按比例放间距、边框和
# 尺寸；字号走点值，Tk 会照系统比例换算，不用再乘一遍。
SCALE_PREFERENCE = "ui_scale"
FONT_SCALE_PREFERENCE = "ui_font_scale"
SCALE_STEPS = (0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0)
SCALE_MIN = 0.8
SCALE_MAX = 2.0
FONT_SCALE_STEPS = ("small", "normal", "large", "huge")
FONT_SCALE_LABELS = {"small": "小", "normal": "标准", "large": "大", "huge": "特大"}
_FONT_FACTORS = {"small": 0.9, "normal": 1.0, "large": 1.15, "huge": 1.32}
DEFAULT_FONT_SCALE = "normal"

FAMILY = "Microsoft YaHei"
MONO = "Consolas"
FONT_TITLE = (FAMILY, 12, "bold")
# 空会话正中的一句问话，要比正文大一号才立得住。
FONT_HERO = (FAMILY, 18, "bold")
# 按任务书的目标：正文约 15px、辅助 13px、标题 16px、页面大标题 24px。
# 96 DPI 下 1pt ≈ 1.33px，所以正文 11pt、辅助 10pt。
FONT_BODY = (FAMILY, 11)
FONT_SMALL = (FAMILY, 10)
FONT_INPUT = (FAMILY, 11)
FONT_BUTTON = (FAMILY, 10, "bold")
FONT_MONO = (MONO, 9)
FONT_CODE = (MONO, 10)
# 统一线性图标：同一个符号字体，不用 emoji 凑。
FONT_ICON = ("Segoe UI Symbol", 10)
ICON_FONT = FONT_ICON

_FONTS = {"title": FONT_TITLE, "hero": FONT_HERO, "body": FONT_BODY, "small": FONT_SMALL, "mono": FONT_MONO}

# ---------- 间距与圆角 ----------
# 定稿规范：间距只用 4/8/12/16/24/32，圆角只用 8/12/16。
PAD_XS = 4
PAD_SM = 8
PAD_MD = 16
PAD_LG = 24
PAD_XL = 32
BUBBLE_PAD = 8
RADIUS_SM = 8
RADIUS = 12
RADIUS_LG = 16

# ---------- 布局尺寸 ----------
TOPBAR_HEIGHT = 60      # 顶栏
NAV_WIDTH = 248         # 左侧导航展开宽
NAV_COLLAPSED_WIDTH = 68  # 左侧导航收起宽
CONTENT_MAX_WIDTH = 900   # 中央消息与输入区最大宽
PANEL_WIDTH = 360         # 右侧任务面板默认宽
PANEL_WIDTH_MIN = 340
PANEL_WIDTH_MAX = 400
ANIMATION_MS = 160       # 过渡动画时长，120-200ms 之间
CARD_SCREEN_WIDTH_RATIO = 0.82
CARD_SCREEN_HEIGHT_RATIO = 0.88

# 基础值只记一次，改缩放时从基础值重算，避免来回相乘越放越大。
_BASE_FONTS = {
    "FONT_TITLE": FONT_TITLE,
    "FONT_HERO": FONT_HERO,
    "FONT_BODY": FONT_BODY,
    "FONT_SMALL": FONT_SMALL,
    "FONT_INPUT": FONT_INPUT,
    "FONT_BUTTON": FONT_BUTTON,
    "FONT_MONO": FONT_MONO,
    "FONT_CODE": FONT_CODE,
    "FONT_ICON": FONT_ICON,
}
_BASE_PIXELS = {
    "PAD_XS": PAD_XS,
    "PAD_SM": PAD_SM,
    "PAD_MD": PAD_MD,
    "PAD_LG": PAD_LG,
    "PAD_XL": PAD_XL,
    "BUBBLE_PAD": BUBBLE_PAD,
    "RADIUS_SM": RADIUS_SM,
    "RADIUS": RADIUS,
    "RADIUS_LG": RADIUS_LG,
    "TOPBAR_HEIGHT": TOPBAR_HEIGHT,
    "NAV_WIDTH": NAV_WIDTH,
    "NAV_COLLAPSED_WIDTH": NAV_COLLAPSED_WIDTH,
    "CONTENT_MAX_WIDTH": CONTENT_MAX_WIDTH,
    "PANEL_WIDTH": PANEL_WIDTH,
    "PANEL_WIDTH_MIN": PANEL_WIDTH_MIN,
    "PANEL_WIDTH_MAX": PANEL_WIDTH_MAX,
}
# 基础按钮内边距也跟着缩放，不然高 DPI 下按钮显得又瘦又挤。
# 高度按 shadcn 的 default 按钮靠：一行字加 10px 上下留白，约 30px。
_BASE_BUTTON_PAD = (12, 5)

_DPI = 1.0
_USER_SCALE = 1.0
_FONT_SCALE = DEFAULT_FONT_SCALE
# 只有我们自己声明过 DPI 感知，才敢按系统比例放大界面；没人声明过就按 1 倍，
# 免得尺寸对不上（测试进程从来不声明）。
_AWARE = False


def _clamp(value, low, high):
    return low if value < low else (high if value > high else value)


def _mix(color, other, amount):
    """按比例混两种颜色，用来算悬浮态：只挪一小步，不换色系。"""
    first = str(color or "").lstrip("#")
    second = str(other or "").lstrip("#")
    if len(first) != 6 or len(second) != 6:
        return color
    parts = []
    for index in (0, 2, 4):
        left = int(first[index:index + 2], 16)
        right = int(second[index:index + 2], 16)
        parts.append(int(round(left * (1 - amount) + right * amount)))
    return "#%02x%02x%02x" % tuple(parts)


def _luminance(color):
    text = str(color or "").strip().lstrip("#")
    if len(text) != 6:
        return 0.0
    try:
        parts = [int(text[index:index + 2], 16) / 255.0 for index in (0, 2, 4)]
    except ValueError:
        return 0.0

    def channel(value):
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = (channel(item) for item in parts)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(first, second):
    high, low = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _readable_on(background):
    """按钮文字用黑还是白：按实际对比度挑，绝不硬编码。"""
    dark, light = "#12101c", "#ffffff"
    return light if contrast_ratio(background, light) >= contrast_ratio(background, dark) else dark


def enable_dpi_awareness():
    """告诉 Windows 我们自己会处理放大。必须在建任何窗口之前调用。"""
    global _AWARE
    if not sys.platform.startswith("win"):
        return "none"
    import ctypes

    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            _AWARE = True
            return "per-monitor-v2"
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        _AWARE = True
        return "system-aware"
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        _AWARE = True
        return "system"
    except Exception:
        return "none"


def awareness_enabled():
    return bool(_AWARE)


def system_dpi_scale():
    """系统显示比例。没声明过感知、或读不到，都按 1.0。"""
    if not _AWARE or not sys.platform.startswith("win"):
        return 1.0
    import ctypes

    try:
        dpi = int(ctypes.windll.user32.GetDpiForSystem())
        if dpi >= 96:
            return dpi / 96.0
    except Exception:
        pass
    return 1.0


def dpi_scale():
    return _DPI


def user_scale():
    return _USER_SCALE


def font_scale():
    return _FONT_SCALE


def current_scale():
    """界面实际的放大倍数：系统比例乘用户倍率。"""
    return round(_DPI * _USER_SCALE, 3)


def _scale_font(font, factor):
    size = max(6, int(round(font[1] * factor)))
    return (font[0], size) + tuple(font[2:])


def apply_scale(ui_scale=None, font_scale_name=None, dpi=None):
    """按显示比例重算间距、尺寸和字号。颜色和业务状态一律不动。"""
    global _DPI, _USER_SCALE, _FONT_SCALE, ICON_FONT
    if dpi is not None:
        _DPI = _clamp(float(dpi), 1.0, 3.0)
    if ui_scale is not None:
        _USER_SCALE = _clamp(float(ui_scale), SCALE_MIN, SCALE_MAX)
    if font_scale_name is not None:
        wanted = str(font_scale_name or "").strip().lower()
        _FONT_SCALE = wanted if wanted in _FONT_FACTORS else DEFAULT_FONT_SCALE
    factor = _DPI * _USER_SCALE
    for name, value in _BASE_PIXELS.items():
        globals()[name] = max(1, int(round(value * factor)))
    point = _FONT_FACTORS[_FONT_SCALE]
    for name, value in _BASE_FONTS.items():
        globals()[name] = _scale_font(value, point)
    ICON_FONT = FONT_ICON
    _FONTS.clear()
    _FONTS.update(
        {"title": FONT_TITLE, "hero": FONT_HERO, "body": FONT_BODY,
         "small": FONT_SMALL, "mono": FONT_MONO}
    )
    return factor


def reset_scale():
    """恢复默认：跟着系统比例走，不再额外放大，字号回到标准。"""
    return apply_scale(
        ui_scale=1.0, font_scale_name=DEFAULT_FONT_SCALE, dpi=system_dpi_scale()
    )


def scale_saved(preferences):
    """从偏好里读缩放设置并应用。读不到就用默认值。"""
    ui_scale = 1.0
    font_name = DEFAULT_FONT_SCALE
    if preferences is not None:
        try:
            stored = preferences.get_preference(SCALE_PREFERENCE)
            stored_font = preferences.get_preference(FONT_SCALE_PREFERENCE)
        except Exception:
            stored = stored_font = None
        if stored:
            try:
                ui_scale = float(stored)
            except Exception:
                ui_scale = 1.0
        if stored_font:
            font_name = str(stored_font)
    return apply_scale(
        ui_scale=ui_scale, font_scale_name=font_name, dpi=system_dpi_scale()
    )


def step_scale(current, direction):
    """加减一档，返回落在档位上的新倍率。"""
    try:
        value = float(current)
    except Exception:
        value = 1.0
    steps = list(SCALE_STEPS)
    if direction > 0:
        for item in steps:
            if item > value + 1e-6:
                return item
        return steps[-1]
    for item in reversed(steps):
        if item < value - 1e-6:
            return item
    return steps[0]


def button_padding():
    """按钮内边距按当前比例算，别让高 DPI 下的按钮显得干瘪。"""
    factor = current_scale()
    return (
        max(4, int(round(_BASE_BUTTON_PAD[0] * factor))),
        max(2, int(round(_BASE_BUTTON_PAD[1] * factor))),
    )


_MESSAGE_TAGS = {
    "me": ("TEXT", "BUBBLE_ME", "right"),
    "pet": ("TEXT", "BUBBLE_PET", "left"),
    "err": ("ERROR", "BUBBLE_SYS", "left"),
    "hint": ("MUTED", "BUBBLE_SYS", "left"),
    "notice": ("ACCENT", "BUBBLE_SYS", "left"),
    "proactive": ("OK", "BUBBLE_SYS", "left"),
    "task": ("WARN", "BUBBLE_SYS", "left"),
}

_STATE_TAGS = {"idle": "MUTED", "busy": "ACCENT", "ok": "OK", "warn": "WARN", "error": "ERROR"}


def icon_font(bump=0):
    """图标字体。bump 是相对基础图标字号的加档，按钮上要显眼就用它。"""
    base = FONT_ICON
    if not bump:
        return base
    return (base[0], max(6, int(base[1]) + int(bump)))


def _recompute():
    """把颜色变化同步到衍生表：样式表保持同一个对象，调用方不用改引用。"""
    MESSAGE_STYLES.clear()
    for tag, (fg, bg, align) in _MESSAGE_TAGS.items():
        MESSAGE_STYLES[tag] = (globals()[fg], globals()[bg], align)
    STATE_COLORS.clear()
    for kind, name in _STATE_TAGS.items():
        STATE_COLORS[kind] = globals()[name]
    _BUTTON_KINDS.clear()
    _BUTTON_KINDS.update(
        {
            # (底色, 字色, 悬浮底色, 悬浮字色, 边框色)
            "primary": (ACCENT, ON_ACCENT, _mix(ACCENT, "#000000", 0.10), ON_ACCENT, ""),
            "ghost": (PANEL, TEXT, SIDEBAR_ACTIVE, TEXT, BORDER),
            "danger": (PANEL, ERROR, SIDEBAR_ACTIVE, ERROR, BORDER),
            "flat": (BG, MUTED, SIDEBAR_ACTIVE, TEXT, ""),
            "chip": (PANEL_2, TEXT, SIDEBAR_ACTIVE, TEXT, BORDER),
            # 顶栏图标按钮：平时融进底色，移上去才浮出一块，不画边框。
            "toolbar": (TOPBAR_BG, MUTED, SIDEBAR_ACTIVE, TEXT, ""),
            "toolbar_danger": (
                TOPBAR_BG,
                ERROR,
                _mix(ERROR, BG, 0.86),
                ERROR,
                "",
            ),
        }
    )


def system_theme():
    """读 Windows 的浅色开关。读不到就按深色，不抛错。"""
    if not sys.platform.startswith("win"):
        return "light"
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        try:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        finally:
            winreg.CloseKey(key)
        return "light" if int(value) else "dark"
    except Exception:
        return "dark"


def resolve(name):
    """把主题名解析成真的 palette 名。"""
    wanted = str(name or "").strip().lower()
    if wanted == "system":
        return system_theme()
    if wanted in PALETTES:
        return wanted
    # 认不出的主题按默认的浅色走，不再掉回纯黑。
    return "light"


def active():
    return _ACTIVE


def accent():
    return _ACCENT


def accent_label(name=None):
    return ACCENT_LABELS.get(accent() if name is None else str(name), "")


def set_accent(name):
    """挑一个强调色并立刻生效。认不出的名字就地退回默认，不抛异常。"""
    global _ACCENT
    wanted = str(name or "").strip().lower()
    _ACCENT = wanted if wanted in ACCENTS else DEFAULT_ACCENT
    _paint_accent()
    return _ACCENT


def preset():
    """当前渐变预设的名字。"""
    return _PRESET


def preset_label(name=None):
    return PRESETS.get(preset() if name is None else str(name), {}).get("label", "")


def presets():
    return {key: dict(value) for key, value in PRESETS.items()}


def gradient():
    """当前预设在当前模式下的渐变：(起点, 终点, 光晕)。"""
    item = PRESETS.get(_PRESET, PRESETS[DEFAULT_PRESET])
    return item["night" if _ACTIVE == "dark" else "day"]


def set_preset(name, mode=None):
    """换渐变预设：背景和强调色一起换，文字色阶不动。"""
    global _PRESET
    wanted = str(name or "").strip().lower()
    _PRESET = wanted if wanted in PRESETS else DEFAULT_PRESET
    accent = PRESETS[_PRESET]["accent"]
    return apply(mode or _ACTIVE, accent_name=accent)


def _paint_accent():
    """强调色分深浅两版，当前是哪档就刷哪版。"""
    dark_accent, light_accent = ACCENTS[_ACCENT]
    accent = dark_accent if _ACTIVE == "dark" else light_accent
    globals()["ACCENT"] = accent
    # 字色跟着强调色走：浅底白字容易不达标，这里按对比度选黑或白。
    globals()["ON_ACCENT"] = _readable_on(accent)
    _recompute()
    _sync_aliases()


def apply(name, accent_name=None):
    """切换主题。返回实际生效的名字。只改颜色，不动任何业务状态。

    accent_name 给了就连强调色一起换；不给就沿用手上这档。
    """
    global _ACTIVE
    resolved = resolve(name)
    palette = PALETTES[resolved]
    for key, value in palette.items():
        globals()[key] = value
    _ACTIVE = resolved
    # 光晕色跟着预设和日夜走；它只用于阴影/背景，不参与文字对比度。
    globals()["GLOW"] = PRESETS.get(_PRESET, PRESETS[DEFAULT_PRESET])[
        "night" if resolved == "dark" else "day"
    ][2]
    global _ACCENT
    if accent_name is not None:
        wanted = str(accent_name or "").strip().lower()
        _ACCENT = wanted if wanted in ACCENTS else DEFAULT_ACCENT
    _paint_accent()
    return resolved


def _sync_aliases():
    """已经把颜色拷到自己模块里的老代码，切主题时跟着更新。"""
    module = sys.modules.get("ui.desktop")
    if module is None:
        return
    for name in (
        "BG", "PANEL", "FIELD", "TEXT", "MUTED", "ACCENT", "ERROR", "OK", "WARN", "BORDER",
        "PLACEHOLDER", "GLOW",
    ):
        setattr(module, name, globals()[name])


def apply_saved(preferences):
    """从偏好里恢复主题与强调色；没有记录就用深色加默认色。"""
    global _PRESET
    # 参考图是浅色玻璃工作台；没有偏好时默认白天/浅色。
    name = "light"
    accent_name = DEFAULT_ACCENT
    _PRESET = DEFAULT_PRESET
    if preferences is not None:
        try:
            stored = preferences.get_preference(THEME_PREFERENCE)
            stored_accent = preferences.get_preference(ACCENT_PREFERENCE)
            stored_preset = preferences.get_preference(PRESET_PREFERENCE)
        except Exception:
            stored = stored_accent = stored_preset = None
        if stored:
            name = str(stored)
        if stored_preset and str(stored_preset).lower() in PRESETS:
            _PRESET = str(stored_preset).lower()
        if stored_accent:
            accent_name = str(stored_accent)
        else:
            accent_name = PRESETS[_PRESET]["accent"]
    return apply(name, accent_name)


_recompute()


def label(master, text="", kind="body", **kw):
    """统一字号与底色的文字标签。"""
    options = {"bg": BG, "fg": TEXT, "font": _FONTS.get(kind, FONT_BODY), "bd": 0}
    options.update(kw)
    return tk.Label(master, text=text, **options)


def button(master, text, command, kind="ghost", **kw):
    """统一样式的按钮。kind: primary / ghost / danger / flat / chip。

    边框用 highlight 环画：Tk 的 bd 走系统 3D 色，跟不了主题。
    鼠标移上去底色挪一档，按下去用同一档，手感一致。
    """
    bg, fg, hover_bg, hover_fg, border = _BUTTON_KINDS.get(
        kind, _BUTTON_KINDS["ghost"]
    )
    custom_bg = "bg" in kw
    options = {
        "bg": bg,
        "fg": fg,
        "bd": 0,
        "highlightthickness": 1 if border else 0,
        "highlightbackground": border or bg,
        "highlightcolor": ACCENT,
        "activebackground": hover_bg,
        "activeforeground": hover_fg,
        "disabledforeground": MUTED,
        "font": FONT_BUTTON,
        "padx": button_padding()[0],
        "pady": button_padding()[1],
        "relief": "flat",
        "cursor": "hand2",
    }
    options.update(kw)
    widget = tk.Button(master, text=text, command=command, **options)
    widget._theme_kind = kind
    if not custom_bg:
        def _enter(_event=None):
            try:
                if str(widget.cget("state")) != "disabled":
                    widget.configure(bg=hover_bg)
            except Exception:
                pass

        def _leave(_event=None):
            try:
                widget.configure(bg=bg)
            except Exception:
                pass

        widget.bind("<Enter>", _enter, add="+")
        widget.bind("<Leave>", _leave, add="+")
    return widget


def entry(master, **kw):
    """统一样式的输入框。"""
    options = {
        "bg": FIELD,
        "fg": TEXT,
        "insertbackground": TEXT,
        "selectbackground": SELECT_BG,
        "bd": 0,
        # 1px 边框 + 聚焦时换成强调色：Tk 的 highlight 环正好当焦点框。
        "highlightthickness": 1,
        "highlightbackground": BORDER,
        "highlightcolor": ACCENT,
        "font": FONT_INPUT,
    }
    options.update(kw)
    return tk.Entry(master, **options)


def text_area(master, read_only=True, **kw):
    """统一样式的多行文本区。默认只读，写入前由调用方临时打开。"""
    options = {
        "bg": FIELD,
        "fg": TEXT,
        "bd": 0,
        "highlightthickness": 1,
        "highlightbackground": BORDER,
        "highlightcolor": ACCENT,
        "wrap": "word",
        "font": FONT_BODY,
        "padx": BUBBLE_PAD,
        "pady": BUBBLE_PAD,
        "selectbackground": SELECT_BG,
    }
    options.update(kw)
    widget = tk.Text(master, **options)
    widget.config(state="disabled" if read_only else "normal")
    return widget


def style_widgets(master=None):
    """把 ttk 控件（表格、下拉、滚动条）也刷成同一套配色。

    ttk 默认跟系统主题走，深色界面上会冒出白底表格和灰色滚动条。
    这里统一成项目色板，只作用于本进程，不改系统设置。
    """
    style = ttk.Style(master)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    factor = current_scale()
    style.configure("TFrame", background=BG)
    style.configure(
        "Treeview",
        background=FIELD,
        fieldbackground=FIELD,
        foreground=TEXT,
        borderwidth=0,
        rowheight=max(20, int(round(30 * factor))),
        font=FONT_BODY,
    )
    style.map(
        "Treeview",
        background=[("selected", SIDEBAR_ACTIVE)],
        foreground=[("selected", TEXT)],
    )
    style.configure(
        "Treeview.Heading",
        background=PANEL_2,
        foreground=MUTED,
        borderwidth=0,
        relief="flat",
        font=FONT_SMALL,
        padding=(PAD_SM, max(4, PAD_XS)),
    )
    style.map("Treeview.Heading", background=[("active", SIDEBAR_ACTIVE)])
    style.configure(
        "TCombobox",
        fieldbackground=FIELD,
        background=PANEL,
        foreground=TEXT,
        arrowcolor=MUTED,
        bordercolor=BORDER,
        lightcolor=BORDER,
        darkcolor=BORDER,
        borderwidth=1,
        padding=(PAD_SM, max(3, int(round(4 * factor)))),
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", FIELD)],
        foreground=[("readonly", TEXT)],
        bordercolor=[("focus", ACCENT)],
        arrowcolor=[("active", TEXT)],
    )
    style.configure(
        "Vertical.TScrollbar",
        background=PANEL,
        troughcolor=BG,
        bordercolor=BG,
        darkcolor=BG,
        lightcolor=BG,
        arrowcolor=MUTED,
        arrowsize=0,
        gripcount=0,
        width=max(8, int(round(10 * factor))),
    )
    style.map("Vertical.TScrollbar", background=[("active", SIDEBAR_ACTIVE)])
    style.configure("TSeparator", background=BORDER)
    return style


def tabs(master):
    """统一样式的分页控件：功能页里分组用它，别各写一套配色。"""
    style = style_widgets(master)
    style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0))
    style.configure(
        "TNotebook.Tab",
        background=PANEL_2,
        foreground=MUTED,
        padding=(PAD_SM + 4, PAD_XS + 3),
        font=FONT_SMALL,
        borderwidth=0,
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", CARD), ("active", SIDEBAR_ACTIVE)],
        foreground=[("selected", TEXT), ("active", TEXT)],
    )
    return ttk.Notebook(master)


def card(master, **kw):
    """一块卡片：底板、细边框，并交给原生层做四角遮罩。"""
    radius = int(kw.pop("radius", RADIUS))
    options = {
        "bg": CARD,
        "highlightthickness": 1,
        # 上边缘再亮一点：玻璃的"高光边"，不是粗描边。
        "highlightbackground": _mix(BORDER, TEXT, 0.18),
        "bd": 0,
    }
    options.update(kw)
    widget = tk.Frame(master, **options)
    try:
        from ui import glass_native

        glass_native.round_widget(widget, radius)
    except Exception:
        pass
    return widget


def cardify(master, old_bg=None, new_bg=None):
    """Recolour a built page so stray main-background blocks sit on the card.

    This only moves surface colours; the actual rounded clipping is done by
    ui.glass_native, and text/field colours stay untouched.
    """
    old_bg = str(old_bg or BG).lower()
    new_bg = str(new_bg or CARD).lower()
    stack = [master]
    while stack:
        widget = stack.pop()
        for option in ("bg", "background", "activebackground", "highlightbackground"):
            try:
                if str(widget.cget(option)).lower() == old_bg:
                    widget.configure(**{option: new_bg})
            except Exception:
                continue
        try:
            stack.extend(widget.winfo_children())
        except Exception:
            continue
    return master


def section(master, title, note=""):
    """分区标题：小小的字，不抢正文。返回 (容器, 内容区)。"""
    box = card(master)
    theme_title = tk.Label(
        box, text=str(title or ""), bg=CARD, fg=TEXT, font=FONT_TITLE, anchor="w",
        bd=0, highlightthickness=0,
    )
    theme_title.pack(anchor="w", padx=PAD_MD, pady=(PAD_SM, 0))
    if note:
        tk.Label(
            box, text=str(note), bg=CARD, fg=MUTED, font=FONT_SMALL,
            anchor="w", justify="left", bd=0, highlightthickness=0,
        ).pack(anchor="w", padx=PAD_MD, pady=(PAD_XS, 0))
    body = tk.Frame(box, bg=CARD)
    body.pack(fill="x", padx=PAD_MD, pady=(PAD_SM, PAD_MD))
    return box, body


def scroll_area(master, padding=None):
    """可滚动的内容区。缩放调大之后页面也不会被裁掉。返回内层 Frame。"""
    style_widgets(master)
    outer = tk.Frame(master, bg=BG)
    outer.pack(fill="both", expand=True)
    canvas = tk.Canvas(outer, bg=BG, highlightthickness=0, bd=0)
    # 用 ttk 的滚动条：tk.Scrollbar 在 Windows 上走系统画法，深色界面里是两条灰杠。
    bar = ttk.Scrollbar(
        outer, orient="vertical", command=canvas.yview, style="Vertical.TScrollbar"
    )
    canvas.configure(yscrollcommand=bar.set)
    bar.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)
    inner = tk.Frame(canvas, bg=BG)
    window = canvas.create_window((0, 0), window=inner, anchor="nw")

    def _region(event=None):
        try:
            canvas.configure(scrollregion=canvas.bbox("all"))
        except Exception:
            return None
        return None

    def _fit_width(event):
        try:
            canvas.itemconfigure(window, width=int(event.width))
        except Exception:
            return None
        return None

    def _wheel(event):
        step = -1 if getattr(event, "delta", 0) > 0 else 1
        try:
            canvas.yview_scroll(step * 3, "units")
        except Exception:
            return None
        try:
            from ui import glass_native

            glass_native.refresh_rounding_later()
        except Exception:
            pass
        return None

    inner.bind("<Configure>", _region)
    canvas.bind("<Configure>", _fit_width)
    canvas.bind("<MouseWheel>", _wheel)
    inner.bind("<MouseWheel>", _wheel)
    inner.canvas = canvas
    inner.outer = outer
    if padding:
        inner.configure(padx=padding[0], pady=padding[1])
    return inner


def bind_wheel(widget, canvas):
    """把滚轮接到外层的滚动区上：子控件自己不滚，往上滚。"""
    if canvas is None:
        return 0
    count = 0

    def _wheel(event):
        step = -1 if getattr(event, "delta", 0) > 0 else 1
        try:
            canvas.yview_scroll(step * 3, "units")
        except Exception:
            return None
        try:
            from ui import glass_native

            glass_native.refresh_rounding_later()
        except Exception:
            pass
        return "break"

    stack = [widget]
    while stack:
        current = stack.pop()
        try:
            current.bind("<MouseWheel>", _wheel, add="+")
            count += 1
        except Exception:
            pass
        try:
            stack.extend(current.winfo_children())
        except Exception:
            continue
    return count


def tooltip(widget, text, delay=450):
    """鼠标停在控件上冒一句说明。只影响显示，不改点击行为。"""
    text = str(text or "")
    if not text:
        return None
    state = {"window": None, "id": None}

    def hide(event=None):
        pending = state["id"]
        state["id"] = None
        if pending is not None:
            try:
                widget.after_cancel(pending)
            except Exception:
                pass
        window = state["window"]
        state["window"] = None
        if window is not None:
            try:
                window.destroy()
            except Exception:
                pass

    def show():
        state["id"] = None
        if state["window"] is not None:
            return
        try:
            box = tk.Toplevel(widget)
            box.wm_overrideredirect(True)
            box.attributes("-topmost", True)
            box.configure(bg=BORDER)
            tk.Label(
                box, text=text, bg=PANEL, fg=TEXT, font=FONT_SMALL,
                padx=PAD_SM, pady=PAD_XS, justify="left",
            ).pack(padx=1, pady=1)
            x = widget.winfo_rootx() + 8
            y = widget.winfo_rooty() + widget.winfo_height() + 6
            box.wm_geometry("+%d+%d" % (x, y))
            state["window"] = box
        except Exception:
            state["window"] = None

    def enter(event=None):
        if state["id"] is None:
            try:
                state["id"] = widget.after(delay, show)
            except Exception:
                state["id"] = None

    widget.bind("<Enter>", enter, add="+")
    widget.bind("<Leave>", hide, add="+")
    widget.bind("<ButtonPress>", hide, add="+")
    widget.bind("<Destroy>", hide, add="+")
    return text


def style_messages(widget, margin=None):
    """把消息样式装进文本区：靠右/靠左的气泡、代码块与选中色。

    margin 是气泡两侧留白的底色，默认跟输入区一致；消息区空着的时候
    宿主会把它刷成主背景，免得首屏留一块空框。
    """
    margin = FIELD if margin is None else margin
    gutter = max(BUBBLE_PAD * 3, 24)
    for tag, (fg, bg, align) in MESSAGE_STYLES.items():
        widget.tag_config(
            tag,
            foreground=fg,
            background=bg,
            justify=align,
            # 左右各留一条走廊：气泡不贴边，连续消息也不会连成整屏色块。
            lmargin1=gutter,
            lmargin2=gutter,
            rmargin=gutter,
            # 不加这两行，气泡底色会连左右留白一起刷满整条。
            lmargincolor=margin,
            rmargincolor=margin,
            spacing1=6,
            spacing3=6,
        )
    # 消息之间的空隙：空白行，不刷气泡底色。
    widget.tag_config(
        "gap",
        font=(FAMILY, 4),
        background=margin,
        spacing1=0,
        spacing3=0,
    )
    widget.tag_config(
        "code",
        foreground=TEXT,
        background=CODE_BG,
        font=FONT_CODE,
        lmargin1=BUBBLE_PAD * 2,
        lmargin2=BUBBLE_PAD * 2,
        rmargin=BUBBLE_PAD,
        lmargincolor=margin,
        rmargincolor=margin,
        spacing1=6,
        spacing3=6,
    )
    widget.tag_config("code_lang", foreground=MUTED, font=FONT_SMALL)
    # 消息后面的小时间戳：不抢正文的注意力。
    widget.tag_config("stamp", foreground=MUTED, font=FONT_SMALL)
    widget.tag_config("search_hit", background=SELECT_BG)
    return widget
