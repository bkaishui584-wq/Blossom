"""桌宠桌面界面（tkinter）。

生产界面只显示用户自己的 Live2D 模型。没有角色画面时保持透明拖动载体，
不再画旧的 Canvas 占位脸。情绪仍由 Agent 经 EmotionState 推到 AvatarProvider。
"""

import ctypes
import base64
import io
import json
import logging
import queue
import threading
import time
import tkinter as tk
import traceback
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog

from agent.router import MODE_LABELS
from agent.task import TASK_STATE_LABELS, TaskState
from avatar.interface import EMOTION_EXPRESSIONS, AvatarProvider
from events import bus as bus_module
from security import startup as startup_control
from security.config import resource_path
from ui import glass, theme
from ui import tray as tray_module
from characters import store as character_store
from persona import cards as persona_cards
from persona import habits as persona_habits
from persona import history as persona_history
from persona import lab as persona_lab
from persona import learner as persona_learner
from persona import policy as persona_policy
from persona import profile as persona_profile
from persona import traits as traits_module
from persona.interface import build_persona_note
from model.base import LOCAL_ENDPOINT_HOSTS, endpoint_host
from voice import pack as voice_pack
from voice.capture import (
    NullCapture,
    build_capture,
    build_stream_capture,
    default_capture_path,
)
from voice.speaker import build_provider as build_speech
from voice.stt import build_stt, build_streaming_stt, streaming_model_status
from voice.subtitle import SubtitlePipeline, SubtitleTranslator
from voice.subtitle_history import SubtitleHistoryRepository
from ui import chat_view
from ui.avatar_glue import AvatarGlue
from ui.chat_wiring import ChatWiring
from ui.developer import DeveloperPanel
from ui.panels import PanelOpener
from ui.state_store import LEGACY_CARD_SIZES, StateStore  # noqa: F401  (外部按老路径引用)
from ui.awareness_view import AwarenessWindow
from ui.knowledge_view import KnowledgeWindow
from ui.goals_view import GoalsWindow
from ui.ideas_view import IdeasWindow
from ui.chat_view import ChatView
from ui.pet_host import (
    HIT_TAG,
    TRANSPARENT_KEY,
    PetHost,
    hide_from_taskbar,
    minimize_window,
    restore_window,
    show_in_taskbar,
)
from ui.memory_view import MemoryWindow
from ui.task_view import TaskWindow
from ui.persona_view import PersonaWindow
from ui.settings_view import SettingsWindow
from ui.skills_view import SkillsWindow
from ui.capability_view import CapabilityWindow
from ui.device_view import DeviceWindow
from model.logged import LoggedProvider

# 颜色仍从本模块导出，外部（含测试）照旧可以直接引用。
def human_bytes(size):
    """把字节数写成人看的大小。"""
    try:
        value = float(size or 0)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


BG = theme.BG
PANEL = theme.PANEL
FIELD = theme.FIELD
TEXT = theme.TEXT
MUTED = theme.MUTED
ACCENT = theme.ACCENT
ERROR = theme.ERROR

# 聊天浮层大小。桌宠本体不再是窗口，窗口只是角色的透明载体。
CARD_WIDTH = 400
CARD_HEIGHT = 520
# 首次打开的默认工作台尺寸：按定稿规范跟屏幕走，宽 82%、高 88%，
# 上限只防超宽屏摊成一大片。
CARD_SCREEN_WIDTH_RATIO = 0.82
CARD_SCREEN_HEIGHT_RATIO = 0.88
# 规范给的目标尺寸，屏幕够就用这个起步，更大再按比例放大。
CARD_TARGET_WIDTH = 1280
CARD_TARGET_HEIGHT = 820
MAX_CARD_WIDTH = 1600
MAX_CARD_HEIGHT = 1040
EMOTION_LABELS = {
    "neutral": "平静",
    "happy": "开心",
    "sad": "低落",
    "angry": "生气",
    "tired": "累了",
    "excited": "兴奋",
    "thinking": "思考中",
    "curious": "好奇",
    "annoyed": "有点烦",
    "calm": "放松",
    "sleepy": "困了",
}

SKIN_COLORS = {
    "normal": "#cfd8e3",
    "smile": "#ffe066",
    "sad": "#7fa7d9",
    "angry": "#ff8f8f",
    "sleepy": "#b8a9d9",
    "excited": "#ffb86b",
    "thinking": "#8fd9c1",
    "curious": "#a8dbe8",
    "annoyed": "#f0a06a",
}

# 每种表情的画法：眼睛风格 + 嘴型
FACE_SHAPES = {
    "normal": ("dot", "flat"),
    "smile": ("arc", "smile"),
    "sad": ("droop", "frown"),
    "angry": ("slant", "frown"),
    "sleepy": ("half", "flat"),
    "excited": ("wide", "smile"),
    "curious": ("wide", "flat"),
    "annoyed": ("slant", "wave"),
    "thinking": ("dot", "wave"),
}


def emotion_label(emotion):
    return EMOTION_LABELS.get(emotion, emotion or "未知")


def format_status(emotion, totals=None, reply=None, model="", mode="", task="", mic="", quiet=False):
    parts = [f"情绪：{emotion_label(emotion or 'neutral')}"]
    if quiet:
        parts.append("静默中")
    if mic:
        parts.append(f"麦克风：{mic}")
    if mode:
        parts.append(str(mode))
    parts.append(f"聊天模型：{model or '未配置'}")
    if task is not None:
        parts.append(f"任务能力：{task or '未配置'}")
    if reply is not None and reply.ok:
        parts.append(f"本轮 {reply.usage.total_tokens} tokens")
    if totals:
        parts.append(f"累计 {totals.get('total_tokens', 0)} tokens")
    return "  ·  ".join(parts)


def format_status_short(emotion, mode="", mic="", quiet=False, working=False):
    """状态行只留「现在是什么状态」。

    模型、任务和 token 这些容易越长越长的东西挪进详情弹窗，别常驻挤占标题区。
    """
    parts = [f"情绪：{emotion_label(emotion or 'neutral')}"]
    if quiet:
        parts.append("静默中")
    if working:
        parts.append("工作记录中")
    if mic:
        parts.append(f"麦克风：{mic}")
    if mode:
        parts.append(str(mode))
    return "  ·  ".join(parts)


def format_debug(debug):
    """仅用于界面展示。调试信息绝不发送给模型。"""
    if not debug:
        return ""
    keys = (
        "model",
        "mode",
        "intent",
        "capability",
        "local",
        "safe_tool",
        "companion_status",
        "task_status",
        "task_state",
        "error_type",
        "emotion",
        "estimated_tokens",
        "budget",
        "recent_sent",
        "recent_available",
        "memory_count",
        "has_summary",
        "tools_offered",
        "tool_calls",
        "tool_results",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
    )
    lines = [f"{key}: {debug[key]}" for key in keys if key in debug]
    extra = {key: value for key, value in debug.items() if key not in keys}
    if extra:
        lines.append("其他: " + json.dumps(extra, ensure_ascii=False, default=str))
    return "\n".join(lines)


class PetCanvas(tk.Canvas):
    """透明载体。测试可显式打开旧的 Canvas 表情，生产固定关闭。"""

    DEFAULT_SIZE = 148

    def __init__(self, master, size=None, bg=BG, draw_placeholder=True):
        size = int(size or PetCanvas.DEFAULT_SIZE)
        super().__init__(
            master, width=size, height=size, bg=bg, highlightthickness=0, bd=0
        )
        self.size = size
        self.draw_placeholder = bool(draw_placeholder)
        self.blank = not self.draw_placeholder
        self.expression = "normal"
        if not self.blank:
            self.render("normal")

    def set_blank(self, blank):
        """Live2D 接管画面时清空占位图：载体全透明，既不挡角色也不挡鼠标。"""
        self.blank = bool(blank) or not self.draw_placeholder
        if self.blank:
            self._clear_body()
        else:
            self.render(self.expression)
        return self.blank

    def _clear_body(self):
        """只清占位图。角色的碰撞体积是另一层，不能跟着一起被清掉。"""
        for item in self.find_all():
            if HIT_TAG not in self.gettags(item):
                self.delete(item)

    def render(self, expression):
        expression = expression if expression in FACE_SHAPES else "normal"
        self.expression = expression
        if not self.draw_placeholder:
            self._clear_body()
            return None
        if self.blank:
            # 角色已经由独立窗口在画，占位图不再动手。
            return None
        self._clear_body()

        center = self.size / 2
        radius = self.size * 0.32
        skin = SKIN_COLORS.get(expression, SKIN_COLORS["normal"])
        eye_style, mouth_style = FACE_SHAPES[expression]

        self.create_oval(
            center - radius,
            center - radius,
            center + radius,
            center + radius,
            fill=skin,
            outline="",
        )
        # 耳朵
        for sign in (-1, 1):
            self.create_oval(
                center + sign * radius * 0.62 - radius * 0.22,
                center - radius * 1.18,
                center + sign * radius * 0.62 + radius * 0.22,
                center - radius * 0.62,
                fill=skin,
                outline="",
            )

        dx = radius * 0.42
        ey = center - radius * 0.18
        for sign in (-1, 1):
            self._draw_eye(center + sign * dx, ey, radius * 0.16, eye_style, sign)
        self._draw_mouth(center, center + radius * 0.36, radius * 0.42, mouth_style)

        if expression == "sleepy":
            self.create_text(
                center + radius * 0.95, center - radius * 0.95, text="z", fill=MUTED, font=("Segoe UI", 11)
            )

    def _draw_eye(self, x, y, r, style, sign):
        if style == "dot":
            self.create_oval(x - r * 0.55, y - r * 0.75, x + r * 0.55, y + r * 0.75, fill="#26262e", outline="")
        elif style == "wide":
            self.create_oval(x - r * 0.8, y - r * 0.95, x + r * 0.8, y + r * 0.95, fill="#26262e", outline="")
        elif style == "arc":
            self.create_arc(
                x - r, y - r * 0.6, x + r, y + r * 1.1,
                start=20 if sign > 0 else 160, extent=140, style="arc", width=2, outline="#26262e",
            )
        elif style == "half":
            self.create_rectangle(x - r * 0.9, y - r * 0.1, x + r * 0.9, y + r * 0.5, fill="#26262e", outline="")
        elif style == "droop":
            self.create_line(x - r * 0.8, y - r * 0.5, x + r * 0.8, y + r * 0.2, width=2, fill="#26262e")
        elif style == "slant":
            self.create_line(x - r * 0.8, y + r * 0.5, x + r * 0.8, y - r * 0.6, width=2, fill="#26262e")

    def _draw_mouth(self, x, y, w, style):
        if style == "flat":
            self.create_line(x - w * 0.35, y, x + w * 0.35, y, width=2, fill="#26262e")
        elif style == "smile":
            self.create_arc(x - w * 0.5, y - w * 0.5, x + w * 0.5, y + w * 0.5, start=200, extent=140, style="arc", width=2, outline="#26262e")
        elif style == "frown":
            self.create_arc(x - w * 0.5, y - w * 0.1, x + w * 0.5, y + w * 0.9, start=20, extent=140, style="arc", width=2, outline="#26262e")
        elif style == "wave":
            self.create_line(x - w * 0.4, y, x - w * 0.1, y - w * 0.18, x + w * 0.15, y, x + w * 0.4, y - w * 0.14, width=2, fill="#26262e", smooth=True)


class TkAvatarProvider(AvatarProvider):
    """把 Agent 的状态推到界面。可被工作线程调用，刷新回主线程执行。"""

    name = "tkinter"

    def __init__(self, post, widget):
        self._post = post
        self.widget = widget
        self.expression = "normal"
        self.animation = None
        self.state = {}

    def set_expression(self, expression):
        self.expression = str(expression or "normal")
        self._refresh()
        return self.expression

    def play_animation(self, animation):
        self.animation = animation
        return self.animation

    def set_state(self, state):
        self.state = dict(state or {})
        emotion = self.state.get("emotion")
        if emotion:
            self.expression = EMOTION_EXPRESSIONS.get(emotion, emotion)
        self._refresh()
        return self.state

    def _refresh(self):
        try:
            self._post(lambda: self.widget.render(self.expression))
        except Exception:
            pass

    def snapshot(self):
        return {
            "expression": self.expression,
            "animation": self.animation,
            "state": dict(self.state),
        }


class Confirmer:
    """权限确认入口。界面就绪前一律拒绝（fail closed）。"""

    def __init__(self):
        self.ui = None

    def __call__(self, tool_name, level, reason):
        if self.ui is None:
            return False
        return self.ui.confirm(tool_name, level, reason)


class ComboPanel:
    """一个页面里装几块面板：外壳按面板算，里面各自管自己。"""

    def __init__(self, pet, win, parts):
        self.pet = pet
        self.win = win
        self.parts = list(parts or [])
        self.embedded = True

    def refresh(self):
        count = 0
        for part in self.parts:
            handler = getattr(part, "refresh", None)
            if not callable(handler):
                continue
            try:
                handler()
                count += 1
            except Exception:
                continue
        return count


class DesktopPet(StateStore, AvatarGlue, ChatWiring, PanelOpener, DeveloperPanel):
    """桌面宿主：桌宠本体是桌面上的角色，聊天是它旁边的小浮层。"""

    POLL_MS = 80
    SHELL_MS = 3000  # 重新抬窗、隐藏任务栏、对齐角色窗口的节奏
    HIT_MS = 2000  # 碰撞体积自检节奏：角色在画就得拖得动
    IDLE_MS = 5000  # 待机检查节奏；真正动不动由行为层节流
    ACTIVITY_MS = 10000  # 用户活动检查节奏；只看空闲时间，不看内容
    DETECTION_MS = 30000  # 视觉检测节奏；没接检测源时什么都不发
    SCREEN_MS = 30000  # 屏幕布局检查节奏；只读数字，不截图
    ENVIRONMENT_MS = 60000  # 环境感知节奏；只读电量与内存，不联网
    FOCUS_MS = 5000  # 专注模式检查节奏；只看窗口是不是全屏
    VOICE_MS = 200  # 有语音包时才启动的取音节奏
    AWARENESS_MS = 8000
    WORK_MS = 30000  # 工作记录采样；只读前台应用元数据，不读画面
    # 角色画面起来慢是正常的（WebView2 第一次要初始化），超过这个时间还没就绪
    # 就催一次；再等这么久还没好，如实说明并让占位形象先顶着。
    LIVE2D_READY_MS = 18000  # 屏幕感知节奏；只在用户开了以后才跑，只读应用名和标题
    SESSION_ID = "desktop"
    POSITION_KEY = "pet_position"
    # 被忽略过的建议记在这儿，键是「分组:名字」，逗号分隔。
    SUGGEST_MUTE_PREFERENCE = "suggest.mute"
    # 定稿规范的最小窗口。
    MIN_CARD_WIDTH = 1000
    MIN_CARD_HEIGHT = 680

    # 计划里的步骤状态 -> 聊天面板上小圆点的状态。两套词不一样，这里翻译一次。
    STEP_STATES = {
        "pending": "todo",
        "running": "running",
        "done": "done",
        "failed": "failed",
        "skipped": "todo",
        "waiting": "waiting",
    }

    # 点桌宠弹出的轻量菜单：只放现在真的能用的东西。
    MENU_ITEMS = (
        ("聊天", "toggle_chat"),
        ("贴到边上 / 取消贴边", "toggle_dock"),
        ("缩起来 / 展开", "toggle_peek"),
        ("人格", "open_persona"),
        ("记忆", "open_memory"),
        ("任务", "open_tasks"),
        ("工作记录", "open_work"),
        ("能力", "open_capabilities"),
        ("设置", "open_settings"),
        ("隐藏", "hide_pet"),
        ("退出", "close"),
    )

    def __init__(self, agent, config, memory=None, bus=None, scheduler=None, logger=None, root=None, behavior=None, voice=None, speech=None, stt=None, wake=None, mic=None, activity=None, focus=None, quiet=None, environment=None, screen=None, vision=None, awareness=None, detection=None, body=None, privacy=None, pending_tasks=None, capture=None, knowledge=None, worklog=None):
        self.agent = agent
        self.config = config
        self.memory = memory
        self.bus = bus
        self.scheduler = scheduler
        self.logger = logger
        self.behavior = behavior
        self.voice = voice
        self.speech = speech
        self.capture = capture if capture is not None else NullCapture("没有接入音频采集")
        self._record_busy = False
        self.stt = stt
        # 实时字幕只在用户手动开始时建立，不默认占用音频设备。
        self.subtitle = None
        self._subtitle_overlay = None
        self._subtitle_mode = "bilingual"
        self.subtitle_history = SubtitleHistoryRepository(
            getattr(getattr(memory, "repository", None), "db", None),
            logger=logger,
        )
        self.wake = wake
        self.mic = mic
        self.activity = activity
        self.focus = focus
        self.quiet = quiet
        self.environment = environment
        self.screen = screen
        self.vision = vision
        self.awareness = awareness
        self.worklog = worklog
        self.detection = detection
        self.body = body
        self.privacy = privacy
        # V9.1：本地知识库。没接入时是 None，界面如实说没接入。
        self.knowledge = knowledge
        # V3：上次没跑完的任务。只摆出来给用户看，不自动接着跑。
        self.pending_tasks = list(pending_tasks or [])
        self._privacy_vars = {}
        self._focus_hid_chat = False
        self.root = root or tk.Tk()
        # V4：主题在搭界面之前套好，换主题只要重画一遍聊天浮层。
        theme.apply_saved(self.memory)
        # 缩放要在建控件之前算好：间距、字号、窗口尺寸建的时候就读定了。
        theme.scale_saved(self.memory)
        # ttk 的表格、下拉、滚动条也按同一套色板走，别让系统主题漏出来。
        theme.style_widgets(self.root)
        # 会话：默认还是原来那一个；侧栏可以切到别的会话。
        self.session_id = self.SESSION_ID
        self._queue = queue.Queue()
        self._busy = False
        # 流式回复：一条气泡只服务一轮；轮次号一变，迟到的回复就丢弃。
        self._stream_open = False
        self._reply_token = 0
        # 在跑的回复线程：退出时要等它们收尾，免得带着写一半的数据库连接一起退。
        self._workers = []
        self.chat_visible = False
        # 最小化到任务栏时保持窗口存在；只有点 × 才真正收掉。
        self._chat_minimized = False
        self._taskbar_ready = False
        self._card_drag = (0, 0)
        self._card_size = (CARD_WIDTH, CARD_HEIGHT)
        self._resize = None
        self._last_user = ""
        self.last_reply = None
        self._closed = False
        # Live2D 角色窗口：接不上就一直保持 None，界面照旧用占位形象。
        self.live2d = None
        # 输入框里的附件清单、会话列表是否显示归档、正在等谁给权限答案。
        self._attachments = []
        self._show_archived = False
        self._permission_wait = None
        self._reminder_popups = {}
        self._reminder_seen = set()
        self._chat_ready = False
        self._live2d_echo = False
        self._live2d_factor = 1.0
        self._live2d_scale_value = 1.0
        self._placeholder_size = (148, 148)
        self._build()
        # 界面回调里的意外异常不能就这么消失，统一走日志 + 人话提示。
        self.root.report_callback_exception = self._on_ui_error

    # ---------- 线程安全 ----------
    def post(self, action):
        """任何线程都可以调用，动作会在主线程执行。"""
        self._queue.put(action)

    def _drain(self):
        while True:
            try:
                action = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                action()
            except Exception as exc:
                if self.logger:
                    self.logger.warning("ui action failed: %s", exc)
        self.root.after(self.POLL_MS, self._drain)

    # ---------- 界面搭建 ----------
    def _build(self):
        # 桌宠本体：一个透明窗口，里面只有角色。
        self.root.title("桌宠")  # 第二次双击时靠标题找到这个窗口
        self.host = PetHost(
            self.root,
            build_canvas=lambda parent: PetCanvas(
                parent,
                size=self._px(PetCanvas.DEFAULT_SIZE),
                bg=TRANSPARENT_KEY,
                draw_placeholder=False,
            ),
            logger=self.logger,
        )
        self.pet = self.host.canvas
        # 没有角色画面时也保留一块透明命中区，桌宠照样能拖动和右键。
        self.host.set_fallback_hit()
        # 左键互动、右键菜单：互动走行为层，不再每次都弹菜单。
        self.host.on_click = self._on_pet_click
        self.host.on_double_click = self._on_pet_double_click
        self.host.on_pointer_near = self._on_pointer_near
        self.host.on_pointer_away = self._on_pointer_away
        self.host.on_right_click = self.open_menu
        self.host.on_moved = self._on_pet_moved
        self.host.on_long_press = self._on_pet_long_press
        self.host.on_drag_end = self._on_pet_drag_end
        self.host.docking = self.dock_enabled()
        self.host.auto_hide = self.dock_auto_hide()
        self._live2d_ready = False
        self._live2d_problem = ""
        self._live2d_model = None
        self._apply_saved_position()
        self._restore_dock_edge()
        self._set_window_icon()
        self._stay_on_top()

        # 聊天：贴在桌宠旁边的小浮层，需要时再出现。
        self.card = tk.Toplevel(self.root)
        self.card.title("桌宠 · 聊天")
        self.card.overrideredirect(True)
        self.card.configure(bg=theme.BG)
        try:
            from ui import glass_native

            glass_native.apply_window(
                self.card, radius=theme.RADIUS_LG, alpha=0.98, tint=theme.CARD
            )
        except Exception as exc:
            self._warn("window glass failed: %s", exc)
        self.card.withdraw()
        self._build_card()

        # 界面接管形象：Agent 只认 AvatarProvider 接口，不关心背后是占位图还是 Live2D。
        previous = getattr(self.agent, "avatar", None)
        self.avatar = TkAvatarProvider(self.post, self.pet)
        if previous is not None and getattr(previous, "state", None):
            self.avatar.state = dict(previous.state)
        attach = getattr(previous, "attach_fallback", None)
        if attach is not None:
            # 形象接入层继续当 Agent 的形象，占位图退到下面兜底：
            # 模型和渲染器都到位时由 Live2D 接管，没到位就和以前一模一样。
            attach(self.avatar)
            self.avatar = previous
        self.agent.avatar = self.avatar
        if self.behavior is not None:
            self.behavior.avatar = self.avatar
            self.behavior.emotion = self.agent.emotion
        self.avatar.set_state(self.avatar.state or {"emotion": "neutral", "intensity": 0.5})
        # 有模型也有渲染环境时，角色交给独立透明窗口画；失败就继续用占位形象。
        self._attach_live2d()
        if self.pending_tasks:
            self._append("notice", self._pending_tasks_text())
        if not self.config.has_companion():
            # 空状态用一条短提示，不把大段错误说明塞进聊天正文。
            self._append(
                "notice",
                "尚未配置模型：本地功能仍然可用（报时间、记事、问日期）。"
                "点上面「设置 → 模型」填好就能聊天。",
            )
        self._refresh_status()

    def _greeting_text(self):
        """空会话里的第一句话。拿不到人格就用通用问候，不凭空编名字。"""
        persona = getattr(self.agent, "persona", None)
        name = ""
        try:
            profile = persona.get_profile() if persona is not None else None
        except Exception:
            profile = None
        if isinstance(profile, dict):
            name = str(profile.get("name") or "").strip()
        if name:
            return "我是%s。想聊什么直接说，任务类请求我会先问一声。" % name
        return ChatView.DEFAULT_GREETING

    def _build_card(self):
        """聊天浮层。控件与样式交给 ChatView，这里只接回调、接状态。"""
        try:
            self.card.bind("<Map>", self._on_card_map, add="+")
        except Exception:
            pass
        # 背景层要先建、沉到最底，后面创建的控件才会压在它上面。
        self._create_background()
        self._card_size = self._saved_card_size()
        self.chat = ChatView(
            self.card,
            on_send=self._send,
            on_cancel=self._cancel,
            on_clear=self._clear_chat,
            on_settings=self.open_settings,
            on_close=self.hide_chat,
            wraplength=max(theme.PAD_MD * 2 + 80, self._card_size[0] - 24),
            logger=self.logger,
            on_attach=self.attach_file,
            on_skills=self.skill_menu,
            on_tasks=self.open_tasks,
            on_session=self.switch_session,
            on_new_session=self.new_session,
            on_toggle_sidebar=self._save_sidebar_state,
            on_event=self._chat_event,
        )
        # Ctrl + 加减号 / 0：改界面缩放，只影响自己，不动系统设置。
        self.bind_zoom_keys(self.card)
        # 这些入口外部（含测试）一直在用，指向 ChatView 里的真实控件。
        self.transcript = self.chat.transcript
        self.input = self.chat.input
        self.send_button = self.chat.send_button
        self.status = self.chat.status
        self.state = self.chat.state
        for widget in self.chat.drag_handles:
            widget.bind("<ButtonPress-1>", self._card_press)
            widget.bind("<B1-Motion>", self._card_motion)
        self.card.bind("<Escape>", lambda event: self.hide_chat())
        self._update_hero_banner()
        # 截图是用户主动点的一次动作：点了才截，不点不截，也不上传。
        self.chat.menu.add_command(label="看一眼屏幕", command=self.look_at_screen)

        self.chat.show_sidebar(self._sidebar_state())
        self.chat.set_greeting(self._greeting_text())
        self.chat.set_commands(self.chat_commands())
        self._chat_ready = False
        self.chat.toggle_nav(force=self._preference("chat.nav") != "0")
        self.chat.set_panel(self._preference("chat.panel") == "1")
        # 任务视图的偏好单独记；默认不动面板，免得和面板偏好打架。
        if self._preference("chat.task_view") == "1":
            self._task_view_on = True
            self.chat.set_task_mode(True)
            self.chat.set_panel_text(self._task_panel_text())
        self.chat.set_tips(self.chat_tips())
        self.chat.set_feed(self.home_feed())
        self._chat_ready = True
        self.refresh_sessions()

        self.dev = None
        if self.config.dev_mode:
            self.dev = tk.Text(
                self.card, height=6, bg=theme.PANEL, fg=theme.MUTED, bd=0, wrap="word",
                font=(theme.MONO, 8), state="disabled",
            )
            self.dev.pack(fill="x", padx=theme.PAD_MD, pady=(0, theme.PAD_SM))
        # 右下角缩放把手。松手才算改完，改完就记住。
        self.chat.pack_grip()
        self.chat.grip.bind("<ButtonPress-1>", self._resize_start)
        self.chat.grip.bind("<B1-Motion>", self._resize_move)
        self.chat.grip.bind("<ButtonRelease-1>", self._resize_done)

    # ---------- Live2D 角色 ----------
    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)

    def _pet_window_handle(self):
        """透明载体的系统句柄。角色窗口靠它对齐，不受工作目录和缩放影响。"""
        try:
            handle = int(self.root.winfo_id())
            parent = int(ctypes.windll.user32.GetParent(handle) or 0)
            return parent or handle
        except Exception:
            return 0

    def _attach_live2d(self):
        """把角色画面接到桌宠身上。任何一步失败都只是继续用占位形象。"""
        try:
            return self._attach_live2d_inner()
        except Exception as exc:
            self._warn("live2d attach failed: %s", exc)
            self.live2d = None
            return None

    def _attach_live2d_inner(self):
        provider = self._avatar_provider()
        if provider is None or getattr(provider, "model", None) is None:
            return None
        self._live2d_model = provider.model
        host = getattr(provider, "host", None)
        if host is None or not hasattr(host, "attach_window"):
            return None
        try:
            if not host.is_available():
                reason = getattr(host, "unavailable_reason", lambda: "")()
                if self.logger:
                    self.logger.info("live2d render host unavailable: %s", reason)
                return self._live2d_give_up(reason or "这台机器上没有可用的角色渲染环境")
        except Exception as exc:
            self._warn("live2d host probe failed: %s", exc)
            return None
        side = int(getattr(self.pet, "size", 148) or 148)
        self._placeholder_size = (side, side)
        try:
            host.attach_window(self._pet_window_handle)
            host.set_sink(self._on_live2d_event)
            self.host.on_geometry = self._on_pet_geometry
            self.host.on_visibility = self._on_pet_visibility
        except Exception as exc:
            self._warn("live2d wiring failed: %s", exc)
            return None
        self.live2d = host
        self._live2d_scale_value = self._saved_scale(
            float(getattr(host, "scale", lambda: 1.0)())
        )
        try:
            result = provider.load_model() or {}
        except Exception as exc:
            self._warn("live2d load failed: %s", exc)
            result = {"ok": False, "error": str(exc)}
        if not result.get("ok"):
            self.live2d = None
            if self.logger:
                self.logger.info("live2d not started: %s", result.get("error"))
            return self._live2d_give_up(result.get("error") or "渲染进程没起来")
        # 配了模型就别再摆占位形象了：加载期间先空着，别让人以为这就是角色。
        self.pet.set_blank(True)
        self._live2d_problem = ""
        self._live2d_ready = False
        if self.logger:
            self.logger.info("live2d render attached: %s", provider.model.path)
        self._arm_live2d_ready_check()
        return host

    def _arm_live2d_ready_check(self, tries=1):
        """过一会儿看角色画面到底出来了没有。"""
        try:
            self.root.after(
                self.LIVE2D_READY_MS, lambda: self._live2d_ready_check(tries)
            )
        except Exception as exc:
            self._warn("live2d ready check failed to schedule: %s", exc)
        return tries

    def _on_pet_geometry(self, x, y, width, height):
        """桌宠窗口动了就让角色窗口跟上；角色自己拖动时不回声，免得来回抖。"""
        if self.live2d is None or self._live2d_echo:
            return
        if self.host.hidden:
            return
        try:
            self.live2d.sync_geometry()
        except Exception as exc:
            self._warn("live2d geometry sync failed: %s", exc)

    def _on_pet_visibility(self, hidden):
        if self.live2d is None:
            return
        try:
            self.live2d.set_visible(not hidden)
        except Exception as exc:
            self._warn("live2d visibility failed: %s", exc)

    def _on_live2d_event(self, name, payload):
        """渲染子进程的工作线程调进来，一律切回主线程再动界面。"""
        self.post(lambda: self._handle_live2d_event(name, payload))

    def _handle_live2d_event(self, name, payload):
        try:
            if name == "live2d_ready":
                info = payload or {}
                try:
                    self._live2d_factor = float(info.get("dpi_scale") or 1.0) or 1.0
                except (TypeError, ValueError):
                    self._live2d_factor = 1.0
                self._fit_live2d_body(info)
                # 渲染进程是重起的，减少动态效果这类偏好要重新告诉它一次。
                self._apply_motion()
                self.pet.set_blank(True)
                self._live2d_ready = True
                self._live2d_problem = ""
                self._hint("角色已显示")
                self._refresh_status()
                return
            if name == "live2d_mask":
                self._apply_hit_volume(payload)
                return
            if name == "live2d_restarting":
                # 角色窗口挂了或假死了，宿主会自己重起一个；这期间先让占位形象撑住，
                # 桌宠不会从桌面上消失。
                detail = ""
                if isinstance(payload, dict):
                    detail = str(payload.get("reason") or "")
                self.pet.set_blank(True)
                self.host.clear_hit_mask()
                self.host.set_fallback_hit()
                if detail and self.logger:
                    self.logger.info("live2d restarting: %s", detail)
                self._hint("角色画面在重新加载，桌面暂时只保留透明拖动区域。")
                self._refresh_status()
                return
            if name in ("live2d_error", "live2d_exit"):
                detail = ""
                if isinstance(payload, dict):
                    detail = str(payload.get("error") or payload.get("reason") or "")
                if self._live2d_still_rendering():
                    # 画面其实还在画。这条多半是中间某一步没读到状态，
                    # 不能因为一句误会就把角色换成占位形象、把点击也关掉。
                    if detail and self.logger:
                        self.logger.info("live2d error ignored, still rendering: %s", detail)
                    self._resume_live2d()
                    return
                self.pet.set_blank(True)
                self.host.clear_hit_mask()
                self.host.set_fallback_hit()
                if self.live2d is not None:
                    self._restore_placeholder()
                self._hint(
                    "角色暂时显示不了，桌面只保留透明拖动区域。"
                    + (f"（{detail}）" if detail else "")
                )
                return
            if name in ("live2d_moved", "live2d_moving"):
                self._apply_live2d_position(payload, final=name == "live2d_moved")
                return
            if name == "live2d_hover":
                self._behavior_interaction("pointer_near")
                return
            if name == "live2d_unhover":
                self._behavior_interaction("pointer_away")
                return
            if name == "live2d_click":
                self._on_pet_click()
                return
            if name == "live2d_double_click":
                self._on_pet_double_click()
                return
            if name == "live2d_right_click":
                self.open_menu()
                return
        except Exception as exc:
            self._warn("live2d event %s failed: %s", name, exc)

    def _resume_live2d(self):
        """误会一场：把角色重新认回来，碰撞体积和状态一起补上。"""
        self._live2d_problem = ""
        self._hit_misses = 0
        if self.live2d is not None and not self.host.has_hit():
            try:
                self.live2d.request_hit()
            except Exception as exc:
                self._warn("live2d hit refresh failed: %s", exc)
        self.pet.set_blank(True)
        try:
            self.live2d.sync_geometry()
        except Exception as exc:
            self._warn("live2d resume geometry failed: %s", exc)
        self._live2d_ready = True
        self._refresh_status()

    def _apply_hit_volume(self, payload):
        """把角色当前画面的形状交给桌宠窗口当碰撞体积。

        这样鼠标落在角色身上才算点中它，透明处照旧点得到桌面。
        """
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or self.host is None:
            return False
        try:
            cols = int(data.get("cols") or 0)
            rows = int(data.get("rows") or 0)
            bits = base64.b64decode(str(data.get("data") or ""))
        except Exception as exc:
            self._warn("live2d mask decode failed: %s", exc)
            return False
        if cols <= 0 or rows <= 0 or len(bits) * 8 < cols * rows:
            # 形状本身就不对：这是坏数据，不动现在的碰撞体积。
            self._warn("live2d mask shape rejected: %sx%s" % (cols, rows))
            return False
        if not any(bits):
            # 形状没问题但一个点都没有：角色在画却整块点不着，退回兜底体积。
            return self.host.set_fallback_hit()
        return self.host.set_hit_mask(cols, rows, bits)

    def _fit_live2d_body(self, status):
        """窗口大小按模型自身比例算，保证不变形也不裁切。"""
        height = int(getattr(self.config, "live2d_height", 320) or 320)
        height = max(120, min(1200, int(height * max(0.2, self._live2d_scale_value))))
        aspect = 0.55
        box = (status or {}).get("boxSize")
        try:
            if box and len(box) == 2 and float(box[1]):
                aspect = float(box[0]) / float(box[1])
        except (TypeError, ValueError):
            aspect = 0.55
        aspect = max(0.25, min(2.0, aspect))
        width = max(80, int(round(height * aspect)))
        self._live2d_echo = True
        try:
            self.host.resize_body(width, height)
        finally:
            self._live2d_echo = False
        try:
            self.live2d.sync_geometry()
        except Exception as exc:
            self._warn("live2d fit failed: %s", exc)
        return width, height

    def _apply_live2d_position(self, payload, final=False):
        """角色窗口是 DPI 感知的，坐标要按缩放换回 Tk 的逻辑坐标。"""
        if not isinstance(payload, dict):
            return
        try:
            x = int(payload.get("x") or 0)
            y = int(payload.get("y") or 0)
        except (TypeError, ValueError):
            return
        factor = self._live2d_factor or 1.0
        self._live2d_echo = True
        try:
            self.host.move_to(x / factor, y / factor)
        finally:
            self._live2d_echo = False
        if self.chat_visible:
            self._place_card()
        if final:
            # 角色窗口转发来的拖动不走 Tk 的松手回调，这里自己补一次吸附判定。
            self.host.snap_after_drag()
            self._save_preference(
                self.DOCK_EDGE_PREFERENCE, self.host.docked_edge() or ""
            )
            self._behavior_interaction("drag")
            self._save_position()

    # ---------- 角色设置 ----------
    @staticmethod
    def _join_names(values, empty="没有"):
        items = [str(item) for item in (values or []) if str(item)]
        return "、".join(items) if items else empty

    def avatar_rows(self):
        """角色页只展示真实检测到的结果，不猜、不假装。"""
        provider = self._avatar_provider()
        rows = list(self._character_rows())
        rows.append(("模型目录", str(getattr(self.config, "live2d_dir", ""))))
        if provider is None:
            rows.append(("形象接入", "透明载体（Live2D 未启用）"))
            return rows
        model = getattr(provider, "model", None)
        rows.append(("当前显示模型", model.label() if model else "还没放模型"))
        rows.append(("模型文件", getattr(model, "path", "") or "没有找到入口文件"))
        rows.append(("渲染方式", self._live2d_backend_text()))
        rows.append(("WebView2 运行时", self._live2d_webview2_text()))
        rows.append(("加载状态", self._live2d_state_text()))
        rows.append(("角色缩放", f"{self._live2d_scale_value:.2f} 倍"))
        if model is not None:
            rows.append(("贴图", f"{len(getattr(model, 'textures', ()))} 张"))
            rows.append(("表情", self._join_names(getattr(model, "expressions", ()))))
            rows.append(("动作", self._join_names(list(getattr(model, "motions", {}) or {}))))
            rows.append(("缺少文件", self._join_names(list(getattr(model, "missing_files", lambda: ())() or ()))))
            rows.append(("物理", getattr(model, "physics", "") or "没有"))
            rows.append(("姿态", getattr(model, "pose", "") or "没有"))
        return rows

    def _remember(self, key, value):
        """角色的显示设置跟着偏好走，重启后照旧。"""
        try:
            self._save_preference("live2d." + key, value)
        except Exception as exc:
            self._warn("remember %s failed: %s", key, exc)

    # ---------- 桌宠位置 ----------
    def _apply_saved_position(self):
        """把记住的位置应用一次。窗口刚建好时尺寸可能还没稳定，run() 里会再校准一次。"""
        saved = self._preference(self.POSITION_KEY)
        if not saved:
            return
        try:
            x, y = (int(part) for part in str(saved).split(",", 1))
        except (TypeError, ValueError):
            return
        applied = self.host.move_to(self._px(x), self._px(y))
        if self.logger and "%d,%d" % applied != str(saved).strip():
            # 只有被屏幕边界挪动过才记一笔，正常恢复不刷日志。
            self.logger.info(
                "pet position adjusted: saved=%s applied=%s size=%s",
                saved,
                applied,
                self.host.size(),
            )

    def dock_enabled(self):
        """拖到边上要不要自动吸附。默认开。"""
        return str(self._preference(self.DOCK_PREFERENCE) or "1").strip() != "0"

    def dock_auto_hide(self):
        """吸附后要不要自动缩入。默认开。"""
        return str(self._preference(self.DOCK_AUTO_PREFERENCE) or "1").strip() != "0"

    def set_dock(self, enabled):
        """开关自动吸附。关掉时顺手取消掉当前的贴边状态。"""
        flag = bool(enabled)
        self._save_preference(self.DOCK_PREFERENCE, "1" if flag else "0")
        self.host.docking = flag
        if not flag:
            self.undock_pet()
        self._hint("拖到屏幕边上会自动吸附。" if flag else "已关掉自动吸附，桌宠停哪就在哪。")
        return flag

    def set_dock_auto(self, enabled):
        """开关缩入。开着才有「只露一角」的效果。"""
        flag = bool(enabled)
        self._save_preference(self.DOCK_AUTO_PREFERENCE, "1" if flag else "0")
        self.host.auto_hide = flag
        if not flag and self.host.peeked():
            self.host.slide_out()
        self._hint("贴边后会缩到边上，鼠标碰一下就出来。" if flag else "贴边后保持完整显示。")
        return flag

    def _dock_edge_name(self, edge):
        return {
            "left": "左边",
            "right": "右边",
            "top": "上边",
            "bottom": "下边",
        }.get(str(edge or ""), "")

    def dock_pet(self, edge=None):
        """贴到最近的一条边，也可以指定。"""
        if not self.host.docking:
            self.host.docking = True
            self._save_preference(self.DOCK_PREFERENCE, "1")
        placed = self.host.snap_to_edge(edge)
        if not placed:
            self._hint("没找到可以贴的边。")
            return ""
        self._save_preference(self.DOCK_EDGE_PREFERENCE, placed)
        self._save_position()
        if self.chat_visible:
            self._place_card()
        self._hint("已贴到屏幕%s。" % self._dock_edge_name(placed))
        return placed

    def undock_pet(self):
        """取消贴边，回到完整显示。"""
        self.host.slide_out()
        self.host.undock()
        self._save_preference(self.DOCK_EDGE_PREFERENCE, "")
        self._save_position()
        if self.chat_visible:
            self._place_card()
        self._hint("已取消贴边。")
        return True

    def toggle_dock(self):
        if self.host.docked_edge():
            return self.undock_pet()
        return self.dock_pet()

    def toggle_peek(self):
        """缩入 / 滑出。没贴边时不能缩，直接说清楚。"""
        if not self.host.docked_edge():
            self._hint("先贴到屏幕边上，才能缩起来。")
            return False
        self.host.toggle_peek()
        peeked = self.host.peeked()
        self._hint("桌宠缩到边上了，鼠标碰一下就会出来。" if peeked else "桌宠回来了。")
        return peeked

    def dock_text(self):
        """设置页看的一行状态。"""
        edge = self.host.docked_edge()
        if not edge:
            return "没贴边"
        name = self._dock_edge_name(edge)
        if self.host.peeked():
            return "贴在%s，已缩入" % name
        return "贴在%s" % name

    def reset_position(self):
        """把桌宠请回默认角落，位置照旧记住。"""
        if self.host.docked_edge():
            self.host.slide_out()
            self.host.undock()
            self._save_preference(self.DOCK_EDGE_PREFERENCE, "")
        try:
            x, y = self.host.default_position()
        except Exception as exc:
            self._warn("default position failed: %s", exc)
            return None
        self.host.move_to(x, y)
        self._save_position()
        if self.chat_visible:
            self._place_card()
        return self.host.position()

    def position_text(self):
        try:
            x, y = self.host.position()
        except Exception:
            return "读取失败"
        return f"{x},{y}"

    def _on_pet_moved(self, position):
        self._behavior_interaction("drag")
        # 松手后宿主可能刚刚吸附到边上，把当前那条边记下来。
        self._save_preference(self.DOCK_EDGE_PREFERENCE, self.host.docked_edge() or "")
        self._save_position()
        if self.chat_visible:
            self._place_card()

    def _on_pet_long_press(self, position=None):
        """按住不动够久：只做一次反应，不改变窗口位置。"""
        self._behavior_interaction("long_press")
        return self._live2d_react("long_press")

    def _on_pet_drag_end(self, position=None):
        """拖到别处再松手：给一个放下的反应。位置由 _on_pet_moved 记。"""
        return self._live2d_react("drag_end")

    # ---------- 互动与待机（都走行为层，不调用模型） ----------
    def _on_pet_click(self, event=None):
        self._behavior_interaction("click")
        self._live2d_react("click")

    def _on_pet_double_click(self, event=None):
        self._behavior_interaction("double_click")
        self._live2d_react("double_click")

    def _on_pointer_near(self, event=None):
        self._behavior_interaction("pointer_near")
        self._live2d_react("hover")

    def _on_pointer_away(self, event=None):
        self._behavior_interaction("pointer_away")

    def _behavior_interaction(self, name):
        if self.behavior is None:
            return None
        try:
            return self.behavior.on_interaction(name)
        except Exception as exc:
            if self.logger:
                self.logger.warning("behavior interaction failed: %s", exc)
            return None

    def _idle_tick(self):
        focused = self.focus is not None and self.focus.active
        if self.behavior is not None and not focused:
            try:
                self.behavior.idle()
            except Exception as exc:
                if self.logger:
                    self.logger.warning("behavior idle failed: %s", exc)
        self.root.after(self.IDLE_MS, self._idle_tick)

    def user_away(self, seconds=0.0):
        """用户离开电脑：让行为层停下待机小动作。"""
        if self.behavior is None:
            return None
        try:
            return self.behavior.user_away()
        except Exception as exc:
            if self.logger:
                self.logger.warning("activity away failed: %s", exc)
            return None

    def user_back(self, seconds=0.0):
        """用户回到电脑：恢复互动节奏。"""
        if self.behavior is None:
            return None
        try:
            return self.behavior.user_back()
        except Exception as exc:
            if self.logger:
                self.logger.warning("activity back failed: %s", exc)
            return None

    def _activity_tick(self):
        """看看用户还在不在。程序判断，不经过模型。"""
        if self.activity is not None:
            try:
                self.activity.check()
            except Exception as exc:
                if self.logger:
                    self.logger.warning("activity check failed: %s", exc)
        self.root.after(self.ACTIVITY_MS, self._activity_tick)

    def enter_focus(self):
        """用户全屏了：角色变淡、收起聊天浮层，别挡着。"""
        if self.chat_visible:
            try:
                self.hide_chat()
                self._focus_hid_chat = True
            except Exception as exc:
                if self.logger:
                    self.logger.warning("focus hide chat failed: %s", exc)
        try:
            return self.host.set_dim(True)
        except Exception as exc:
            if self.logger:
                self.logger.warning("focus dim failed: %s", exc)
            return False

    def exit_focus(self):
        """用户退出全屏：恢复原样，之前开着的聊天会自己回来。"""
        try:
            self.host.set_dim(False)
        except Exception as exc:
            if self.logger:
                self.logger.warning("focus undim failed: %s", exc)
        if self._focus_hid_chat:
            self._focus_hid_chat = False
            try:
                self.show_chat()
            except Exception as exc:
                if self.logger:
                    self.logger.warning("focus show chat failed: %s", exc)
        return False

    def _screen_tick(self):
        """屏幕布局变了就重新校准位置，别让自己留在看不见的地方。"""
        if self.screen is not None:
            try:
                if self.screen.update():
                    self._apply_saved_position()
            except Exception as exc:
                if self.logger:
                    self.logger.warning("screen check failed: %s", exc)
        self.root.after(self.SCREEN_MS, self._screen_tick)

    def _awareness_tick(self):
        """看一眼当前应用。用户没开感知就不做任何事，也不建任何东西。"""
        if self.awareness is not None and self.awareness.active():
            try:
                self.awareness.observe()
            except Exception as exc:
                if self.logger:
                    self.logger.warning("awareness read failed: %s", exc)
        self.root.after(self.AWARENESS_MS, self._awareness_tick)

    def _work_tick(self):
        """Record one foreground interval only when work mode is explicitly on."""
        if self.worklog is not None:
            try:
                idle = self.activity.idle_seconds() if self.activity is not None else 0.0
                self.worklog.observe(idle_seconds=idle)
            except Exception as exc:
                if self.logger:
                    self.logger.warning("work record sample failed: %s", exc)
        self.root.after(self.WORK_MS, self._work_tick)

    def awareness_rows(self):
        """设置页与感知页要看的一屏数据。没接入就说没接入。"""
        if self.awareness is None:
            return []
        rows = []
        seen = set()

        def add(label, reader):
            if label in seen:
                return
            try:
                value = reader()
            except Exception as exc:
                rows.append((label, "读不到（%s）" % exc))
                seen.add(label)
                return
            rows.append((label, value))
            seen.add(label)

        add("感知开关", lambda: "已开启" if self.awareness.active() else self.awareness.status())
        add("读取层级", lambda: self.awareness.describe()["level_label"])
        add("隐私开关", lambda: "允许" if self.awareness.privacy_ok() else "在隐私设置里被关掉了")
        add("现在在用", lambda: self.awareness.observe().get("app") or "读不到")
        add("应用类别", lambda: self.awareness.observe().get("category_label") or "未分类")
        add("把握程度", lambda: self.awareness.observe().get("confidence_label") or "无法确定")
        add("最近一次", lambda: self.awareness.observe().get("at") or "还没读过")
        add("排除的应用", lambda: "、".join(self.awareness.exclusions()) or "没有")
        return rows

    def set_awareness_level(self, level):
        if self.awareness is None:
            self._hint("这一层没有接入屏幕感知。")
            return ""
        picked = self.awareness.set_level(level)
        if picked != "off":
            self._hint("屏幕感知已开启：%s。" % self.awareness.describe()["level_label"])
        else:
            self._hint("屏幕感知已关闭。")
        return picked

    def toggle_awareness(self):
        if self.awareness is None:
            self._hint("这一层没有接入屏幕感知。")
            return False
        if self.awareness.active():
            return self.set_awareness_level("off")
        if not self.awareness.privacy_ok():
            self._hint("要先去「权限与设置」里允许读取当前应用。")
            return False
        return self.set_awareness_level("metadata")

    def clear_awareness(self):
        if self.awareness is None:
            return False
        cleared = self.awareness.clear()
        self._hint("已清掉这一轮的临时上下文。" if cleared else "本来就没什么可清的。")
        return cleared

    def exclude_awareness_app(self, app):
        if self.awareness is None:
            return ()
        name = str(app or "").strip()
        if not name:
            name = str(self.awareness.observe().get("app") or "").strip()
        if not name:
            self._hint("现在读不到应用名，没法加进排除名单。")
            return self.awareness.exclusions()
        items = self.awareness.exclude(name)
        self._hint("以后不再看 %s。" % name)
        return items

    # ---------- 工作记录 ----------
    def enable_worklog(self, consent=False):
        if self.worklog is None:
            self._hint("工作记录没有接入。")
            return False
        ok = self.worklog.set_enabled(True, consent=bool(consent))
        if ok:
            self._hint("工作记录已开启。只会记录前台应用和时长，可随时暂停或删除。")
        else:
            self._hint("需要先明确允许记录，才能开启工作模式。")
        self._refresh_status()
        return ok

    def pause_worklog(self):
        if self.worklog is None:
            return False
        ok = self.worklog.pause()
        self._hint("工作记录已暂停。" if ok else "暂停没存下来。")
        self._refresh_status()
        return ok

    def resume_worklog(self):
        if self.worklog is None:
            return False
        ok = self.worklog.resume()
        self._hint("工作记录已恢复。" if ok else "工作记录还没有开启。")
        self._refresh_status()
        return ok

    def stop_worklog(self):
        if self.worklog is None:
            return False
        ok = self.worklog.set_enabled(False)
        self._hint("工作记录已关闭，不再采集新的应用活动。" if ok else "关闭没存下来。")
        self._refresh_status()
        return ok

    def include_awareness_app(self, app):
        if self.awareness is None:
            return ()
        return self.awareness.include(app)

    def _detection_tick(self):
        """看一眼检测源：只在出现/消失时发事件，没接检测源时什么都不发。"""
        if self.detection is not None:
            try:
                self.detection.update()
            except Exception as exc:
                if self.logger:
                    self.logger.warning("detection check failed: %s", exc)
        self.root.after(self.DETECTION_MS, self._detection_tick)

    def _environment_tick(self):
        """读一次环境状态；只在变化时发事件，不做判断也不问模型。"""
        if self.environment is not None:
            try:
                self.environment.update()
            except Exception as exc:
                if self.logger:
                    self.logger.warning("environment check failed: %s", exc)
        self.root.after(self.ENVIRONMENT_MS, self._environment_tick)

    def _focus_tick(self):
        """看看用户是不是在全屏。程序判断，不经过模型。"""
        if self.focus is not None:
            try:
                self.focus.update()
            except Exception as exc:
                if self.logger:
                    self.logger.warning("focus check failed: %s", exc)
        self.root.after(self.FOCUS_MS, self._focus_tick)

    # ---------- 聊天浮层 ----------
    def toggle_chat(self):
        if self.chat_visible:
            self.hide_chat()
        else:
            self.show_chat()
        return self.chat_visible

    def show_chat(self):
        if getattr(self, "_chat_minimized", False):
            try:
                restore_window(self.card)
            except Exception as exc:
                self._warn("restore chat failed: %s", exc)
            self._chat_minimized = False
        self.chat_visible = True
        try:
            self.chat.set_tips(self.chat_tips())
            self.chat.set_feed(self.home_feed())
        except Exception as exc:
            self._warn("home feed failed: %s", exc)
        self._save_preference("chat.open", "1")
        try:
            self._place_card()
        except Exception as exc:
            # 定位算不出来也要把窗口打开，不能卡在这一步上不显示。
            self._warn("chat placement failed: %s", exc)
        self.card.deiconify()
        self.card.lift()
        if not getattr(self, "_taskbar_ready", False):
            try:
                self.card.update_idletasks()
                if show_in_taskbar(self.card):
                    self._taskbar_ready = True
            except Exception as exc:
                self._warn("chat taskbar entry failed: %s", exc)
        try:
            self.input.focus_set()
        except Exception:
            pass
        return True

    def hide_chat(self):
        self.chat_visible = False
        self._chat_minimized = False
        self._save_preference("chat.open", "0")
        self.card.withdraw()
        return False

    def _on_card_map(self, event=None):
        """从任务栏点回来时会触发 Map：把最小化标记清掉。"""
        if event is not None and getattr(event, "widget", None) is not self.card:
            return None
        self._chat_minimized = False
        self.chat_visible = True
        return True

    def _place_card(self):
        width, height = self._card_size
        x, y = self.host.card_position(width, height)
        self.card.geometry(f"{width}x{height}+{x}+{y}")

    def _card_press(self, event):
        self._card_drag = (event.x_root - self.card.winfo_x(), event.y_root - self.card.winfo_y())

    def _card_motion(self, event):
        self.card.geometry(f"+{event.x_root - self._card_drag[0]}+{event.y_root - self._card_drag[1]}")

    def _resize_start(self, event):
        width, height = self._card_size
        self._resize = (event.x_root, event.y_root, width, height)

    def _resize_move(self, event):
        if not self._resize:
            return
        x0, y0, width, height = self._resize
        width = max(self._px(self.MIN_CARD_WIDTH), width + (event.x_root - x0))
        height = max(self._px(self.MIN_CARD_HEIGHT), height + (event.y_root - y0))
        self._card_size = (width, height)
        self._place_card()

    def _resize_done(self, event=None):
        """松手才写偏好，拖一次不会刷一堆记录。"""
        if not self._resize:
            return False
        self._resize = None
        self._save_card_size()
        return True

    def _px(self, value):
        """把设计值换成当前显示比例下的实际像素。"""
        try:
            return max(1, int(round(float(value) * theme.current_scale())))
        except Exception:
            return int(value)

    def _unpx(self, value):
        """把屏幕上的实际像素换回设计值，落盘用设计值，换显示器才不会串味。"""
        try:
            scale = theme.current_scale() or 1.0
            return max(1, int(round(float(value) / scale)))
        except Exception:
            return int(value)

    def _default_card_size(self):
        """首次打开的窗口尺寸：按屏幕比例算，够大但不铺满。"""
        try:
            screen_w = int(self.root.winfo_screenwidth())
            screen_h = int(self.root.winfo_screenheight())
        except Exception:
            return (self._px(CARD_WIDTH), self._px(CARD_HEIGHT))
        if screen_w <= 1 or screen_h <= 1:
            return (self._px(CARD_WIDTH), self._px(CARD_HEIGHT))
        width = max(self._px(CARD_TARGET_WIDTH), int(screen_w * CARD_SCREEN_WIDTH_RATIO))
        height = max(self._px(CARD_TARGET_HEIGHT), int(screen_h * CARD_SCREEN_HEIGHT_RATIO))
        # 屏幕放不下就退到九成，别顶到边上。
        width = min(width, int(screen_w * 0.94))
        height = min(height, int(screen_h * 0.94))
        return (
            max(self._px(self.MIN_CARD_WIDTH), min(width, self._px(MAX_CARD_WIDTH))),
            max(self._px(self.MIN_CARD_HEIGHT), min(height, self._px(MAX_CARD_HEIGHT))),
        )

    def bind_zoom_keys(self, widget):
        """给一个窗口挂上缩放快捷键。重建界面时会再挂一次，先解掉旧的免得叠起来。"""
        count = 0
        for sequence, action in self.ZOOM_KEYS:
            try:
                widget.unbind(sequence)
            except Exception:
                pass
            try:
                widget.bind(sequence, lambda event, name=action: self.zoom(name), add="+")
                count += 1
            except Exception as exc:
                self._warn("zoom binding failed for %s: %s", sequence, exc)
        return count

    def zoom(self, action):
        action = str(action or "").strip().lower()
        if action == "in":
            return self.zoom_in()
        if action == "out":
            return self.zoom_out()
        return self.zoom_reset()

    def zoom_in(self):
        return self.set_ui_scale(theme.step_scale(theme.user_scale(), 1))

    def zoom_out(self):
        return self.set_ui_scale(theme.step_scale(theme.user_scale(), -1))

    def zoom_reset(self):
        """恢复默认：倍率回 100%，字号回标准。"""
        return self.set_ui_scale(1.0, font_scale_name=theme.DEFAULT_FONT_SCALE)

    def ui_scale(self):
        return theme.user_scale()

    def font_scale_name(self):
        return theme.font_scale()

    def scale_text(self):
        """给设置页显示用：当前是系统比例 × 用户倍率。"""
        return "%d%%（系统 %d%% × 界面 %d%%）" % (
            round(theme.current_scale() * 100),
            round(theme.dpi_scale() * 100),
            round(theme.user_scale() * 100),
        )

    def set_ui_scale(self, value, font_scale_name=None, rebuild=True):
        """改界面缩放：先算好新尺寸，再重建界面。"""
        theme.apply_scale(
            ui_scale=value,
            font_scale_name=theme.font_scale() if font_scale_name is None else font_scale_name,
            dpi=theme.dpi_scale() or theme.system_dpi_scale(),
        )
        self._save_preference(theme.SCALE_PREFERENCE, "%.2f" % theme.user_scale())
        self._save_preference(theme.FONT_SCALE_PREFERENCE, theme.font_scale())
        if rebuild:
            self._rebuild_ui()
        else:
            self._refresh_status()
        return theme.user_scale()

    def set_font_scale(self, name, rebuild=True):
        return self.set_ui_scale(
            theme.user_scale(), font_scale_name=name, rebuild=rebuild
        )

    def _rebuild_ui(self, hint=None):
        """重建聊天工作台。控件尺寸和颜色在创建时就定了，改缩放、换主题只能重开一遍。"""
        page = ""
        try:
            page = str(getattr(self.chat, "page_key", "") or "")
        except Exception:
            page = ""
        was_open = bool(getattr(self, "chat_visible", False))
        # ChatView 本身不是控件，它管着的控件都挂在 card 上，所以清的是 card 的子控件。
        try:
            self.chat.detach()
        except Exception as exc:
            self._warn("chat detach failed: %s", exc)
        try:
            for child in list(self.card.winfo_children()):
                child.destroy()
        except Exception as exc:
            self._warn("chat rebuild failed: %s", exc)
            return None
        # 旧的功能页对象还攥着已经销毁的控件，一起丢掉，重建时重新读一遍数据。
        self.forget_panels()
        # Toplevel 换了新句柄，任务栏样式必须重新挂一次。
        self._taskbar_ready = False
        self._build_card()
        self._place_card()
        self.chat.clear()
        self._load_history(self.session_id)
        self.refresh_sessions()
        self._refresh_chat_title()
        if was_open:
            try:
                self.card.deiconify()
            except Exception:
                pass
        if page:
            self._chat_nav(page)
        self._hint(hint or "界面缩放已改成 %d%%。" % round(theme.current_scale() * 100))
        return self.chat

    def hide_pet(self):
        self.host.set_hidden(True)
        if self.logger:
            self.logger.info("pet hidden, click the dot to bring it back")

    def toggle_visible(self):
        """显示 / 隐藏桌宠。托盘右键和设置页共用这一个入口。"""
        if self.host.hidden:
            self.host.set_hidden(False)
            self._hint("桌宠已显示。")
        else:
            self.hide_pet()
            self._hint("桌宠已隐藏，托盘里还能叫回来。")
        return True

    # ---------- 语音中心 ----------
    def voice_center_rows(self):
        """语音状态一览：麦克风、监听、唤醒词、识别、发声、语音包。只给界面看。"""
        mic = getattr(self, "mic", None)
        rows = [("麦克风", self._mic_text(mic))]
        rows.append(("能不能听", "允许" if self._privacy_allows("listen") else "关闭（权限页可改）"))
        voice = getattr(self, "voice", None)
        collecting = bool(voice is not None and getattr(voice, "listening", False))
        rows.append(("监听", "正在采集" if collecting else "没有采集"))
        wake = getattr(self, "wake", None)
        if wake is None:
            rows.append(("唤醒词", "未接入"))
        else:
            words = "、".join(getattr(wake, "words", ()) or ())
            rows.append(("唤醒词", ("等唤醒：" if wake.armed else "已停用：") + (words or "无")))
        rows.append(("怎么说话", self._voice_input_note()))
        rows.append(("识别引擎", self._stt_note()))
        rows.append(("录音设备", self._capture_note()))
        rows.append(("语音输出", self._provider_note(getattr(self, "speech", None))))
        pack = self.current_voice_pack()
        rows.append(("语音包", pack.label() if pack else "未选（用系统默认发音）"))
        return rows

    def current_voice_pack(self):
        return voice_pack.resolve(getattr(self.config, "voices_dir", ""), self.memory)

    def voice_packs(self):
        return voice_pack.scan(getattr(self.config, "voices_dir", ""))

    def select_voice_pack(self, pack_id):
        """选定语音包并立刻换掉正在用的发声实现。选不上时如实说。"""
        if not voice_pack.select(self.memory, pack_id):
            self._hint("记忆不可用，语音包没选上。")
            return False
        self.rebuild_speech()
        pack = self.current_voice_pack()
        self._hint(f"已选语音包：{pack.label()}" if pack else "已改用系统默认发音。")
        return True

    def import_voice_pack(self, source):
        """导入语音包：只记元数据，不复制也不执行包里的其它文件。"""
        result = voice_pack.import_pack(getattr(self.config, "voices_dir", ""), source)
        if not result.get("ok"):
            self._hint(f"导入失败：{result.get('error')}")
            return result
        self.select_voice_pack(result["pack"].id)
        self._hint(f"已导入语音包：{result['pack'].label()}")
        return result

    def rebuild_speech(self):
        """按当前选中的语音包重建发声实现。失败就保持原来的，不影响聊天。"""
        if self.speech is None:
            return False
        try:
            provider = build_speech(
                self.config, self.logger, pack=self.current_voice_pack()
            )
        except Exception as exc:
            self._hint(f"语音包换不上：{exc}")
            return False
        self.speech.provider = provider
        return True

    def set_listening(self, flag):
        """开始 / 停止监听。没有麦克风、隐私关着，都如实返回 False。"""
        if flag and not self._privacy_allows("listen"):
            self._hint("权限页里「能不能听」是关着的，先去打开。")
            return False
        voice = getattr(self, "voice", None)
        if voice is None:
            self._hint("没有接入语音输入。")
            return False
        if not flag:
            voice.stop()
            if self.mic is not None:
                self.mic.off()
            self._hint("已停止监听，不再采集。")
            return True
        if not voice.start():
            self._hint("没有可用的麦克风，先接上再试。")
            return False
        self._mic_state(bool(getattr(self, "wake", None) is not None and self.wake.armed))
        self._hint("开始监听了。")
        return True

    def set_wake(self, flag):
        """启用 / 停用唤醒词。停用后听到的话直接进大脑，不再等关键词。"""
        wake = getattr(self, "wake", None)
        if wake is None:
            self._hint("没有接入唤醒词。")
            return False
        try:
            wake.arm() if flag else wake.disarm()
        except Exception as exc:
            self._hint(f"唤醒词切换失败：{exc}")
            return False
        if self.mic is not None and getattr(self, "voice", None) is not None:
            if self.voice.listening:
                self._mic_state(bool(flag))
        self._hint("已启用唤醒词。" if flag else "已停用唤醒词。")
        return True

    def _capture_note(self):
        capture = getattr(self, "capture", None)
        if capture is None:
            return "未接入"
        try:
            return str(capture.describe().get("note") or "状态未知")
        except Exception:
            return "状态未知"

    def _stt_note(self):
        """识别引擎现在什么情况：装了没、配没配模型、能不能真用。"""
        stt = getattr(self, "stt", None)
        if stt is None:
            return "未接入"
        try:
            if stt.available():
                name = getattr(getattr(stt, "provider", None), "name", "")
                return f"可用（{name}）" if name else "可用"
        except Exception:
            return "状态未知"
        return self._stt_reason()

    def _stt_reason(self):
        provider = getattr(getattr(self, "stt", None), "provider", None)
        if provider is None:
            return "不可用"
        describe = getattr(provider, "describe", None)
        note = ""
        if describe is not None:
            try:
                note = str((describe() or {}).get("note") or "")
            except Exception:
                note = ""
        return f"不可用（{note}）" if note else "不可用"

    def rebuild_stt(self):
        """按当前配置重挑识别引擎。挑不到就保持不可用，如实说。"""
        if getattr(self, "stt", None) is None:
            return False
        try:
            provider = build_stt(self.config, self.logger)
        except Exception as exc:
            self._hint(f"识别引擎换不上：{exc}")
            return False
        self.stt.provider = provider
        self.stt.on_text = self.submit_voice
        return True

    def record_phrase(self, seconds=None):
        """录一小段话再识别。没麦克风或没识别引擎时如实说明，不假装。"""
        if not self._privacy_allows("listen"):
            self._hint("权限页里「能不能听」是关着的，先去打开。")
            return False
        capture = getattr(self, "capture", None)
        if capture is None or not self._capture_available(capture):
            self._hint(f"现在录不了音：{self._capture_note()}")
            return False
        if not self._stt_ready():
            self._hint("没有能用的识别引擎，录下来的声音变不成文字。")
            return False
        if self._record_busy:
            self._hint("上一段还在录，稍等一下。")
            return False
        self._record_busy = True
        if self.mic is not None:
            self.mic.start_listening()
        self._hint("开始录音，说完自动停。")
        path = default_capture_path(self.config)
        threading.Thread(
            target=self._record_worker, args=(seconds, path), daemon=True
        ).start()
        return True

    def _capture_available(self, capture):
        try:
            return bool(capture.is_available())
        except Exception:
            return False

    def _stt_ready(self):
        try:
            return bool(self.stt is not None and self.stt.available())
        except Exception:
            return False

    def _record_worker(self, seconds, path):
        try:
            result = self.capture.record(seconds=seconds, path=path)
        except Exception as exc:
            result = {"ok": False, "error": f"录音出错了：{exc}"}
        self.post(lambda: self._record_done(result))

    def _record_done(self, result):
        self._record_busy = False
        if self.mic is not None:
            self.mic.off()
        if not result.get("ok"):
            self._hint(f"没录到声音：{result.get('error')}")
            return False
        text = ""
        try:
            text = self.stt.feed(result.get("path") or "") or ""
        except Exception as exc:
            if self.logger:
                self.logger.warning("transcribe failed: %s", exc)
        if not text:
            self._hint(
                f"录到 {result.get('bytes', 0)} 字节，但没听出内容，可以重录一次。"
            )
            return False
        self._hint(f"听到：{text}")
        return True

    def resolve_stt_text(self, path):
        """把一段录音文件交给识别管线。测试和命令行调试都用它。"""
        try:
            return self.stt.feed(path) or ""
        except Exception as exc:
            if self.logger:
                self.logger.warning("transcribe failed: %s", exc)
            return ""

    # ---------- 实时字幕 ----------
    def subtitle_sources(self):
        """当前系统里可选的实时音源。没有 soundcard 时返回空表。"""
        capture = build_stream_capture(self.logger)
        try:
            return capture.sources()
        except Exception as exc:
            if self.logger:
                self.logger.warning("subtitle sources failed: %s", exc)
            return []

    def subtitle_capture_note(self):
        capture = build_stream_capture(self.logger)
        try:
            return str(capture.describe().get("note") or "")
        except Exception:
            return "音频采集状态未知"

    def test_subtitle_source(self, source_id=""):
        """用户主动测试音源，只读很短一段，拿到真实音量后立即停止。"""
        sources = self.subtitle_sources()
        source = next(
            (item for item in sources if str(item.get("id") or "") == str(source_id or "")),
            None,
        )
        if source is None:
            self._hint("先选一个可用音源再测试。")
            return {"ok": False, "error": "没有这个音源"}
        permission = "system_audio" if source.get("kind") == "system" else "listen"
        if not self._privacy_allows(permission):
            self._hint("权限页里对应的音频采集开关还关着，先去打开。")
            return {"ok": False, "error": "没有音频采集权限"}
        capture = build_stream_capture(self.logger)
        try:
            result = capture.probe(source_id, seconds=0.8)
        except Exception as exc:
            result = {"ok": False, "error": "测试音源失败：%s" % exc}
        if result.get("ok"):
            self._hint("音源可用，%s。" % result.get("note"))
        else:
            self._hint(result.get("error") or "音源测试失败。")
        return result

    def subtitle_asr_note(self, model_path="", kind="auto"):
        path = str(model_path or self._preference("subtitle.model") or "").strip()
        return str(self.subtitle_asr_state(path, kind=kind).get("note") or "")

    def subtitle_asr_state(self, model_path="", kind="auto"):
        path = str(model_path or self._preference("subtitle.model") or "").strip()
        try:
            return streaming_model_status(path, kind=kind)
        except Exception:
            return {
                "path": path,
                "state": "probe_failed",
                "ready": False,
                "runtime_ready": False,
                "model_ready": False,
                "note": "识别状态读不出来",
            }

    def subtitle_asr_threads(self):
        try:
            value = int(float(self._preference("subtitle.asr_threads") or 2))
        except (TypeError, ValueError):
            value = 2
        return max(1, min(8, value))

    def subtitle_translation_note(self):
        role = self.subtitle_translation_role()
        if role == "off":
            return "已关闭字幕翻译，只显示原文"
        provider = self._subtitle_translation_provider()
        if provider is None:
            return "未配置%s，只能显示原文" % self.SUBTITLE_TRANSLATION_ROLES[role]
        if not self._subtitle_translation_allowed(provider):
            return "字幕云端翻译权限关着，只能显示原文"
        try:
            available = bool(provider.is_available())
        except Exception:
            available = False
        if not available:
            return "%s不可用，只能显示原文" % self.SUBTITLE_TRANSLATION_ROLES[role]
        model = str(getattr(provider, "model", "") or "")
        label = self.SUBTITLE_TRANSLATION_ROLES[role]
        return "%s可用（%s）" % (label, model) if model else "%s可用" % label

    def subtitle_status_text(self):
        pipeline = getattr(self, "subtitle", None)
        if pipeline is None:
            return "未开始"
        try:
            data = pipeline.describe()
        except Exception:
            return "状态未知"
        if data.get("error"):
            return "需要处理：%s" % data.get("error")
        if data.get("active"):
            state = "已暂停" if data.get("paused") else "运行中"
            return "%s · %s 行字幕" % (state, data.get("segments", 0))
        return "已停止"

    def _sync_subtitle_status_ui(self):
        """设置页开着时，把真实字幕状态同步到它那块标签。"""
        view = (getattr(self, "_panels", None) or {}).get("settings")
        label = getattr(view, "subtitle_status_label", None)
        if label is None:
            return
        try:
            if label.winfo_exists():
                label.configure(text="状态：%s" % self.subtitle_status_text())
        except Exception:
            pass

    def _subtitle_source_name(self, pipeline=None):
        pipeline = pipeline or getattr(self, "subtitle", None)
        capture = getattr(pipeline, "capture", None)
        describe = getattr(capture, "describe", None)
        if not callable(describe):
            return ""
        try:
            return str((describe() or {}).get("source") or "").strip()
        except Exception:
            return ""

    # ---------- 实时字幕：历史与外观 ----------
    SUB_HOTKEY_PREFS = {
        "start_subtitles_from_preferences": "subtitle.hotkey.start",
        "toggle_subtitle_pause": "subtitle.hotkey.pause",
        "stop_subtitles": "subtitle.hotkey.stop",
        "toggle_subtitle_overlay": "subtitle.hotkey.overlay",
    }
    SUBTITLE_TRANSLATION_ROLES = {
        "task": "任务模型",
        "companion": "陪伴模型",
        "off": "关闭翻译",
    }

    def subtitle_history_enabled(self):
        return str(self._preference("subtitle.history") or "0").strip() == "1"

    def subtitle_translation_role(self):
        value = str(self._preference("subtitle.translation_role") or "task").strip()
        return value if value in self.SUBTITLE_TRANSLATION_ROLES else "task"

    def set_subtitle_translation_role(self, role):
        value = str(role or "").strip().lower()
        if value not in self.SUBTITLE_TRANSLATION_ROLES:
            self._hint("不认识这个字幕翻译引擎。")
            return False
        saved = self._save_preference("subtitle.translation_role", value)
        if saved:
            self._hint(
                "字幕翻译已设为%s；正在运行的字幕要先停止再开始。"
                % self.SUBTITLE_TRANSLATION_ROLES[value]
            )
        return bool(saved)

    def check_subtitle_translation(self):
        note = self.subtitle_translation_note()
        self._hint(note)
        return note

    def _subtitle_translation_provider(self):
        role = self.subtitle_translation_role()
        if role == "off":
            return None
        return getattr(getattr(self.agent, role + "_slot", None), "provider", None)

    def set_subtitle_history(self, enabled):
        flag = bool(enabled)
        saved = self._save_preference("subtitle.history", "1" if flag else "0")
        if saved:
            self._hint("字幕历史已开启，只保存最终字幕。" if flag else "字幕历史已关闭。")
        return bool(saved)

    def subtitle_history_rows(self, limit=100):
        return self.subtitle_history.recent(limit=limit)

    def subtitle_history_text(self, limit=100):
        rows = self.subtitle_history_rows(limit=limit)
        if not rows:
            return "还没有保存的字幕。打开「保存字幕历史」后，最终字幕会记在本地。"
        lines = []
        for row in rows:
            source = str(row.get("source_text") or "").strip()
            translated = str(row.get("translated_text") or "").strip()
            start = float(row.get("start_time") or 0.0)
            clock = "%02d:%02d" % (int(start) // 60, int(start) % 60)
            lines.append("[%s] %s" % (clock, source))
            if translated:
                lines.append("      %s" % translated)
        return "\n".join(lines)

    def clear_subtitle_history(self, confirm=False):
        if not confirm:
            self._hint("清空字幕历史要先确认。")
            return 0
        removed = self.subtitle_history.clear(confirm=True)
        self._hint("已清空 %s 条字幕记录。" % removed)
        return removed

    def export_subtitle_history(self, target="", fmt="srt"):
        fmt = str(fmt or "srt").strip().lower()
        if fmt not in SubtitleHistoryRepository.FORMATS:
            fmt = "srt"
        if not target:
            ext = "." + fmt
            target = filedialog.asksaveasfilename(
                parent=self.root,
                title="导出字幕历史",
                defaultextension=ext,
                filetypes=[("字幕文件", "*" + ext), ("所有文件", "*.*")],
            )
        if not target:
            return {"ok": False, "error": "没有选择保存位置"}
        result = self.subtitle_history.export(target, fmt=fmt)
        if result.get("ok"):
            self._hint("字幕已导出：%s" % result.get("path"))
        else:
            self._hint("字幕没导出：%s" % result.get("error"))
        return result

    def hand_subtitle_to_chat(self, action, text):
        """只把用户选中的字幕放进输入框，不自动发送、不写入长期记忆。"""
        body = str(text or "").strip()
        if not body:
            return False
        prompts = {
            "summary": "请总结这段字幕的要点：",
            "keywords": "请提取这段字幕的关键词：",
            "explain": "请解释这段字幕里的术语和背景：",
            "translate": "请翻译这段字幕；原文是中文就译成英文，否则译成中文：",
            "notes": "请根据这段字幕整理一份简短学习笔记：",
        }
        prompt = prompts.get(str(action or ""), "请结合这段字幕回答：")
        quoted = "\n".join("> " + line for line in body.splitlines() if line.strip())
        return bool(self.prefill_chat(prompt + "\n\n" + quoted))

    def start_subtitles_from_preferences(self):
        """热键入口：只用用户已经选好并保存的音源与模型，不猜配置。"""
        source_id = str(self._preference("subtitle.source") or "").strip()
        model_path = str(self._preference("subtitle.model") or "").strip()
        source_language = str(
            self._preference("subtitle.source_language") or "auto"
        ).strip()
        target_language = str(
            self._preference("subtitle.target_language") or ""
        ).strip()
        mode = str(self._preference("subtitle.mode") or "bilingual")
        asr_kind = str(self._preference("subtitle.asr_kind") or "auto")
        if not source_id or not model_path:
            self._hint("开始字幕前要先去设置里选好字幕音源和识别模型。")
            return False
        return self.start_subtitles(
            source_id=source_id,
            model_path=model_path,
            source_language=source_language,
            target_language=target_language,
            mode=mode,
            asr_kind=asr_kind,
            asr_threads=self.subtitle_asr_threads(),
        )

    def reconnect_subtitles(self):
        """按上次真实使用的音源和模型重新建立字幕会话。"""
        if getattr(self, "subtitle", None) is None:
            self._hint("实时字幕还没开始，先去设置里选好音源和模型。")
            return False
        return self.start_subtitles_from_preferences()

    def toggle_subtitle_pause(self):
        pipeline = getattr(self, "subtitle", None)
        if pipeline is None:
            self._hint("实时字幕还没开始。")
            return False
        try:
            paused = bool(pipeline.describe().get("paused"))
        except Exception:
            paused = False
        return self.resume_subtitles() if paused else self.pause_subtitles()

    def toggle_subtitle_overlay(self):
        overlay = getattr(self, "_subtitle_overlay", None)
        if overlay is not None and overlay.visible():
            return self.hide_subtitle_overlay()
        return self.show_subtitle_overlay()

    def subtitle_overlay_settings(self):
        def number(key, default, low, high, cast=float):
            try:
                value = cast(self._preference(key))
            except (TypeError, ValueError):
                value = default
            return max(low, min(high, value))

        color = str(self._preference("subtitle.overlay.text_color") or "theme").strip()
        if color not in ("theme", "contrast", "sky", "cyan", "pink"):
            color = "theme"
        return {
            "font_family": str(
                self._preference("subtitle.overlay.font_family") or theme.FAMILY
            ).strip()
            or theme.FAMILY,
            "font_size": number(
                "subtitle.overlay.font_size",
                max(12, int(theme.FONT_BODY[1]) + 1),
                9,
                30,
                int,
            ),
            "line_spacing": number(
                "subtitle.overlay.line_spacing", 3, 0, 12, int
            ),
            "recent_lines": number("subtitle.overlay.rows", 4, 1, 12, int),
            "text_color": color,
            "auto_scroll": str(
                self._preference("subtitle.overlay.auto_scroll") or "1"
            ).strip()
            == "1",
            "alpha": number("subtitle.overlay.alpha", 0.96, 0.82, 1.0),
            "topmost": str(
                self._preference("subtitle.overlay.topmost") or "1"
            ).strip()
            == "1",
            "click_through": str(
                self._preference("subtitle.overlay.click_through") or "0"
            ).strip()
            == "1",
        }

    def set_subtitle_overlay_option(self, name, value):
        name = str(name or "").strip()
        prefs = {
            "font_family": "subtitle.overlay.font_family",
            "font_size": "subtitle.overlay.font_size",
            "line_spacing": "subtitle.overlay.line_spacing",
            "recent_lines": "subtitle.overlay.rows",
            "text_color": "subtitle.overlay.text_color",
            "auto_scroll": "subtitle.overlay.auto_scroll",
            "alpha": "subtitle.overlay.alpha",
            "topmost": "subtitle.overlay.topmost",
            "click_through": "subtitle.overlay.click_through",
        }
        key = prefs.get(name)
        if key is None:
            return False
        overlay = self._ensure_subtitle_overlay()
        applied = True
        if name == "font_family":
            value = str(value or "").strip()
            if overlay is None or not overlay.set_font_family(value, save=False):
                return False
        elif name == "font_size":
            value = max(9, min(30, int(float(value))))
            if overlay is not None:
                overlay.set_font_size(value, save=False)
        elif name == "line_spacing":
            value = max(0, min(12, int(float(value))))
            if overlay is not None:
                overlay.set_line_spacing(value, save=False)
        elif name == "recent_lines":
            value = max(1, min(12, int(float(value))))
            if overlay is not None:
                overlay.set_recent_lines(value, save=False)
        elif name == "text_color":
            value = str(value or "").strip().lower()
            if overlay is None or not overlay.set_text_color(value, save=False):
                return False
        elif name == "alpha":
            value = max(0.82, min(1.0, float(value)))
            if overlay is not None:
                overlay.set_opacity(value, save=False)
        elif name in ("topmost", "auto_scroll"):
            value = bool(value)
            if overlay is not None:
                if name == "topmost":
                    overlay.set_topmost(value, save=False)
                else:
                    overlay.set_auto_scroll(value, save=False)
        else:
            value = bool(value)
            if overlay is None:
                self._hint("字幕窗口没能建立，点击穿透没有生效。")
                return False
            applied = overlay.set_click_through(value, save=False)
            if not applied:
                return False
        stored = (
            "1" if value is True else "0" if value is False else
            ("%.2f" % value if name == "alpha" else str(value))
        )
        return bool(self._save_preference(key, stored))

    def copy_current_subtitles(self):
        overlay = self._ensure_subtitle_overlay()
        if overlay is None:
            self._hint("字幕窗口没能建立。")
            return False
        return overlay.copy_current()

    def start_subtitles(
        self,
        source_id="",
        model_path="",
        source_language="auto",
        target_language="",
        mode="bilingual",
        asr_kind="auto",
        asr_threads=None,
    ):
        """手动开始实时字幕。默认只留内存，不保存音频。"""
        if getattr(self, "subtitle", None) is not None:
            self.stop_subtitles()
        sources = self.subtitle_sources()
        source = next(
            (item for item in sources if str(item.get("id") or "") == str(source_id or "")),
            None,
        )
        if source is None:
            self._hint("先选一个可用的音源；没有音源时请先安装可选的 soundcard 组件。")
            return False
        permission = "system_audio" if source.get("kind") == "system" else "listen"
        if not self._privacy_allows(permission):
            self._hint("权限页里对应的音频采集开关还关着，先去打开。")
            return False
        path = str(model_path or self._preference("subtitle.model") or "").strip()
        if not path:
            self._hint("先选择在线识别模型目录。")
            return False
        try:
            threads = (
                self.subtitle_asr_threads()
                if asr_threads is None
                else max(1, min(8, int(asr_threads)))
            )
        except (TypeError, ValueError):
            threads = self.subtitle_asr_threads()
        asr = build_streaming_stt(
            path,
            logger=self.logger,
            kind=asr_kind,
            language=source_language,
            num_threads=threads,
        )
        try:
            if not asr.is_available():
                note = str(asr.describe().get("note") or "在线识别模型不可用")
                self._hint(note)
                return False
        except Exception as exc:
            self._hint("在线识别模型不可用：%s" % exc)
            return False
        translator = None
        translation_note = ""
        target = str(target_language or "").strip()
        if target:
            provider = self._subtitle_translation_provider()
            if provider is not None and self._subtitle_translation_allowed(provider):
                candidate = SubtitleTranslator(provider, logger=self.logger)
                if candidate.available():
                    translator = candidate
                else:
                    translation_note = (
                        "%s不可用，只显示原文。"
                        % self.SUBTITLE_TRANSLATION_ROLES[
                            self.subtitle_translation_role()
                        ]
                    )
            else:
                translation_note = self.subtitle_translation_note() + "。"
        capture = build_stream_capture(self.logger)
        pipeline = SubtitlePipeline(
            capture,
            asr,
            translator=translator,
            on_event=self._on_subtitle_segment,
            on_error=self._on_subtitle_error,
            logger=self.logger,
        )
        overlay = self._ensure_subtitle_overlay()
        if overlay is not None:
            overlay.set_mode(mode)
            overlay.show()
            overlay.set_status("正在打开音源")
        if not pipeline.start(source_id, source_language, target):
            note = pipeline.last_error or "实时字幕启动失败"
            if overlay is not None:
                overlay.set_status("未启动")
            self._hint(note)
            return False
        self.subtitle = pipeline
        self._subtitle_mode = str(mode or "bilingual")
        self._save_preference("subtitle.source", str(source_id or ""))
        self._save_preference("subtitle.model", path)
        self._save_preference("subtitle.source_language", str(source_language or "auto"))
        self._save_preference("subtitle.target_language", target)
        self._save_preference("subtitle.mode", self._subtitle_mode)
        self._save_preference("subtitle.asr_kind", str(asr_kind or "auto"))
        self._save_preference("subtitle.asr_threads", str(threads))
        if overlay is not None:
            overlay.set_session(
                self._subtitle_source_name(pipeline), source_language, target
            )
            overlay.set_status("采集中")
        self._sync_subtitle_status_ui()
        self._hint("实时字幕已开始。" + translation_note)
        return True

    def stop_subtitles(self):
        pipeline = getattr(self, "subtitle", None)
        if pipeline is None:
            return False
        try:
            pipeline.stop()
        except Exception as exc:
            if self.logger:
                self.logger.warning("subtitle stop failed: %s", exc)
        self.subtitle = None
        overlay = getattr(self, "_subtitle_overlay", None)
        if overlay is not None:
            overlay.set_status("已停止")
        self._sync_subtitle_status_ui()
        self._hint("实时字幕已停止。")
        return True

    def pause_subtitles(self):
        pipeline = getattr(self, "subtitle", None)
        if pipeline is None:
            self._hint("实时字幕还没开始。")
            return False
        ok = pipeline.pause()
        if ok:
            overlay = getattr(self, "_subtitle_overlay", None)
            if overlay is not None:
                overlay.set_status("已暂停")
            self._sync_subtitle_status_ui()
            self._hint("实时字幕已暂停。")
        else:
            self._hint(pipeline.last_error or "暂时停不下来。")
        return ok

    def resume_subtitles(self):
        pipeline = getattr(self, "subtitle", None)
        if pipeline is None:
            self._hint("实时字幕还没开始。")
            return False
        ok = pipeline.resume()
        if ok:
            overlay = getattr(self, "_subtitle_overlay", None)
            if overlay is not None:
                overlay.set_status("采集中")
            self._sync_subtitle_status_ui()
            self._hint("实时字幕继续了。")
        else:
            self._hint(pipeline.last_error or "暂时恢复不了。")
        return ok

    def set_subtitle_mode(self, mode):
        overlay = self._ensure_subtitle_overlay()
        if overlay is None:
            return False
        ok = overlay.set_mode(mode)
        if ok:
            self._subtitle_mode = str(mode)
            self._save_preference("subtitle.mode", self._subtitle_mode)
        return ok

    def show_subtitle_overlay(self):
        overlay = self._ensure_subtitle_overlay()
        if overlay is None:
            self._hint("字幕窗口没能建立。")
            return False
        overlay.show()
        return True

    def hide_subtitle_overlay(self):
        overlay = getattr(self, "_subtitle_overlay", None)
        if overlay is None:
            return False
        overlay.hide()
        return True

    def _ensure_subtitle_overlay(self):
        overlay = getattr(self, "_subtitle_overlay", None)
        if overlay is not None:
            return overlay
        try:
            from ui.subtitle_overlay import SubtitleOverlay

            overlay = SubtitleOverlay(self, on_hide=self._subtitle_overlay_hidden)
            overlay.set_mode(self._subtitle_mode)
            self._subtitle_overlay = overlay
            return overlay
        except Exception as exc:
            if self.logger:
                self.logger.warning("subtitle overlay failed: %s", exc)
            return None

    def _subtitle_overlay_hidden(self):
        self._hint("字幕窗口已隐藏，采集还在继续。")

    def _on_subtitle_segment(self, segment):
        if getattr(self, "_closed", False):
            return
        self.post(lambda: self._apply_subtitle_segment(segment))

    def _apply_subtitle_segment(self, segment):
        if (
            bool(getattr(segment, "is_final", False))
            and self.subtitle_history_enabled()
        ):
            self.subtitle_history.save(segment)
        overlay = self._ensure_subtitle_overlay()
        if overlay is None:
            return
        overlay.update_segment(segment)
        state = "已识别" if getattr(segment, "is_final", False) else "识别中"
        pipeline = getattr(self, "subtitle", None)
        capture = getattr(pipeline, "capture", None)
        describe = getattr(capture, "describe", None)
        if callable(describe):
            try:
                level = (describe() or {}).get("level") or {}
                if "percent" in level:
                    state += " · 音量 %d%%" % int(level.get("percent") or 0)
            except Exception:
                pass
        overlay.set_status(state)
        self._sync_subtitle_status_ui()

    def _on_subtitle_error(self, message):
        if getattr(self, "_closed", False):
            return
        self.post(lambda: self._apply_subtitle_error(message))

    def _apply_subtitle_error(self, message):
        pipeline = getattr(self, "subtitle", None)
        active = True
        try:
            active = bool(pipeline.describe().get("active")) if pipeline else False
        except Exception:
            active = False
        overlay = getattr(self, "_subtitle_overlay", None)
        if overlay is not None:
            overlay.set_status("识别出错" if active else "已中断 · 点「重新连接」")
        self._sync_subtitle_status_ui()
        self._hint("实时字幕出错：%s" % message)

    def _subtitle_translation_allowed(self, provider):
        base_url = str(getattr(provider, "base_url", "") or "")
        host = endpoint_host(base_url)
        if host in LOCAL_ENDPOINT_HOSTS:
            return True
        return self._privacy_allows("subtitle_cloud")

    # ---------- 角色包 ----------
    def character_package_rows(self):
        """角色包只看真实文件，统计不出就显示 0。"""
        try:
            data = character_store.describe(getattr(self.config, "characters_dir", ""))
        except Exception as exc:
            return [("角色包", f"读取失败：{exc}")]
        return [
            ("角色包数量", str(data.get("count", 0))),
            ("角色包目录", data.get("root") or "未配置"),
            (
                "角色包占用",
                f"{data.get('files', 0)} 个文件 · {human_bytes(data.get('bytes', 0))}",
            ),
        ]

    def create_character_package(self, identifier="", name="", model="", persona=None):
        """新建角色包：只写一份清单，不复制模型，也不下载任何东西。"""
        result = character_store.create_package(
            getattr(self.config, "characters_dir", ""),
            identifier,
            name=name,
            model=model or self._current_model_name(),
            persona=persona,
            live2d_root=getattr(self.config, "live2d_dir", ""),
        )
        if not result.get("ok"):
            self._hint(f"角色包没建成：{result.get('error')}")
            return result
        self.reload_characters()
        self._hint(f"已新建角色包：{result['id']}")
        return result

    def _current_model_name(self):
        provider = self._avatar_provider()
        model = getattr(provider, "model", None) if provider is not None else None
        path = getattr(model, "path", "") if model is not None else ""
        return Path(path).name if path else ""

    def delete_character_package(self, identifier, confirm=False):
        """删角色包。没确认只报影响范围；模型源文件一律不动。"""
        result = character_store.delete_package(
            getattr(self.config, "characters_dir", ""), identifier, confirm=bool(confirm)
        )
        if result.get("ok"):
            self.reload_characters()
            self._hint(f"已删除角色包：{result.get('removed')}")
            return result
        if result.get("needs_confirm"):
            self._hint(f"{result.get('error')}")
            return result
        self._hint(f"没删成：{result.get('error')}")
        return result

    def backup_characters(self, target=""):
        """把角色包的清单类小文件打成一个 zip，模型和密钥都不进去。"""
        result = character_store.backup(
            getattr(self.config, "characters_dir", ""),
            target=target or character_store.default_backup_path(self.config),
        )
        if not result.get("ok"):
            self._hint(f"备份没做成：{result.get('error')}")
            return result
        self._hint(f"已备份 {result['count']} 个文件到 {result['path']}")
        return result

    def restore_characters(self, source, overwrite=False):
        """从备份里恢复角色包。脚本和模型文件一律跳过。"""
        result = character_store.restore(
            getattr(self.config, "characters_dir", ""), source, overwrite=bool(overwrite)
        )
        if not result.get("ok"):
            self._hint(f"恢复没成功：{result.get('error')}")
            return result
        self.reload_characters()
        self._hint(f"已恢复 {result['count']} 个文件")
        return result

    def reload_characters(self):
        """角色包变了就重扫一遍；出问题保持原样，不让角色空着。"""
        return self._rescan_characters()

    # ---------- 人格改动记录 ----------
    def persona_history_rows(self, limit=20):
        return persona_history.rows(self.memory, limit=limit)

    def persona_version(self):
        return persona_history.version(self.memory)

    def record_persona_change(self, before, after, note=""):
        return persona_history.record(self.memory, before, after, note=note)

    def undo_persona_change(self):
        """撤回上一次人格改动，并把改之前的强度重新应用一遍。"""
        result = persona_history.undo(self.memory)
        if not result.get("ok"):
            self._hint(str(result.get("error")))
            return result
        restored = result.get("restored") or {}
        persona = getattr(self.agent, "persona", None)
        setter = getattr(persona, "set_traits", None) if persona is not None else None
        applied = False
        if setter is not None:
            try:
                setter(restored)
                applied = True
            except Exception as exc:
                self._hint(f"撤销后没能应用：{exc}")
        saved = traits_module.save(self.memory, restored)
        if not (applied or saved):
            self._hint("撤销记下来了，但没能生效，重启后会按记录恢复。")
        else:
            self._hint("已撤销上一次人格改动。")
        return result

    # ---------- 开机启动与全局快捷键 ----------
    def startup_enabled(self):
        try:
            return bool(startup_control.is_enabled())
        except Exception:
            return False

    def set_startup(self, flag):
        """开机启动：只有用户明确打开才写注册表，写不进去就如实说。"""
        if not startup_control.describe().get("supported"):
            self._hint("当前系统不支持开机启动。")
            return False
        try:
            startup_control.set_enabled(bool(flag))
        except Exception as exc:
            self._hint(f"开机启动设置失败：{exc}")
            return False
        enabled = self.startup_enabled()
        self._hint("已设为开机启动。" if enabled else "已取消开机启动。")
        return enabled

    def start_tray(self):
        """起系统托盘。装不上只记日志，桌宠照常运行。"""
        if not getattr(self.config, "tray", False):
            return None
        existing = getattr(self, "tray", None)
        if existing is not None and existing.started:
            return existing
        try:
            icon = tray_module.TrayIcon(
                self,
                icon_path=str(resource_path("deskpet.ico")),
                tooltip="桌宠",
                hotkey=self._hotkey_spec(),
                extra_hotkeys=self.subtitle_hotkeys(),
                logger=self.logger,
                dispatch=self._tray_dispatch,
            )
            started = icon.start()
        except Exception as exc:
            if self.logger:
                self.logger.warning("tray unavailable: %s", exc)
            return None
        self.tray = icon
        if not started and self.logger:
            self.logger.info("tray not available, right click menu still works")
        return icon if started else None

    def _tray_dispatch(self, method):
        """托盘菜单命令一律切回主线程执行，托盘线程不直接碰界面控件。"""
        name = str(method or "")
        if name not in self._tray_handlers():
            return False
        self.post(lambda: self._tray_call(name))
        return True

    @staticmethod
    def _tray_handlers():
        return set(tray_module.menu_map().values()) | {
            "toggle_chat",
            "start_subtitles_from_preferences",
            "toggle_subtitle_pause",
            "stop_subtitles",
            "toggle_subtitle_overlay",
        }

    def _tray_call(self, method):
        action = getattr(self, method, None)
        if not callable(action):
            if self.logger:
                self.logger.warning("tray command %s has no handler", method)
            return False
        try:
            action()
        except Exception as exc:
            if self.logger:
                self.logger.warning("tray command %s failed: %s", method, exc)
            return False
        return True

    def _hotkey_spec(self):
        """全局快捷键：界面里存的优先，其次才是配置文件。"""
        saved = self._preference("shell.hotkey")
        if saved is not None:
            return str(saved).strip()
        return str(getattr(self.config, "hotkey", "") or "")

    def subtitle_hotkeys(self):
        return {
            action: str(self._preference(key) or "").strip()
            for action, key in self.SUB_HOTKEY_PREFS.items()
        }

    def apply_subtitle_hotkeys(self, specs):
        """保存并立即重绑字幕快捷键；无效、重复或占用的键明确报错。"""
        clean = {
            action: str(dict(specs or {}).get(action) or "").strip()
            for action in self.SUB_HOTKEY_PREFS
        }
        chat_spec = self._hotkey_spec()
        used = {}
        for label, spec in (("聊天", chat_spec),) + tuple(clean.items()):
            if not spec:
                continue
            combo = tray_module.parse_hotkey(spec)
            if not all(combo):
                self._hint("快捷键「%s」写法看不懂，例：ctrl+alt+d" % spec)
                return False
            if combo in used:
                self._hint(
                    "快捷键「%s」和%s重复了，换一个。" % (spec, used[combo])
                )
                return False
            used[combo] = label
        for action, key in self.SUB_HOTKEY_PREFS.items():
            self._save_preference(key, clean.get(action, ""))
        icon = getattr(self, "tray", None)
        if icon is not None:
            icon.extra_hotkeys = clean
            icon.hotkey = chat_spec
            try:
                icon._register_hotkey()
            except Exception as exc:
                if self.logger:
                    self.logger.warning("subtitle hotkey rebind failed: %s", exc)
        errors = dict(getattr(icon, "hotkey_errors", {}) or {}) if icon else {}
        if errors:
            self._hint(
                "字幕快捷键已保存，但有键没注册上：%s"
                % "；".join("%s（%s）" % item for item in errors.items())
            )
        else:
            self._hint("字幕快捷键已保存。")
        return True

    def _privacy_allows(self, key):
        privacy = getattr(self, "privacy", None)
        if privacy is None:
            return True
        try:
            return bool(privacy.allows(key))
        except Exception:
            return False

    def _mic_text(self, mic):
        if mic is None:
            return "未接入"
        try:
            return str(mic.label())
        except Exception:
            return "状态未知"

    def _voice_input_note(self):
        """现在到底能不能用说话输入。不吹牛：没有识别引擎就直说。"""
        pipeline = getattr(self, "stt", None)
        try:
            ready = bool(pipeline is not None and pipeline.available())
        except Exception:
            ready = False
        if ready:
            return "可以用说话输入"
        if getattr(self, "voice", None) is not None and self._privacy_allows("listen"):
            return "麦克风能开，但没有识别引擎，只能打字"
        return "只能打字（还没接入语音识别）"

    def _provider_note(self, pipeline):
        if pipeline is None:
            return "未接入"
        try:
            if pipeline.available():
                name = getattr(getattr(pipeline, "provider", None), "name", "")
                return f"可用（{name}）" if name else "可用"
        except Exception:
            return "状态未知"
        return "不可用"


    # ---------- 置顶 ----------
    def _stay_on_top(self):
        """overrideredirect 窗口要在映射之后再抬一次，否则会被其他窗口压住。"""
        wanted = self._topmost_wanted()
        try:
            self.root.attributes("-topmost", wanted)
            if wanted:
                self.root.lift()
        except Exception as exc:
            if self.logger:
                self.logger.warning("failed to raise window: %s", exc)
        card = getattr(self, "card", None)
        if card is not None and not getattr(self, "_chat_minimized", False):
            try:
                card.attributes("-topmost", wanted)
                if wanted and self.chat_visible:
                    card.lift()
            except Exception:
                pass

    def _shell_tick(self):
        """定时把桌宠重新抬到最前，并把本进程的窗口从任务栏摘掉。

        别的程序（尤其自己就置顶的）会把桌宠压到下面，压下去鼠标就点不着，
        所以抬窗不能只在启动时做一次。任务栏同理：窗口是按需建的，
        隔一会儿扫一遍最省事。顺便让角色窗口跟桌宠对齐一次。
        """
        if self._topmost_wanted():
            try:
                self.host.keep_on_top()
            except Exception as exc:
                self._warn("keep on top failed: %s", exc)
        try:
            # 只摘掉桌宠自己；聊天工作台要留在任务栏，能最小化再点回来。
            hide_from_taskbar(self.root)
        except Exception as exc:
            self._warn("hide pet from taskbar failed: %s", exc)
        if self.live2d is not None and self._live2d_ready and not self.host.hidden:
            try:
                self.live2d.sync_geometry()
            except Exception as exc:
                self._warn("live2d align failed: %s", exc)
        try:
            self.root.after(self.SHELL_MS, self._shell_tick)
        except Exception:
            pass

    def _hit_tick(self):
        """角色在画却没有碰撞体积时补一个，保证桌宠一直拖得动。"""
        try:
            # 载体隐形的这件事会被系统更新、切显示器、别的程序弄掉，掉了就补，
            # 不然后面那层看不见的碰撞体积会整块显形，在桌面上留一块黑。
            self.host.assert_alpha()
        except Exception as exc:
            self._warn("window transparency check failed: %s", exc)
        try:
            if self.live2d is not None and self._live2d_ready and not self.host.hidden:
                if self.host.has_hit():
                    self._hit_misses = 0
                else:
                    self._hit_misses = int(getattr(self, "_hit_misses", 0)) + 1
                    try:
                        self.live2d.request_hit()
                    except Exception as exc:
                        self._warn("hit request failed: %s", exc)
                    if self._hit_misses >= 2:
                        # 问过一轮还是没有：给中间一块区域接鼠标，别让人拖不动。
                        self.host.set_fallback_hit()
                        self._hit_misses = 0
        except Exception as exc:
            self._warn("hit volume tick failed: %s", exc)
        try:
            self.root.after(self.HIT_MS, self._hit_tick)
        except Exception:
            pass

    def _topmost_wanted(self):
        """置顶默认开；用户关掉之后就不再强制抬窗。"""
        saved = self._preference("shell.topmost")
        if saved is None or saved == "":
            return True
        return str(saved).strip().lower() not in {"0", "false", "no", "off"}

    def topmost_enabled(self):
        return self._topmost_wanted()

    def _append(self, tag, text):
        self.transcript.config(state="normal")
        self.transcript.insert("end", text, tag)
        self.transcript.insert("end", "\n")
        self.transcript.see("end")
        self.transcript.config(state="disabled")

    def _append_message(self, speaker, text, tag):
        """桌宠和用户的正文走 Markdown 渲染，围栏代码块单独成块。"""
        body = f"{speaker}：{text}"
        if tag in ("me", "pet") and self.chat.append_markdown(tag, body):
            return body
        self._append(tag, body)
        return body

    def look_at_screen(self, region=None):
        """用户主动点一次才截一张屏。授权、落盘与失败原因都由视觉服务说了算。"""
        vision = getattr(self, "vision", None)
        capture = getattr(vision, "capture", None)
        if capture is None:
            self._append("notice", "这一版还没有接屏幕截图。")
            return {"ok": False, "error": "没有视觉服务"}
        try:
            result = capture(region=region, reason="menu")
        except Exception as exc:
            self._warn("screen capture raised: %s", exc)
            result = {"ok": False, "error": str(exc)}
        result = result or {}
        data = result.get("data") or {}
        if result.get("ok"):
            self._append(
                "notice",
                "看了一眼：%s×%s 的屏幕图存到 %s。图只在本地，没有上传。"
                % (data.get("width"), data.get("height"), data.get("path")),
            )
        else:
            self._append("notice", "看不了：%s" % result.get("error", "原因不明"))
        return result

    # 任务只在值得看的那几步留一行，普通回复不刷任务状态。
    TASK_NOTICE_STATES = (
        TaskState.PENDING_CONFIRMATION.value,
        TaskState.WAITING_USER.value,
        TaskState.COMPLETED.value,
        TaskState.FAILED.value,
        TaskState.CANCELLED.value,
    )

    def _append_task(self, text):
        """任务走到哪一步单独占一行，不和普通聊天混在一起。"""
        self.show_chat()
        self._append_message("任务", text, "task")
        return text

    def _note_task_state(self, debug):
        """显示这一轮真实的任务状态；没有任务就不出声。"""
        if not debug:
            return None
        state = debug.get("task_state")
        if state not in self.TASK_NOTICE_STATES:
            return None
        note = TASK_STATE_LABELS.get(state, state)
        if state == TaskState.PENDING_CONFIRMATION.value:
            note = f"{note}，在弹窗里点允许或拒绝"
        self._sync_task_panel(state)
        return self._append_task(note)

    def _task_in_flight(self):
        """任务在跑或等确认时，才值得在取消后说一声。"""
        state = getattr(self.agent, "task_state", TaskState.NEW.value)
        return state not in (
            TaskState.NEW.value,
            TaskState.COMPLETED.value,
            TaskState.FAILED.value,
            TaskState.CANCELLED.value,
        )

    def _send(self, event=None):
        text = self.chat.read_input()
        if not text or self._busy:
            return "break"
        if text.startswith("/"):
            self._chat_command(text[1:].strip())
            self.chat.clear_input()
            return "break"
        self.chat.clear_input()
        self._last_user = text
        self.last_reply = None
        self._append_message("你", text, "me")
        self._set_busy(True)
        token = self._next_reply_token()
        self._new_worker(self._worker_reply, text, token)
        return "break"

    def _new_worker(self, target, *args):
        """起一个后台线程并记下来，退出时好等它收尾。"""
        self._workers[:] = [item for item in self._workers if item.is_alive()]
        thread = threading.Thread(target=target, args=args, daemon=True)
        self._workers.append(thread)
        thread.start()
        return thread

    def _join_workers(self, timeout=2.0):
        """等在跑的回复线程结束。超时就不再等，绝不卡住界面。"""
        for thread in list(self._workers):
            if thread.is_alive():
                thread.join(timeout)
        self._workers = []

    def _next_reply_token(self):
        """开新一轮就换号，上一轮的迟到回复会被丢掉。"""
        self._reply_token = int(getattr(self, "_reply_token", 0)) + 1
        self._stream_open = False
        return self._reply_token

    def receive_speech(self, text):
        """听到的一句话。开着唤醒词就先等关键词，没开就直接回。"""
        text = (text or "").strip()
        if not text:
            return None
        if self.wake is not None and self.wake.armed:
            word, rest = self.wake.feed(text)
            if not word or not rest:
                return None
            text = rest
        if self.mic is not None:
            self.mic.start_processing()
        return self.submit_voice(text)

    def submit_voice(self, text):
        """语音识别出的文字走打字同一条通道，模型不在主线程里跑。"""
        text = (text or "").strip()
        if not text or self._busy:
            return None
        self._append_message("你（语音）", text, "me")
        self._set_busy(True)
        self._new_worker(self._worker_reply, text)
        return text

    def _voice_tick(self):
        """只有真的有语音包时才走到这里；没接麦克风时不会排这个定时。"""
        if self.voice is None:
            return
        try:
            self.voice.poll()
        except Exception as exc:
            if self.logger:
                self.logger.warning("voice poll failed: %s", exc)
        self.root.after(self.VOICE_MS, self._voice_tick)

    def _mic_state(self, awaiting_wake):
        """把「有没有在听」同步到状态机：等唤醒词，或一直在听。"""
        if self.mic is None:
            return False
        return self.mic.await_wake() if awaiting_wake else self.mic.start_listening()

    def _worker_reply(self, text, token=None):
        """模型调用在工作线程里跑，逐段结果丢回主线程，界面不卡。"""
        try:
            reply = self.agent.respond(
                self.session_id, text, on_delta=self._stream_sink(token)
            )
        except Exception as exc:
            if self.logger:
                self.logger.warning("agent respond failed: %s", exc)
            self.post(lambda: self._show_error(f"对话失败：{exc}"))
            return
        self.post(lambda: self._apply_reply(reply, token))

    def _stream_sink(self, token=None):
        """给 Agent Core 的逐段回调：内容丢回主线程再画。"""

        def sink(chunk):
            self.post(lambda: self._stream_chunk(chunk, token))

        return sink

    def _stream_chunk(self, chunk, token=None):
        if token is not None and token != self._reply_token:
            return None
        if not self._stream_open:
            if not self.chat.begin_stream():
                return None
            self._stream_open = True
        return self.chat.push_stream(chunk)

    def _close_stream(self, body=None):
        if not self._stream_open:
            return False
        self._stream_open = False
        return self.chat.end_stream(body)

    def _apply_reply(self, reply, token=None):
        # 已经被取消或用户又发了一轮：这条回复过期了，直接丢掉。
        if token is not None and token != self._reply_token:
            return False
        self._set_busy(False)
        self.show_chat()
        self.last_reply = reply
        if reply.ok:
            self.chat.set_state(chat_view.OK, "已完成")
            self.chat.show_retry(False)
            if self._close_stream("桌宠：" + (reply.content or "（没有内容）")):
                pass
            else:
                self._append_message("桌宠", reply.content or "（没有内容）", "pet")
            self._say(reply.content)
        else:
            self.chat.set_state(chat_view.ERROR, "出错了，可以重试")
            self.chat.show_retry(True)
            self._append_message("桌宠", reply.error, "err")
            hint = self._no_model_hint(reply)
            if hint:
                self._append("notice", hint)
        self._note_task_state(reply.debug)
        self._refresh_status(reply)
        if self.dev is not None and reply.debug:
            self._set_dev(reply.debug)

    def _say(self, text):
        """把要说的话交给语音包。没有语音包、或者静默中，都安静跳过。"""
        if self.speech is None or not text:
            return False
        if self.quiet is not None and not self.quiet.allows_speech():
            return False
        try:
            emotion = self.avatar.state.get("emotion")
            return self.speech.say(text, emotion=emotion)
        except Exception as exc:
            if self.logger:
                self.logger.warning("speech failed: %s", exc)
            return False

    def _no_model_hint(self, reply):
        """模型没配好时，把能力边界摆出来，给用户能用的替代操作。"""
        debug = reply.debug if isinstance(reply.debug, dict) else {}
        error_type = debug.get("error_type")
        if error_type not in ("COMPANION_MODEL_UNAVAILABLE", "TASK_MODEL_UNAVAILABLE"):
            return ""
        caps = getattr(self.agent, "capabilities", None)
        if caps is None:
            return ""
        try:
            return str(caps.boundary(short=True) or "").strip()
        except Exception as exc:
            self._warn("capability boundary failed: %s", exc)
            return ""

    def _show_error(self, message):
        self._set_busy(False)
        self.chat.set_state(chat_view.ERROR, "出错了")
        self.chat.show_retry(True)
        self._append_message("桌宠", message, "err")

    def _cancel(self):
        """用户点取消：正在生成的这一轮停住，任务也一起收尾。"""
        if not self._busy and self.last_reply is None and not self._task_in_flight():
            return False
        # 换号即作废：工作线程稍后回来的那条回复不会再画到界面上。
        self._reply_token = int(getattr(self, "_reply_token", 0)) + 1
        stop = getattr(self.agent, "cancel_generation", None)
        if callable(stop):
            try:
                stop()
            except Exception as exc:
                if self.logger:
                    self.logger.warning("cancel generation failed: %s", exc)
        self._close_stream(None)
        # 取消会把任务状态改成已结束，所以先记住它是不是真在跑。
        was_running = self._task_in_flight()
        try:
            cancel = getattr(self.agent, "cancel_task", None)
            if cancel is not None:
                cancel()
        except Exception as exc:
            if self.logger:
                self.logger.warning("cancel failed: %s", exc)
        self._set_busy(False)
        self.chat.set_state(chat_view.WARN, "已取消")
        self.chat.show_retry(True)
        if was_running:
            self._append_task("已取消")
        return True

    def _retry(self):
        """重新生成：把上一句话再发一次。"""
        text = self._last_user
        if not text or self._busy:
            return "break"
        self.chat.clear_input()
        self.chat.input.insert(0, text)
        return self._send()

    def _clear_chat(self):
        """清空当前对话前先问一声，避免误删。"""
        if not messagebox.askyesno(
            "清空对话", "确定要清空当前对话吗？", parent=self.card
        ):
            return False
        self.chat.clear()
        self.last_reply = None
        return True

    def _set_busy(self, busy):
        self._busy = bool(busy)
        self.chat.set_busy(self._busy)
        if self._busy:
            self.chat.set_state(chat_view.BUSY, "思考中……")
        else:
            self.chat.show_retry(False)
        return self._busy

    # ---------- 主动陪伴 ----------
    def on_companion(self, event):
        self._new_worker(self._worker_proactive, event)

    def _worker_proactive(self, event):
        try:
            reply = self.agent.respond_to_event(event, session_id=self.session_id)
        except Exception as exc:
            self.post(lambda: self._show_error(f"主动提醒失败：{exc}"))
            return
        self.post(lambda: self._apply_proactive(reply))

    def _apply_proactive(self, reply):
        if reply.ok and reply.content:
            self.show_chat()
            self._append_message("桌宠（主动）", reply.content, "proactive")
            self._refresh_status(reply)
            self._say(reply.content)
        elif not reply.ok:
            self._append_message("桌宠（主动）", reply.error, "err")
        if self.dev is not None and reply.debug:
            self._set_dev(reply.debug)

    # ---------- 独立提醒卡 ----------
    def on_reminder(self, event):
        payload = dict(getattr(event, "payload", None) or {})
        self.post(lambda: self._show_reminder(payload))

    def _reminder_key(self, payload):
        return (
            str(payload.get("kind") or "calendar"),
            str(payload.get("event_id") or ""),
            str(payload.get("occurrence_key") or ""),
            str(payload.get("remind_at_epoch") or ""),
        )

    def _show_reminder(self, payload):
        key = self._reminder_key(payload)
        if key in self._reminder_seen:
            return None
        self._reminder_seen.add(key)
        self._reminder_popups = {
            name: popup
            for name, popup in self._reminder_popups.items()
            if not getattr(popup, "closed", True)
        }
        from ui.notification import ReminderPopup

        popup = ReminderPopup(self, payload, offset=len(self._reminder_popups))
        self._reminder_popups[key] = popup
        return popup

    def handle_reminder_action(self, action, payload):
        action = str(action or "").strip().lower()
        kind = str(payload.get("kind") or "calendar")
        event_id = payload.get("event_id")
        reminder = getattr(self.agent, "calendar_reminder", None)
        if action == "ignore":
            self._hint("这条提醒先忽略。")
            return False
        if action == "open":
            return self._open_reminder_target(kind, event_id)
        if kind == "todo":
            return self._handle_todo_reminder(action, event_id, payload)
        calendar = getattr(getattr(self.agent, "capabilities", None), "calendar", None)
        if calendar is None:
            self._hint("日历服务没有接上。")
            return False
        if action == "complete":
            result = calendar.complete_event(event_id)
            if callable(getattr(reminder, "forget_event", None)):
                reminder.forget_event(event_id)
            self._hint("日程已完成。" if result.get("ok") else str(result.get("error") or "没完成"))
            return bool(result.get("ok"))
        if action == "delay":
            result = calendar.delay_event(event_id, 15)
            if not result.get("ok"):
                self._hint(str(result.get("error") or "延期失败"))
                return False
            if reminder is not None:
                reminder.forget_event(event_id)
                reminder.schedule_event(event_id)
            self._hint("日程已顺延 15 分钟。")
            return True
        if action == "snooze":
            if self.scheduler is None:
                self._hint("调度器没有接上，稍后提醒排不了。")
                return False
            next_payload = dict(payload)
            next_payload.pop("job_id", None)
            next_payload.pop("job_name", None)
            next_payload["remind_at_epoch"] = time.time() + 600
            next_payload["snoozed"] = True
            self.scheduler.schedule_once(
                600,
                event=bus_module.REMINDER,
                name="稍后提醒：%s" % (payload.get("title") or ""),
                idempotency_key="snooze:calendar:%s:%s"
                % (event_id, int(time.time() * 1000)),
                **next_payload
            )
            self._hint("10 分钟后再提醒你。")
            return True
        return False

    def _handle_todo_reminder(self, action, todo_id, payload):
        todos = getattr(self.agent, "todo_store", None)
        if todos is None:
            self._hint("待办库没有接上。")
            return False
        if action == "complete":
            result = todos.done(todo_id)
            self._hint("待办已完成。" if result.get("ok") else str(result.get("error") or "没完成"))
            return bool(result.get("ok"))
        if action == "delay":
            postpone = getattr(todos, "postpone", None)
            if not callable(postpone):
                self._hint("这一版还不能延期待办。")
                return False
            result = postpone(todo_id, 15)
            reminder = getattr(self.agent, "todo_reminder", None)
            if result.get("ok") and reminder is not None:
                reminder.schedule_todo(todo_id)
            self._hint("待办顺延 15 分钟。" if result.get("ok") else str(result.get("error") or "延期失败"))
            return bool(result.get("ok"))
        if action == "snooze":
            if self.scheduler is None:
                return False
            next_payload = dict(payload)
            next_payload.pop("job_id", None)
            next_payload.pop("job_name", None)
            next_payload["remind_at_epoch"] = time.time() + 600
            self.scheduler.schedule_once(
                600,
                event=bus_module.REMINDER,
                name="稍后提醒：%s" % (payload.get("title") or ""),
                idempotency_key="snooze:todo:%s:%s" % (todo_id, int(time.time() * 1000)),
                **next_payload
            )
            self._hint("10 分钟后再提醒你。")
            return True
        return False

    def _open_reminder_target(self, kind, event_id):
        if kind == "todo":
            self.show_chat()
            self.open_tasks()
            return True
        panel = self.open_calendar()
        calendar = getattr(getattr(self.agent, "capabilities", None), "calendar", None)
        event = calendar.event(event_id) if calendar is not None else None
        if panel is not None and event is not None:
            try:
                panel.year = event["start_dt"].year
                panel.month = event["start_dt"].month
                panel.selected = event["start_dt"].date()
                panel.refresh()
            except Exception as exc:
                self._warn("calendar focus failed: %s", exc)
        return panel is not None

    # ---------- 状态 ----------
    def _refresh_status(self, reply=None):
        """状态行只写一行短句；完整的模型、任务和 token 走 _status_full_text()。"""
        emotion = self.avatar.state.get("emotion", "neutral")
        mode_name = getattr(self.agent, "mode_name", "")
        try:
            self.status.config(
                text=format_status_short(
                    emotion,
                    MODE_LABELS.get(mode_name, mode_name),
                    mic=self.mic.label() if self.mic is not None else "",
                    quiet=self._quiet_enabled(),
                    working=bool(self.worklog and self.worklog.active()),
                )
            )
        except Exception as exc:
            self._warn("status line failed: %s", exc)
        self._chat_status_labels()

    def _status_full_text(self):
        """老写法的那一整行，现在只在详情里出现。"""
        totals = None
        if self.memory is not None:
            try:
                totals = self.memory.usage_totals()
            except Exception:
                totals = None
        agent = self.agent
        mode_name = getattr(agent, "mode_name", "")
        companion = getattr(agent, "companion_slot", None)
        task = getattr(agent, "task_slot", None)
        return format_status(
            self.avatar.state.get("emotion", "neutral"),
            totals,
            getattr(self, "last_reply", None),
            companion.model if companion is not None else self.config.companion_model,
            MODE_LABELS.get(mode_name, mode_name),
            task.label if task is not None else "未配置",
            mic=self.mic.label() if self.mic is not None else "",
            quiet=self._quiet_enabled(),
        )

    def _show_detail_window(self, title, body):
        """一个只读的小窗，专给「详情」用。"""
        win = tk.Toplevel(self.root)
        win.title("桌宠 · %s" % title)
        win.configure(bg=theme.BG)
        try:
            win.attributes("-topmost", True)
        except Exception:
            pass
        width, height = 520, 480
        try:
            x, y = self.host.card_position(width, height)
        except Exception as exc:
            self._warn("detail window position failed: %s", exc)
            x = y = 120
        win.geometry("%dx%d+%d+%d" % (width, height, x, y))
        theme.label(win, title, kind="title").pack(
            anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_MD, theme.PAD_SM)
        )
        area = theme.text_area(win, height=16, width=1)
        area.pack(fill="both", expand=True, padx=theme.PAD_MD)
        area.config(state="normal")
        area.insert("1.0", body)
        area.config(state="disabled")
        theme.button(win, "关闭", win.destroy, kind="primary").pack(
            anchor="e", padx=theme.PAD_MD, pady=theme.PAD_MD
        )
        return win

    def _set_dev(self, debug):
        text = format_debug(debug)
        totals = None
        if self.memory is not None:
            try:
                totals = self.memory.usage_totals()
            except Exception:
                totals = None
        if totals:
            text += "\n累计 Token: 输入 %s / 输出 %s / 合计 %s" % (
                totals.get("prompt_tokens", 0),
                totals.get("completion_tokens", 0),
                totals.get("total_tokens", 0),
            )
        if self.behavior is not None:
            try:
                text += "\n行为: " + json.dumps(
                    self.behavior.describe(), ensure_ascii=False
                )
            except Exception:
                pass
        if self.voice is not None:
            try:
                text += "\n语音输入: " + json.dumps(
                    self.voice.describe(), ensure_ascii=False
                )
            except Exception:
                pass
        if self.speech is not None:
            try:
                text += "\n语音输出: " + json.dumps(
                    self.speech.describe(), ensure_ascii=False
                )
            except Exception:
                pass
        if self.stt is not None:
            try:
                text += "\n语音识别: " + json.dumps(
                    self.stt.describe(), ensure_ascii=False
                )
            except Exception:
                pass
        if self.wake is not None:
            try:
                text += "\n唤醒词: " + json.dumps(
                    self.wake.describe(), ensure_ascii=False
                )
            except Exception:
                pass
        if self.mic is not None:
            try:
                text += "\n麦克风: " + json.dumps(
                    self.mic.describe(), ensure_ascii=False
                )
            except Exception:
                pass
        if self.activity is not None:
            try:
                text += "\n活动感知: " + json.dumps(
                    self.activity.describe(), ensure_ascii=False
                )
            except Exception:
                pass
        if self.focus is not None:
            try:
                text += "\n专注模式: " + json.dumps(
                    self.focus.describe(), ensure_ascii=False
                )
            except Exception:
                pass
        if self.quiet is not None:
            try:
                text += "\n静默模式: " + json.dumps(
                    self.quiet.describe(), ensure_ascii=False
                )
            except Exception:
                pass
        if self.environment is not None:
            try:
                text += "\n环境感知: " + json.dumps(
                    self.environment.describe(), ensure_ascii=False
                )
            except Exception:
                pass
        if self.screen is not None:
            try:
                text += "\n屏幕感知: " + json.dumps(
                    self.screen.describe(), ensure_ascii=False
                )
            except Exception:
                pass
        if self.vision is not None:
            try:
                text += "\n视觉: " + json.dumps(
                    self.vision.describe(), ensure_ascii=False
                )
            except Exception:
                pass
        if self.detection is not None:
            try:
                text += "\n视觉检测: " + json.dumps(
                    self.detection.describe(), ensure_ascii=False
                )
            except Exception:
                pass
        if self.body is not None:
            try:
                text += "\n实体身体: " + json.dumps(
                    self.body.describe(), ensure_ascii=False
                )
            except Exception:
                pass
        devices = getattr(self.agent, "devices", None)
        if devices is not None and callable(getattr(devices, "describe", None)):
            try:
                text += "\n设备: " + json.dumps(
                    devices.describe(), ensure_ascii=False
                )
            except Exception:
                pass
        if getattr(self, "privacy", None) is not None:
            try:
                text += "\n隐私中心: " + json.dumps(
                    self.privacy.describe(), ensure_ascii=False
                )
                text += "\n权限中心: " + json.dumps(
                    self.privacy.permission_center(), ensure_ascii=False
                )
            except Exception:
                pass
        pets = getattr(getattr(self.agent, "capabilities", None), "pets", None)
        if pets is not None:
            try:
                text += "\n宠物: " + json.dumps(pets.describe(), ensure_ascii=False)
            except Exception:
                pass
        if self.memory is not None:
            try:
                text += "\n关系: " + json.dumps(
                    self.memory.relationship.describe(), ensure_ascii=False
                )
            except Exception:
                pass
        if self.memory is not None:
            try:
                text += "\n日记: " + json.dumps(
                    self.memory.diary.describe(), ensure_ascii=False
                )
            except Exception:
                pass
        if self.memory is not None:
            try:
                text += "\n习惯: " + json.dumps(
                    self.memory.habit.describe(), ensure_ascii=False
                )
            except Exception:
                pass
        if self.memory is not None:
            try:
                text += "\n时间线: " + json.dumps(
                    self.memory.timeline.describe(), ensure_ascii=False
                )
            except Exception:
                pass
        avatar = getattr(self.agent, "avatar", None)
        if avatar is not None:
            try:
                text += "\n形象: " + json.dumps(
                    avatar.describe()
                    if callable(getattr(avatar, "describe", None))
                    else avatar.snapshot(),
                    ensure_ascii=False,
                )
            except Exception:
                pass
        persona = getattr(self.agent, "persona", None)
        if persona is not None:
            try:
                text += "\n角色包: " + json.dumps(
                    persona.describe(), ensure_ascii=False
                )
            except Exception:
                pass
        training = getattr(self.agent, "training", None)
        if training is not None:
            try:
                text += "\n人格样本: " + json.dumps(training.status(), ensure_ascii=False)
            except Exception:
                pass
        self.dev.config(state="normal")
        self.dev.delete("1.0", "end")
        self.dev.insert("end", text)
        self.dev.config(state="disabled")

    # ---------- 权限确认 ----------
    def confirm(self, tool_name, level, reason):
        """在工作线程里等主线程给答案。聊天窗口开着就先在聊天里问一次。"""
        if not self.chat_visible:
            return self._confirm_dialog(tool_name, level, reason)
        done = threading.Event()
        holder = {"ok": False, "event": done}
        self._permission_wait = holder
        self.post(lambda: self.chat.show_permission(tool_name, level, reason))
        answered = done.wait(timeout=180)
        self._permission_wait = None
        self.post(self.chat.clear_permission)
        if not answered:
            return False
        return bool(holder.get("ok"))

    def _confirm_dialog(self, tool_name, level, reason):
        """工作线程里等主线程弹确认框。权限判断由程序层说了算，界面只负责问。"""
        done = threading.Event()
        answer = {}
        # 先挂到右侧面板上：用户在别的页面也能看见"现在在等什么点头"。
        self._show_pending_authorization(tool_name, level, reason)

        def ask():
            try:
                answer["ok"] = messagebox.askyesno(
                    "权限确认",
                    f"桌宠请求执行 {tool_name}（等级 {level}）\n{reason}\n\n允许吗？",
                    parent=self.root,
                )
            finally:
                done.set()

        self.post(ask)
        done.wait(timeout=self.CONFIRM_TIMEOUT_SECONDS)
        self._clear_pending_authorization()
        return bool(answer.get("ok"))

    # ---------- 授权请求 ----------
    CONFIRM_TIMEOUT_SECONDS = 180

    def _show_pending_authorization(self, tool_name, level, reason):
        """把待授权的请求写进右侧面板，并把它展开。问完就撤掉。"""
        # 先记住面板原来写的是什么，问完之后要原样还回去。
        self._panel_before_authorization = str(getattr(self.chat, "_panel_cache", "") or "")
        self._pending_authorization = {
            "tool": str(tool_name or ""),
            "level": str(level or ""),
            "reason": str(reason or ""),
        }
        self._paint_pending_authorization()
        try:
            if not self.chat.panel_is_shown():
                self.chat.set_panel(True)
        except Exception as exc:
            self._warn("authorization panel failed: %s", exc)
        return self._pending_authorization

    def _clear_pending_authorization(self):
        self._pending_authorization = None
        try:
            self.chat.set_panel_text(getattr(self, "_panel_before_authorization", "") or "")
        except Exception as exc:
            self._warn("authorization restore failed: %s", exc)
        return None

    def _paint_pending_authorization(self):
        """面板上的那一块。没有待授权的请求就还原本来的内容，不占地方。"""
        pending = getattr(self, "_pending_authorization", None)
        try:
            if not pending:
                return None
            lines = [
                "等待你的授权",
                "",
                "动作：%s" % pending.get("tool"),
                "等级：%s" % pending.get("level"),
                "原因：%s" % pending.get("reason"),
                "",
                "确认框已经弹出来了。不点就是不允许，超时会自动当拒绝。",
            ]
            self.chat.set_panel_text("\n".join(lines))
        except Exception as exc:
            self._warn("authorization paint failed: %s", exc)
        return pending

    # ---------- 设置 ----------
    def _set_window_icon(self):
        icon = resource_path("deskpet.ico")
        if not icon.is_file():
            return
        try:
            self.root.iconbitmap(default=str(icon))
        except Exception as exc:
            if self.logger:
                self.logger.debug("window icon failed: %s", exc)

    def settings_rows(self):
        config = self.config
        repo = getattr(config, "model_configs", None)
        store = getattr(config, "secrets", None)
        default_companion = repo.default("companion") if repo is not None else None
        default_task = repo.default("task") if repo is not None else None
        stored_keys = False
        if repo is not None and store is not None:
            try:
                stored_keys = any(store.has(row.get("secret_id")) for row in repo.list())
            except Exception:
                stored_keys = False
        rows = [
            ("陪伴模型", (default_companion or {}).get("model") or config.companion_model or "未配置"),
            ("任务模型", (default_task or {}).get("model") or config.task_model or "未配置"),
            (
                "接口地址",
                (default_companion or {}).get("base_url")
                or config.companion_base_url
                or "未配置",
            ),
            (
                "API Key",
                "已设置"
                if (stored_keys or config.companion_api_key or config.llm_api_key)
                else "未设置",
            ),
            ("上下文预算", f"{config.context_budget} tokens"),
            ("最近窗口", f"{config.recent_window} 条"),
            ("每轮检索", f"{config.memory_limit} 条"),
            ("记忆容量", f"{getattr(config, 'memory_capacity', 0)} 条（超出只归档不删除）"),
            ("摘要阈值", f"{config.summary_trigger} 条"),
            ("主动提醒", "开启" if config.proactive else "关闭"),
            ("主动冷却", f"{getattr(config, 'proactive_cooldown', 0) or '不限'} 秒"),
            ("每日上限", f"{getattr(config, 'proactive_max_per_day', 0) or '按规则'} 次"),
            ("界面主题", self._theme_row()),
            ("人格", self._persona_row()),
            ("数据库", str(config.db_path)),
            ("日志", str(config.log_dir / "deskpet.log")),
        ]
        if self.memory is not None:
            try:
                rows.append(("累计 Token", str(self.memory.usage_totals().get("total_tokens", 0))))
            except Exception:
                pass
        return rows

    def _persona_row(self):
        persona = getattr(self.agent, "persona", None)
        if persona is None:
            return "未接入"
        try:
            profile = persona.get_profile() or {}
        except Exception:
            return "读取失败"
        name = str(profile.get("name") or "").strip() or "未命名"
        traits = [
            f"{key} {value}"
            for key, value in sorted(
                (profile.get("traits") or {}).items(), key=lambda item: -item[1]
            )
        ]
        return f"{name}（{'、'.join(traits) if traits else '未设置性格'}）"

    def persona_summary(self):
        """设置页用的一行人格状态。人格设定、即时情绪、关系和模式分开写。"""
        rows = [("人格", self._persona_row())]
        emotion = getattr(self.agent, "emotion", None)
        if emotion is not None:
            try:
                data = emotion.to_dict()
                rows.append(("即时情绪", f"{data.get('emotion')}（{data.get('intensity')}）"))
            except Exception:
                rows.append(("即时情绪", "读取失败"))
        if self.memory is not None:
            try:
                data = self.memory.relationship.describe()
                rows.append(
                    (
                        "关系",
                        f"认识 {data.get('days')} 天，聊过 {data.get('interactions')} 次，"
                        f"算{data.get('stage')}",
                    )
                )
            except Exception:
                rows.append(("关系", "读取失败"))
        rows.append(("行为模式", self._mode_row()))
        return rows

    def proactive_number(self, key, default=0):
        """读一个主动提醒相关的数字偏好，读不到就用配置默认值。"""
        raw = self._preference(key)
        if raw is None or raw == "":
            return default
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return default

    def _theme_row(self):
        stored = self._preference(theme.THEME_PREFERENCE) or "dark"
        return "%s（当前 %s）" % (stored, theme.active())

    def _accent_row(self):
        stored = theme.accent()
        return "%s（当前 %s）" % (theme.accent_label(stored), stored)

    # ---------- 液态玻璃：背景与主题 ----------
    def _create_background(self):
        old = getattr(self, "background", None)
        if old is not None:
            try:
                old.stop()
            except Exception:
                pass
        try:
            self.background = glass.BackgroundLayer(
                self.card, options=self._background_options, logger=self.logger
            )
            from ui import glass_native

            glass_native.set_background_layer(self.background)
        except Exception as exc:
            self.background = None
            self._warn("background layer failed: %s", exc)

    def _background_options(self):
        def read(key, default=""):
            value = self._preference(key)
            return default if value in (None, "") else value

        def number(key, default, low, high):
            try:
                value = float(read(key, default))
            except (TypeError, ValueError):
                value = float(default)
            return max(low, min(high, value))

        try:
            zoom = float(read(theme.BACKGROUND_ZOOM_PREFERENCE, 1.0) or 1.0)
        except (TypeError, ValueError):
            zoom = 1.0
        dim_raw = self._preference(theme.BACKGROUND_DIM_PREFERENCE)
        try:
            dim = None if dim_raw in (None, "") else max(0.0, min(0.7, float(dim_raw)))
        except (TypeError, ValueError):
            dim = None
        return {
            "image": str(read(theme.BACKGROUND_PREFERENCE, "")),
            "zoom": max(1.0, min(2.5, zoom)),
            "x": number(theme.BACKGROUND_X_PREFERENCE, 0.5, 0.0, 1.0),
            "y": number(theme.BACKGROUND_Y_PREFERENCE, 0.5, 0.0, 1.0),
            "blur": int(number(theme.BACKGROUND_BLUR_PREFERENCE, 10, 0, 30)),
            "dim": dim,
            "motion": str(read(theme.BG_MOTION_PREFERENCE, "off")),
            "preset": theme.preset(),
            "mode": theme.active(),
        }

    def background_info(self):
        """设置页要的一行状态：用的是哪张图、还在不在、动效是什么。"""
        options = self._background_options()
        path = options["image"]
        info = {
            "path": path,
            "exists": bool(path) and Path(path).is_file(),
            "zoom": options["zoom"],
            "x": options["x"],
            "y": options["y"],
            "blur": options["blur"],
            "dim": options["dim"]
            if options["dim"] is not None
            else (0.25 if theme.active() == "dark" else 0.08),
            "motion": options["motion"],
            "error": "",
        }
        layer = getattr(self, "background", None)
        if layer is not None:
            try:
                info["error"] = layer.last_error()
            except Exception:
                pass
        if path and not info["exists"]:
            info["error"] = "图片不在了，已退回渐变背景"
        return info

    def set_background(self, path):
        """用本机图片当背景。只记路径，不复制、不改原文件。"""
        result = glass.inspect_image(path)
        if not result.get("ok"):
            return result
        self._save_preference(theme.BACKGROUND_PREFERENCE, str(path))
        self._save_preference(theme.BACKGROUND_ZOOM_PREFERENCE, "1.0")
        if getattr(self, "background", None) is not None:
            self.background.refresh()
        self._update_hero_banner()
        return {
            "ok": True,
            "path": str(path),
            "width": result.get("width", 0),
            "height": result.get("height", 0),
        }

    def clear_background(self):
        self._save_preference(theme.BACKGROUND_PREFERENCE, "")
        if getattr(self, "background", None) is not None:
            self.background.refresh()
        self._update_hero_banner()
        return {"ok": True}

    def set_background_zoom(self, value):
        try:
            zoom = max(1.0, min(2.5, float(value)))
        except (TypeError, ValueError):
            zoom = 1.0
        self._save_preference(theme.BACKGROUND_ZOOM_PREFERENCE, "%.2f" % zoom)
        if getattr(self, "background", None) is not None:
            self.background.refresh()
        return zoom

    def set_background_param(self, name, value):
        """位置 / 模糊 / 暗化：滑动时先记偏好，再防抖重绘，别每像素都渲染。"""
        ranges = {
            "x": (theme.BACKGROUND_X_PREFERENCE, 0.0, 1.0),
            "y": (theme.BACKGROUND_Y_PREFERENCE, 0.0, 1.0),
            "blur": (theme.BACKGROUND_BLUR_PREFERENCE, 0, 30),
            "dim": (theme.BACKGROUND_DIM_PREFERENCE, 0.0, 0.7),
        }
        key, low, high = ranges.get(str(name), ("", 0.0, 1.0))
        if not key:
            return None
        try:
            number = max(low, min(high, float(value)))
        except (TypeError, ValueError):
            return None
        self._save_preference(key, ("%d" % number) if name == "blur" else "%.2f" % number)
        self._schedule_background_refresh()
        return number

    def _schedule_background_refresh(self):
        job = getattr(self, "_background_job", None)
        if job is not None:
            try:
                self.card.after_cancel(job)
            except Exception:
                pass
        try:
            self._background_job = self.card.after(160, self._refresh_background_now)
        except Exception:
            self._background_job = None

    def _refresh_background_now(self):
        self._background_job = None
        if getattr(self, "background", None) is not None:
            try:
                self.background.refresh()
            except Exception as exc:
                self._warn("background refresh failed: %s", exc)
        self._update_hero_banner()

    def _update_hero_banner(self):
        """把背景图铺一块到空会话顶部：用户选的图要真的看得见。"""
        chat = getattr(self, "chat", None)
        if chat is None:
            return False
        try:
            width = max(480, int(self._card_size[0]) - theme.PAD_MD * 6)
        except Exception:
            width = 720
        options = self._background_options()
        try:
            if int(self._card_size[1]) < 720:
                # 窗口矮的时候横幅会把下面的卡片挤出去，直接收起来。
                return chat.set_hero_image(None)
            photo = glass.photo(
                (width, 110),
                preset=options.get("preset"),
                mode=options.get("mode"),
                image_path=options.get("image"),
                zoom=options.get("zoom"),
                offset=(options.get("x", 0.5), options.get("y", 0.5)),
                blur=options.get("blur", 10),
                dim=options.get("dim"),
            )
        except Exception as exc:
            self._warn("hero background failed: %s", exc)
            photo = None
        try:
            return chat.set_hero_image(photo)
        except Exception:
            return False

    def set_background_motion(self, level):
        wanted = str(level or "off").lower()
        if wanted not in theme.BG_MOTION_NAMES:
            wanted = "off"
        self._save_preference(theme.BG_MOTION_PREFERENCE, wanted)
        if getattr(self, "background", None) is not None:
            self.background.set_motion(wanted)
            self.background.refresh()
        return wanted

    def background_motion(self):
        value = str(self._preference(theme.BG_MOTION_PREFERENCE) or "off").lower()
        return value if value in theme.BG_MOTION_NAMES else "off"

    def theme_preset(self):
        return theme.preset()

    def set_theme_preset(self, name):
        """换渐变预设。只重画界面，不碰任何业务数据。"""
        theme.set_preset(name, mode=theme.active())
        self._save_preference(theme.PRESET_PREFERENCE, theme.preset())
        self._save_preference(theme.ACCENT_PREFERENCE, theme.accent())
        return self._reskin("已换主题：%s" % theme.preset_label())

    def day_night_mode(self):
        return theme.active()

    def set_day_night(self, mode):
        """日/夜切换：只重画界面，不动桌宠渲染进程和模型资源。"""
        wanted = "light" if str(mode or "").lower() in ("light", "day") else "dark"
        theme.apply(wanted)
        self._save_preference(theme.THEME_PREFERENCE, wanted)
        self._save_preference(theme.ACCENT_PREFERENCE, theme.accent())
        return self._reskin("已切换到%s" % ("白天" if wanted == "light" else "夜晚"))

    def set_theme(self, name):
        """切主题：只重画界面，不动任何数据。"""
        resolved = theme.apply(name)
        if self.memory is not None:
            try:
                self.memory.set_preference(theme.THEME_PREFERENCE, str(name))
            except Exception as exc:
                self._warn("theme preference write failed: %s", exc)
        return self._reskin("已切换主题：%s" % resolved)

    def set_accent(self, name):
        """换强调色：只重画界面，不动任何数据。"""
        stored = self._preference(theme.THEME_PREFERENCE) or "dark"
        theme.apply(stored, accent_name=name)
        picked = theme.accent()
        if self.memory is not None:
            try:
                self.memory.set_preference(theme.ACCENT_PREFERENCE, picked)
            except Exception as exc:
                self._warn("accent preference write failed: %s", exc)
        return self._reskin("已换强调色：%s" % theme.accent_label(picked))

    def _reskin(self, message):
        """换配色以后重画一遍界面。

        控件颜色是创建时定死的，只刷底色会留下白底白字、深底深字，
        所以工作台已经建好时整块重建，重建失败再退回就地换皮。
        """
        overlay = getattr(self, "_subtitle_overlay", None)
        if overlay is not None:
            try:
                overlay.apply_theme()
            except Exception as exc:
                self._warn("subtitle overlay retheme failed: %s", exc)
        if getattr(self, "_chat_ready", False):
            try:
                self._rebuild_ui(hint=message)
                return theme.active()
            except Exception as exc:
                self._warn("reskin rebuild failed: %s", exc)
        try:
            self.chat.apply_theme()
        except Exception as exc:
            self._warn("chat retheme failed: %s", exc)
        if getattr(self, "background", None) is not None:
            try:
                self.background.refresh()
            except Exception as exc:
                self._warn("background refresh failed: %s", exc)
        if self.dev is not None:
            try:
                self.dev.configure(bg=theme.PANEL, fg=theme.MUTED)
            except Exception:
                pass
        window = getattr(self, "settings_window", None)
        if window is not None:
            try:
                if window.win.winfo_exists():
                    window.win.destroy()
                    self.settings_window = None
                    self.open_settings()
            except Exception as exc:
                self._warn("settings retheme failed: %s", exc)
        self._hint(message)
        return theme.active()

    def retheme(self, name):
        return self.set_theme(name)

    # ---------- V7：减少动态效果 ----------
    def motion_reduced(self):
        return str(self._preference(theme.MOTION_PREFERENCE) or "full").strip() == "reduced"

    def set_motion(self, reduced):
        """减少动态效果：角色不再自动摆动和呼吸，眨眼保留。"""
        flag = bool(reduced)
        self._save_preference(theme.MOTION_PREFERENCE, "reduced" if flag else "full")
        self._apply_motion()
        self._hint("已开启减少动态效果。" if flag else "已恢复角色自身动作。")
        return flag

    def _apply_motion(self):
        """把偏好发到渲染进程；角色没起来就只是记着，等它就绪再补发。"""
        reduced = self.motion_reduced()
        if self.live2d is not None:
            try:
                self.live2d.set_reduced_motion(reduced)
            except Exception as exc:
                self._warn("live2d motion mode failed: %s", exc)
        return reduced

    # ---------- V4：会话 ----------
    def _sidebar_state(self):
        saved = self._preference("chat.sidebar")
        if saved is None or saved == "":
            return False
        return str(saved).strip() == "1"

    def refresh_sessions(self):
        """把真实存在的会话列进侧栏：标题、置顶、标签、归档都用库里的记录。"""
        sessions = []
        if self.memory is not None:
            try:
                rows = self.memory.session_overview(
                    limit=60, include_archived=bool(self._show_archived)
                )
            except Exception as exc:
                self._warn("session list failed: %s", exc)
                rows = []
            for row in rows:
                session_id = str(row.get("session_id") or "")
                if not session_id:
                    continue
                title = str(row.get("title") or "").strip()
                if not title:
                    title = "主对话" if session_id == self.SESSION_ID else session_id
                marks = ""
                if row.get("pinned"):
                    marks += "★ "
                if row.get("archived"):
                    marks += "[归档] "
                tags = str(row.get("tags") or "").strip()
                label = "%s%s（%d）" % (marks, title, int(row.get("total") or 0))
                if tags:
                    label += " #" + tags.replace(",", " #")
                sessions.append((session_id, label))
        if not any(session_id == self.session_id for session_id, _ in sessions):
            sessions.insert(0, (self.session_id, self.session_id))
        self.chat.set_sessions(sessions, active_id=self.session_id)
        return sessions

    def switch_session(self, session_id):
        """切到另一个会话，并把它的历史重新画出来。"""
        self.close_panel()
        session_id = str(session_id or "").strip()
        if not session_id or session_id == self.session_id:
            return None
        self.session_id = session_id
        self.chat.clear()
        self._load_history(session_id)
        self.refresh_sessions()
        self._refresh_chat_title()
        self._set_busy(False)
        self.chat.set_panel_text("")
        self.show_chat()
        return session_id

    def new_session(self):
        """开一个新会话。旧的仍然留在库里，随时能切回去。"""
        self.close_panel()
        stamp = time.strftime("%m%d-%H%M%S")
        self.session_id = "desktop-%s" % stamp
        self.chat.clear()
        self.last_reply = None
        self.chat.set_attachments([])
        self._attachments = []
        self.chat.set_panel_text("")
        self.refresh_sessions()
        self._refresh_chat_title()
        # 新会话回到首屏：和启动时一模一样的欢迎页，不再堆一行提示。
        try:
            self.chat.set_tips(self.chat_tips())
            self.chat.set_feed(self.home_feed())
        except Exception as exc:
            self._warn("home feed failed: %s", exc)
        self.chat.show_empty(True)
        return self.session_id

    # ---------- V4：附件与技能 ----------
    def attach_file(self):
        """附一个本地文件路径到输入框。只填路径，不偷偷上传任何东西。"""
        try:
            path = filedialog.askopenfilename(
                parent=self.card, title="选择要提到的文件"
            )
        except Exception as exc:
            self._hint("选文件失败：%s" % exc)
            return None
        if not path:
            return None
        self._chat_add_attachment(path)
        self.chat.insert_input("%s " % path)
        self.chat.focus_input()
        self.show_chat()
        return path

    def attach_image(self):
        """附一张本地图片，同样只填路径，不偷传。"""
        try:
            path = filedialog.askopenfilename(
                parent=self.card,
                title="选择要提到的图片",
                filetypes=(
                    ("图片", "*.png *.jpg *.jpeg *.gif *.bmp *.webp"),
                    ("所有文件", "*.*"),
                ),
            )
        except Exception as exc:
            self._hint("选图片失败：%s" % exc)
            return None
        if not path:
            return None
        self._chat_add_attachment(path)
        self.chat.insert_input("%s " % path)
        self.chat.focus_input()
        self.show_chat()
        return path

    def skill_rows(self):
        return list(getattr(self.agent, "skill_rows", lambda: [])() or [])

    def use_skill(self, name):
        """把技能点名写法填进输入框，用户接着补要做什么。"""
        self.chat.insert_input("/skill %s " % name)
        self.chat.focus_input()
        self.show_chat()
        return name

    def skill_menu(self):
        """聊天窗口里的技能快捷入口。只列出真实存在的技能。"""
        menu = tk.Menu(self.card, tearoff=0)
        rows = self.skill_rows()
        if not rows:
            menu.add_command(label="还没有可用的技能", state="disabled")
        for row in rows:
            name = str(row.get("name"))
            label = "%s（%s）" % (name, row.get("permission") or "READ")
            if not row.get("enabled"):
                label += " · 已停用"
            menu.add_command(
                label=label,
                command=(lambda chosen=name: self.use_skill(chosen)) if row.get("enabled") else None,
                state="normal" if row.get("enabled") else "disabled",
            )
        menu.add_separator()
        menu.add_command(label="管理技能…", command=self.open_skills)
        try:
            x, y = self.card.winfo_rootx() + 40, self.card.winfo_rooty() + 60
        except Exception:
            x, y = 0, 0
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()
        return menu

    def _sync_task_panel(self, state=None):
        """真实的任务步骤：单行条给当前一步，右侧面板给整张清单。"""
        agent = self.agent
        state = state or getattr(agent, "task_state", "")
        steps = list(getattr(agent, "_task_steps", None) or [])
        terminal = (TaskState.COMPLETED.value, TaskState.FAILED.value, TaskState.CANCELLED.value)
        if not steps or state in terminal or state == TaskState.NEW.value:
            self.chat.set_task("", active=False)
            return self.chat.set_panel_text("")
        rows = []
        for step in steps[-8:]:
            detail = str(step.tool or step.error or "")
            rows.append((step.title, self.STEP_STATES.get(str(step.status), "todo"), detail))
        return self.chat.set_task_steps(
            rows, title=TASK_STATE_LABELS.get(state, state)
        )

    # ---------- 聊天工作台：事件入口 ----------
    WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

    # 能力卡片点了之后填进输入框的那句话。只填，不替用户发出去。
    CARD_PROMPTS = {
        "code": "帮我看看这个项目：先读结构，再找问题，最后给最小的改动。",
        "research": "帮我查一个题目，把来源写清楚，没查到的地方直说：",
        "files": "帮我整理一批文件，先给计划，动手之前让我看一眼。",
        "plan": "帮我安排日程和提醒，写明时间、优先级和提醒方式：",
        "chat": "随便聊两句，今天过得怎么样：",
        "study": "帮我把这件事讲清楚，先说思路再说结论：",
    }

    def home_feed(self):
        """首页信息流：每条都从真实存储里来，取不到就不摆这一条。"""
        cards = []
        for builder in (
            self._feed_today,
            self._feed_pending,
            self._feed_progress,
            self._feed_resume,
        ):
            try:
                card = builder()
            except Exception as exc:
                self._warn("feed card failed: %s", exc)
                continue
            if not card:
                continue
            if self.suggestion_muted("feed", card.get("key")):
                continue
            cards.append(card)
        return cards[:4]

    def _feed_today(self):
        """今日简报：本地时间加上真记着的东西，不编日程。"""
        now = datetime.now()
        stamp = "%d 年 %d 月 %d 日 %s" % (
            now.year, now.month, now.day, self.WEEKDAYS[now.weekday()]
        )
        bits = [stamp]
        rows = self.task_rows(limit=50)
        if rows:
            bits.append("任务库里 %d 件" % len(rows))
        if self.memory is not None:
            bits.append("记忆 %s" % self._memory_stats_text())
        return {
            "key": "today",
            "title": "今日简报",
            "body": "。".join(bits) + "。",
            "actions": [
                ("今天做什么", "帮我安排今天的计划，写明时间和优先级："),
                ("忽略", "dismiss:today"),
            ],
        }

    def _feed_pending(self):
        """待处理事项：等你决定的和出过错的，都是真的。"""
        rows = [
            row for row in self.task_rows(limit=50)
            if row["state"] in ("waiting_user", "failed")
        ]
        if not rows:
            return None
        body = "；".join(
            "%s（%s）" % ((row.get("goal") or "")[:24], row.get("state_label") or "")
            for row in rows[:3]
        )
        return {
            "key": "pending",
            "title": "需要你处理",
            "body": "有 %d 件在等你：%s。" % (len(rows), body),
            "actions": [
                ("打开任务中心", "open:tasks"),
                ("忽略", "dismiss:pending"),
            ],
        }

    def _feed_progress(self):
        """任务进展：按状态数出来的，不是估的。"""
        rows = self.task_rows(limit=50)
        if not rows:
            return None
        counts = {}
        for row in rows:
            name = row.get("state_label") or "未知"
            counts[name] = counts.get(name, 0) + 1
        body = "、".join("%s %d 件" % (name, count) for name, count in counts.items())
        newest = rows[0]
        goal = (newest.get("goal") or "")[:30]
        actions = [("打开任务中心", "open:tasks")]
        if goal:
            actions.append(("接着做上回的", "接着做上次没做完的那件事：%s" % goal))
        return {
            "key": "progress",
            "title": "任务进展",
            "body": "一共 %d 件：%s。最近一条：%s。" % (len(rows), body, goal or "（没写目标）"),
            "actions": actions + [("忽略", "dismiss:progress")],
        }

    def _feed_resume(self):
        """连续性提示：上次没做完的，只报事实。"""
        capsule = self.continuity_capsule()
        if not capsule:
            return None
        goal = (capsule.get("goal") or "")[:40]
        if not goal:
            return None
        bits = ["%s（%s）" % (goal, capsule.get("state_label") or "")]
        if capsule.get("done"):
            bits.append("已经做完 %d 步" % len(capsule["done"]))
        if capsule.get("next_step"):
            bits.append("下一步是%s" % capsule["next_step"])
        return {
            "key": "resume",
            "title": "上次停在这里",
            "body": "。".join(bits) + "。",
            "actions": [
                ("接着做", "接着做上次没做完的那件事：%s" % goal),
                ("忽略", "dismiss:resume"),
            ],
        }

    # ---------- 连续性上下文包 ----------
    def continuity_capsule(self, task_id=""):
        """把没做完的活包成一份小档案：目标、做到哪一步、下一步做什么。

        每一条都来自任务库里的真实记录；取不到就返回 None，绝不编。
        """
        store = getattr(self.agent, "task_store", None)
        if store is None:
            return None
        record = None
        try:
            ident = str(task_id or "")
            if ident:
                record = store.get(ident)
            else:
                rows = list(store.unfinished(limit=1))
                record = rows[0] if rows else None
        except Exception as exc:
            self._warn("capsule task read failed: %s", exc)
            return None
        if record is None:
            return None
        steps = []
        try:
            steps = list(store.steps(getattr(record, "id", "")))
        except Exception as exc:
            self._warn("capsule steps read failed: %s", exc)
        done, left, running = [], [], []
        for step in steps:
            title = self._cell(step, "title")
            status = self._cell(step, "status")
            if not title:
                continue
            if status in ("done", "completed", "ok", "success"):
                done.append(title)
            elif status in ("running", "doing"):
                running.append(title)
            else:
                left.append(title)
        state = str(getattr(record, "state", "") or "")
        next_step = (running or left or [""])[0]
        return {
            "task_id": str(getattr(record, "id", "")),
            "goal": str(getattr(record, "goal", "") or ""),
            "state": state,
            "state_label": TASK_STATE_LABELS.get(state, state),
            "done": done,
            "left": left,
            "running": running,
            "next_step": next_step,
            "updated_at": str(getattr(record, "updated_at", "") or ""),
        }

    @staticmethod
    def _cell(row, name):
        """任务步骤是数据库里的一行，取字段取不到就返回空串。"""
        try:
            if name in row.keys():
                return str(row[name] or "")
        except Exception:
            pass
        try:
            return str(row.get(name) or "")
        except Exception:
            return ""

    def capsule_text(self, capsule):
        """把上下文包说成人话：停在哪、做完几件、下一步做啥。"""
        if not capsule:
            return ""
        bits = ["上次停在这里：%s（%s）。" % (
            capsule.get("goal") or "（没写目标）", capsule.get("state_label") or "")]
        if capsule.get("done"):
            bits.append("已经做完 %d 步：%s。" % (
                len(capsule["done"]), "、".join(capsule["done"][:3])))
        if capsule.get("next_step"):
            bits.append("下一步：%s。" % capsule["next_step"])
        if capsule.get("updated_at"):
            bits.append("记于 %s。" % capsule["updated_at"])
        return "".join(bits)

    def dismiss_feed(self, key, event=None):
        """首页那张卡先不看了。只影响这一张，不动数据。"""
        self.dismiss_suggestion("feed", key)
        self.chat.set_feed(self.home_feed())
        return str(key or "")

    # ---------- 灵感建议 ----------
    def idea_cards(self):
        """按真实数据攒建议。每条都写清为什么提、用到什么、点了会去哪。"""
        cards = []
        for builder in (self._idea_habits, self._idea_stuck, self._idea_unfinished):
            try:
                card = builder()
            except Exception as exc:
                self._warn("idea card failed: %s", exc)
                continue
            if not card:
                continue
            if self.suggestion_muted("idea", card.get("key")):
                continue
            cards.append(card)
        return cards

    def _idea_habits(self):
        """还在等你拿主意的习惯。依据就是学习记录里那几条。"""
        candidates = list(self.persona_habit_candidates() or [])
        if not candidates:
            return None
        row = candidates[0]
        labels = persona_learner.rules()["labels"]
        field = labels.get(row.get("field"), row.get("field"))
        tries = row.get("seen") or 0
        return {
            "key": "habits",
            "title": "有一条习惯等你点头",
            "why": "%s 这条你重复过 %s 次，我先记着没敢直接用。" % (field, tries),
            "context": "用的是本地学习记录，没读聊天原文。",
            "requires": "不需要模型，也不需要联网。",
            "action": ("去人格页处理", "panel", "persona"),
        }

    def _idea_stuck(self):
        """卡住的任务：等你决定或者出过错的那种，可以接着做。"""
        rows = [
            row for row in self.task_rows(limit=50)
            if row["state"] in ("waiting_user", "failed")
        ]
        if not rows:
            return None
        first = rows[0]
        return {
            "key": "stuck",
            "title": "有 %d 件卡着" % len(rows),
            "why": "「%s」现在是%s，需要你看一眼。" % (
                (first.get("goal") or "")[:26], first.get("state_label") or ""),
            "context": "用的是本地任务库里的记录。",
            "requires": "不需要模型，也不需要联网。",
            "action": ("打开任务中心", "panel", "tasks"),
        }

    def _idea_unfinished(self):
        """上次没做完的活，可以接着做。"""
        capsule = self.continuity_capsule()
        if not capsule or not capsule.get("goal"):
            return None
        needs_model = not self._task_model_ready()
        return {
            "key": "resume",
            "title": "上次停在这里",
            "why": "「%s」还没做完，%s。" % (
                capsule["goal"][:26], capsule.get("state_label") or "在等下一步"),
            "context": "用的是本地任务记录和已经做完的步骤。",
            "requires": "要接着做需要任务模型。" if needs_model else "有任务模型，可以直接接着做。",
            "action": ("接着做", "continue", capsule["goal"][:40]),
        }

    def goal_cards(self):
        """在追的事：没做完的任务就是目标，每一步是里程碑。"""
        cards = []
        for row in self.task_rows(limit=50):
            if row["state"] in ("completed", "cancelled"):
                continue
            milestones = self.goal_milestones(row.get("id"))
            done = len([item for item in milestones if item["done"]])
            running = [item["title"] for item in milestones if item["running"]]
            left = [item["title"] for item in milestones if not item["done"] and not item["running"]]
            next_step = (running or left or [""])[0]
            cards.append({
                "id": row.get("id"),
                "goal": row.get("goal") or "",
                "state": row.get("state"),
                "state_label": row.get("state_label") or "",
                "milestones": milestones,
                "done": done,
                "total": len(milestones),
                "next_step": next_step,
                "needs_model": not self._task_model_ready(),
            })
        return cards

    def _task_model_ready(self):
        slot = getattr(self.agent, "task_slot", None)
        if slot is None:
            return False
        try:
            return bool(slot.available())
        except Exception:
            return False

    def goal_milestones(self, task_id):
        """一个目标的步骤清单。没记步骤就返回空，不编。"""
        store = getattr(self.agent, "task_store", None)
        if store is None:
            return []
        try:
            steps = list(store.steps(str(task_id or "")))
        except Exception as exc:
            self._warn("goal milestones failed: %s", exc)
            return []
        rows = []
        for step in steps:
            title = self._cell(step, "title")
            if not title:
                continue
            status = self._cell(step, "status")
            rows.append({
                "title": title,
                "status": status,
                "done": status in ("done", "completed", "ok", "success"),
                "running": status in ("running", "doing"),
            })
        return rows

    def task_rows(self, limit=20):
        """任务库里的记录，只读，不替用户改状态。"""
        store = getattr(self.agent, "task_store", None)
        if store is None:
            return []
        try:
            recent = list(store.recent(limit=limit))
        except Exception as exc:
            self._warn("task rows failed: %s", exc)
            return []
        rows = []
        for item in recent:
            state = str(getattr(item, "state", "") or "")
            rows.append(
                {
                    "id": str(getattr(item, "id", "")),
                    "goal": str(getattr(item, "goal", "") or ""),
                    "state": state,
                    "state_label": TASK_STATE_LABELS.get(state, state),
                }
            )
        return rows

    def _task_panel_text(self):
        """右面板内容：只搬任务库里的真东西。"""
        rows = self.task_rows(limit=5)
        if not rows:
            return "还没有任务记录。要做的事在输入框里说清楚，我会先给计划再动手。"
        return "\n".join(
            "%s \u00b7 %s" % (row.get("state_label") or "", row.get("goal") or "")
            for row in rows
        )

    def chat_tips(self):
        """推荐操作：只推现在真能做的事，点一下就把那句话填进输入框。"""
        tips = []
        rows = self.task_rows(limit=3)
        if rows:
            goal = str(rows[0].get("goal") or "").strip()
            if goal:
                tips.append(("接着做上回的", "接着做上次没做完的那件事：%s" % goal[:40]))
        try:
            candidates = list(self.persona_habit_candidates() or [])
        except Exception as exc:
            self._warn("habit candidates failed: %s", exc)
            candidates = []
        if candidates:
            tips.append(("看看学到的习惯", "把你还拿不准的习惯列出来给我看看。"))
        tips.append(("现在几点", "现在几点了？"))
        tips.append(("提醒我一件事", "帮我记一个提醒："))
        return tips[:4]

    def _chat_search(self, payload=None):
        """顶栏搜索：先在聊天记录里定位，找不到就翻会话列表。"""
        word = simpledialog.askstring("搜索", "在聊天记录里找：", parent=self.card)
        word = str(word or "").strip()
        if not word:
            return None
        if self.chat.locate(word):
            self.show_chat()
            return word
        hits = []
        if self.memory is not None:
            hits = [
                str(row.get("session_id") or "")
                for row in self.memory.find_sessions(word)
            ]
        if hits:
            self.chat.show_sidebar(True)
            self._append("hint", "聊天记录里没有，会话列表里有 %d 个匹配。" % len(hits))
            return hits
        self._append("hint", "聊天记录里没有找到「%s」。" % word)
        return None

    def _chat_session_rename(self, session_id=None):
        session_id = self._chat_target_session(session_id)
        current = ""
        if self.memory is not None:
            current = str((self.memory.session_meta(session_id) or {}).get("title") or "")
        title = simpledialog.askstring(
            "重命名会话", "新名字：", initialvalue=current, parent=self.card
        )
        if title is None:
            return None
        title = str(title).strip()
        if self.memory is not None:
            self.memory.rename_session(session_id, title)
        self.refresh_sessions()
        self._refresh_chat_title()
        return title

    def _chat_session_tags(self, session_id=None):
        session_id = self._chat_target_session(session_id)
        current = ""
        if self.memory is not None:
            current = str((self.memory.session_meta(session_id) or {}).get("tags") or "")
        tags = simpledialog.askstring(
            "会话标签", "用逗号分开，例如：装机,硬件", initialvalue=current, parent=self.card
        )
        if tags is None:
            return None
        if self.memory is not None:
            self.memory.tag_session(session_id, str(tags).strip())
        self.refresh_sessions()
        return tags

    def _chat_session_delete(self, session_id=None):
        session_id = self._chat_target_session(session_id)
        if not messagebox.askyesno(
            "删除会话", "删除后这个会话的消息就没了，确定吗？", parent=self.card
        ):
            return False
        if self.memory is not None:
            self.memory.delete_session(session_id)
        if session_id == self.session_id:
            self.session_id = self.SESSION_ID
            self.chat.clear()
            self._load_history(self.session_id)
        self.refresh_sessions()
        return True

    def _chat_session_export(self, session_id=None):
        session_id = self._chat_target_session(session_id)
        rows = []
        if self.memory is not None:
            rows = self.memory.repository.recent_messages(session_id, limit=2000) or []
        path = filedialog.asksaveasfilename(
            parent=self.card,
            title="导出会话",
            defaultextension=".json",
            initialfile="deskpet-%s.json" % session_id,
            filetypes=[("JSON", "*.json"), ("纯文本", "*.txt"), ("所有文件", "*.*")],
        )
        if not path:
            return None
        try:
            if str(path).lower().endswith(".txt"):
                body = "\n".join(
                    "%s: %s" % (row.get("role"), row.get("content")) for row in rows
                )
                with io.open(path, "w", encoding="utf-8") as handle:
                    handle.write(body)
            else:
                payload = {"session_id": session_id, "messages": rows}
                with io.open(path, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
        except Exception as exc:
            self._append("err", "导出失败：%s" % exc)
            return None
        self._append("notice", "会话导出到 %s（%d 条消息）。" % (path, len(rows)))
        return path

    def chat_commands(self):
        """斜杠命令表：能做的才列，做不到的不写。"""
        rows = [
            ("新建对话", "new"),
            ("搜索历史", "search"),
            ("打开任务中心", "tasks"),
            ("打开技能", "skills"),
            ("打开设置", "settings"),
            ("清空当前对话", "clear"),
        ]
        for row in self.skill_rows():
            if row.get("enabled"):
                rows.append(("技能 %s" % row.get("name"), "skill:%s" % row.get("name")))
        return rows

    def _refresh_chat_title(self):
        """顶栏显示当前会话的真名字，没名字就用会话号。"""
        title = ""
        if self.memory is not None:
            title = str((self.memory.session_meta(self.session_id) or {}).get("title") or "")
        fallback = "主对话" if self.session_id == self.SESSION_ID else self.session_id
        try:
            self.chat.title.configure(text=title or fallback)
        except Exception as exc:
            self._warn("chat title failed: %s", exc)
            return ""
        return title

    def locate_history(self, needle):
        """在聊天记录里定位一段文字，找不到就如实说找不到。"""
        if self.chat.locate(needle):
            return True
        self._hint("聊天记录里没有找到「%s」。" % needle)
        return False

    # ---------- 中央功能页 ----------
    def forget_panels(self):
        """丢掉缓存的功能页对象。重建界面之后旧对象就作废了，留着只会误导。"""
        self._panels = {}
        return True

    def close_panel(self):
        """关掉当前功能页，回到聊天。"""
        try:
            return self.chat.close_page()
        except Exception as exc:
            self._warn("close panel failed: %s", exc)
            return False

    def _persona_page(self, parent):
        """人格与记忆合成一页：两个分页，都在同一个窗口里。"""

        body = tk.Frame(parent, bg=theme.BG)
        body.pack(fill="both", expand=True)
        book = theme.tabs(body)
        persona_frame = tk.Frame(book, bg=theme.BG)
        memory_frame = tk.Frame(book, bg=theme.BG)
        book.add(persona_frame, text="人格")
        book.add(memory_frame, text="记忆")
        book.pack(fill="both", expand=True)
        persona = PersonaWindow(self, parent=persona_frame)
        memory = MemoryWindow(self, parent=memory_frame)
        panels = getattr(self, "_panels", None)
        if panels is not None:
            # 单独点「记忆」和从这一页点，用的是同一个对象。
            panels["memory"] = memory
        combo = ComboPanel(self, body, [persona, memory])
        combo.persona = persona
        combo.memory = memory
        return combo

    def persona_text(self):
        """当前生效的人格原文。没写过就是空串。"""
        return persona_profile.text(self.memory)

    def persona_name(self):
        """当前生效的名字：用户/角色卡设过的优先，否则用配置里的默认值。"""
        return persona_profile.name(
            self.memory, getattr(self.config, "persona_name", "")
        )

    def set_persona_name(self, value):
        """只改名字，不动人格文本，也不生成人格版本。清空表示回到配置里的默认名字。"""
        if persona_profile.clean_name(value):
            result = persona_profile.set_name(self.memory, value)
        else:
            persona_profile.clear_name(self.memory)
            result = {"ok": True, "name": getattr(self.config, "persona_name", "") or ""}
        if result.get("ok"):
            self._apply_persona_settings()
        return result

    def import_persona_card(self, preview):
        """导入一张角色卡：名字与人格文本一起换，留一条版本记录。"""
        result = persona_cards.apply(self.memory, preview)
        if result.get("ok"):
            self._apply_persona_settings()
        return result

    # ---------- 角色卡库 ----------
    def persona_card_library(self):
        """卡库里的卡（只读列表）。坏文件会被跳过，不影响其它卡。"""
        return persona_cards.library_cards(self.config)

    def save_persona_card(self, preview, source=""):
        """把导入的卡存一份进卡库，方便以后切回来。同名不覆盖。"""
        return persona_cards.save_to_library(self.config, preview, source_name=source)

    def load_persona_card(self, path):
        return persona_cards.load_library_card(path)

    def open_persona_card_folder(self):
        """打开卡库目录。没有就先建好，只动这一个目录。"""
        folder = persona_cards.library_dir(self.config)
        if folder is None:
            return {"ok": False, "error": "没有配置数据目录"}
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))
        except Exception as exc:
            return {"ok": False, "error": "打不开文件夹：%s" % exc}
        return {"ok": True, "path": str(folder)}

    def persona_draft(self):
        """编辑器里的草稿。没存过草稿就是生效文本。"""
        return persona_profile.draft(self.memory)

    def persona_profile_state(self):
        """面板要的一行摘要：字数、版本、草稿状态、每轮上限。"""
        return persona_profile.describe(self.memory, getattr(self.agent, "persona", None))

    def save_persona_text(self, body, note=""):
        """保存并生效。原文照存，顺手留一个版本。"""
        result = persona_profile.save_text(self.memory, body, note=note)
        if result.get("ok"):
            self._apply_persona_settings()
        return result

    def save_persona_draft(self, body):
        """只存草稿，不生效。"""
        return persona_profile.save_draft(self.memory, body)

    def persona_versions(self, limit=persona_profile.VERSION_LIMIT):
        return persona_profile.versions(self.memory, limit=limit)

    def persona_version_text(self, number):
        return persona_profile.version_text(self.memory, number)

    def revert_persona_version(self, number):
        """把历史版本写回生效位置。任何版本都不会被删。"""
        result = persona_profile.revert_to(self.memory, number)
        if result.get("ok"):
            self._apply_persona_settings()
        return result

    def set_persona_budget(self, value):
        """每轮发给模型的人格文本上限。"""
        value = persona_profile.set_budget(self.memory, value)
        self._apply_persona_settings()
        return value

    def persona_habits(self):
        return persona_habits.load(self.memory)

    def _persona_policy(self):
        policy = getattr(self.agent, "policy", None)
        if policy is None:
            policy = persona_policy.PersonaPolicy(self.memory)
            self.agent.policy = policy
            capabilities = getattr(self.agent, "capabilities", None)
            if capabilities is not None:
                capabilities.policy = policy
            behavior = getattr(self.agent, "behavior", None)
            if behavior is not None:
                behavior.policy = policy
        return policy

    def persona_context_mode(self):
        return self._persona_policy().mode()

    def set_persona_context_mode(self, mode):
        policy = self._persona_policy()
        wanted = persona_policy.normalize_mode(mode)
        if not policy.set_mode(wanted):
            return {"ok": False, "error": "情境偏好没存下来"}
        return {
            "ok": True,
            "mode": wanted,
            "label": persona_policy.LABELS.get(wanted, wanted),
        }

    def persona_context_summary(self):
        return self._persona_policy().summary()

    def save_persona_habits(self, values):
        result = persona_habits.save(self.memory, values)
        if result.get("ok"):
            self._apply_persona_settings()
        return result

    def reset_persona_habits(self):
        result = persona_habits.reset(self.memory)
        if result.get("ok"):
            self._apply_persona_settings()
        return result

    def persona_habit_learning(self):
        """习惯学习开关。默认开着。"""
        return persona_habits.learning_enabled(self.memory)

    def set_persona_habit_learning(self, flag):
        """学习开关。接入了学习就走它，因为关掉要顺手把观察到的内容收回。"""
        learner = getattr(self.agent, "learning", None)
        if learner is not None:
            return learner.set_enabled(flag)
        return persona_habits.set_learning_enabled(self.memory, bool(flag))

    # ---------- 习惯学习：候选偏好 ----------
    def persona_habit_candidates(self):
        """学习记录。稍后处理过的先躲开，时间到了自己回来。没接入就返回空。"""
        learner = getattr(self.agent, "learning", None)
        if learner is None:
            return []
        reader = getattr(learner, "visible_rows", None)
        if callable(reader):
            try:
                return reader()
            except Exception as exc:
                self._warn("habit visible rows failed: %s", exc)
        return learner.rows()

    def persona_habit_scope(self, identifier):
        """这条建议管多大范围。读不到就如实说读不到，不编。"""
        learner = getattr(self.agent, "learning", None)
        if learner is None:
            return ""
        try:
            return str(learner.scope(identifier) or "")
        except Exception as exc:
            self._warn("habit scope failed: %s", exc)
            return ""

    def undo_persona_habit(self, identifier):
        """撤销：已采纳的退回候选，不再影响说话方式。"""
        return self._persona_habit_action("undo", identifier)

    def snooze_persona_habit(self, identifier):
        """稍后再说：这条先收起来，过一阵再问你一次。"""
        return self._persona_habit_action("snooze", identifier)

    def persona_habit_summary(self):
        """学习的一行摘要：开关、候选数、已采纳数。"""
        learner = getattr(self.agent, "learning", None)
        if learner is None:
            return {
                "learning": False,
                "total": 0,
                "candidate": 0,
                "accepted": 0,
                "rejected": 0,
                "applied": 0,
            }
        return learner.summary()

    def persona_habit_evidence(self, identifier):
        """某条建议的依据：用户当时说的原话片段。"""
        learner = getattr(self.agent, "learning", None)
        return learner.evidence(identifier) if learner is not None else []

    def _persona_habit_action(self, name, *args):
        """学习记录上的一次改动。失败就如实返回 False。"""
        learner = getattr(self.agent, "learning", None)
        if learner is None:
            return False
        try:
            result = getattr(learner, name)(*args)
        except Exception as exc:
            if self.logger:
                self.logger.warning("habit record %s failed: %s", name, exc)
            return False
        return bool(result)

    def accept_persona_habit(self, identifier):
        return self._persona_habit_action("accept", identifier)

    def reject_persona_habit(self, identifier):
        return self._persona_habit_action("reject", identifier)

    def remove_persona_habit(self, identifier):
        return self._persona_habit_action("remove", identifier)

    def edit_persona_habit(self, identifier, value):
        return self._persona_habit_action("edit", identifier, value)

    def clear_persona_habit_records(self):
        return self._persona_habit_action("clear")

    def _apply_persona_settings(self):
        """把存好的文本与习惯推给当前人格对象。推不动就记一笔，不假装。"""
        try:
            result = persona_profile.apply_to(
                getattr(self.agent, "persona", None),
                self.memory,
                default_name=getattr(self.config, "persona_name", ""),
            )
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        if not result.get("ok") and self.logger:
            self.logger.warning("persona settings not applied: %s", result.get("error"))
        elif getattr(self, "chat", None) is not None:
            # 名字可能变了，空会话的问候跟着换，别还挂着旧名字。
            try:
                self.chat.set_greeting(self._greeting_text())
            except Exception:
                pass
        return result

    def persona_test_run(self, scenario_id, note=None):
        """跑一次人格测试。只调模型，不写聊天记录、不写记忆、不改人格。"""
        item = persona_lab.scenario(scenario_id)
        if item is None:
            return {"ok": False, "error": "没有这个场景", "content": ""}
        body = str(note or "").strip() or self._persona_test_note()
        return persona_lab.run(self._persona_test_provider(), body, item["input"])

    def persona_test_compare(self, scenario_id, left_note="", right_note=""):
        """同一句话，两套人格各回一次。"""
        item = persona_lab.scenario(scenario_id)
        if item is None:
            return {"ok": False, "error": "没有这个场景"}
        left = str(left_note or "").strip() or self._persona_test_note()
        return persona_lab.compare(
            self._persona_test_provider(), left, str(right_note or "").strip(), item["input"]
        )

    def _persona_test_provider(self):
        """测试台用陪伴模型那一套配置；没有就退回当前主模型。"""
        agent = getattr(self, "agent", None)
        slot = getattr(agent, "companion_slot", None) if agent is not None else None
        provider = getattr(slot, "provider", None)
        if provider is not None:
            return provider
        return getattr(agent, "provider", None)

    def _persona_test_note(self):
        """当前这套人格真正会发出去的提示词，测试台按它跑。"""
        try:
            return build_persona_note(getattr(self.agent, "persona", None))
        except Exception:
            return ""

    def _maybe_first_run(self):
        """第一次打开时说清数据在哪、权限怎么给。老用户只记一笔，不弹窗。"""
        from ui import wizard

        if wizard.is_done(self):
            return None
        if wizard.looks_used(self):
            wizard.mark_done(self)
            return None
        try:
            return wizard.FirstRunWizard(self)
        except Exception as exc:
            self._warn("first run wizard failed: %s", exc)
            wizard.mark_done(self)
            return None

    def export_diagnostics(self, target=""):
        """打一个诊断包。包里只有脱敏后的路径、版本和日志。"""
        from logkit import report as report_module

        if not target:
            try:
                target = filedialog.asksaveasfilename(
                    parent=self.root,
                    title="保存诊断包",
                    defaultextension=".zip",
                    initialfile=report_module.default_target(self.config).name,
                    filetypes=[("压缩包", "*.zip"), ("所有文件", "*.*")],
                )
            except Exception:
                target = ""
            if not target:
                self._hint("没选保存位置，诊断包没导出。")
                return None
        extra = {}
        try:
            extra = self.developer_rows_dict()
        except Exception:
            extra = {}
        result = report_module.export_report(self.config, target=target, extra=extra)
        if not result.get("ok"):
            self._hint("诊断包没做成：%s" % result.get("error"))
            return result
        self._hint("诊断包已导出：%s" % result.get("path"))
        return result

    def prefill_chat(self, text):
        """把一句话放进输入框。只放不发，发不发由用户决定。"""
        text = str(text or "").strip()
        if not text:
            return ""
        self.show_chat()
        try:
            self.chat.clear_input()
            self.chat.input.insert("1.0", text)
            self.chat.input.focus_set()
        except Exception as exc:
            self._warn("prefill failed: %s", exc)
            return ""
        return text

    def _hint(self, text):
        """设置窗口底部的小提示，只给用户看，不发给模型。"""
        self._hint_settings(text)
        return text

    def test_model(self, role="companion"):
        """连接测试：只看槽位状态，不真的发请求，免得白花钱。"""
        label = "陪伴" if role == "companion" else "任务"
        slot = getattr(self.agent, role + "_slot", None)
        if slot is None:
            self._hint(f"没有{label}模型槽位。")
            return False
        try:
            available = bool(slot.available())
            model = getattr(slot, "model", "") or "未命名"
            status = slot.label
        except Exception as exc:
            self._hint(f"{label}模型状态读取失败：{exc}")
            return False
        if available:
            self._hint(f"{label}模型已连接：{model}（{status}）")
        else:
            self._hint(f"{label}模型未连接：{status}")
        return available

    # ---------- 多模型配置 ----------
    def reload_models(self):
        """配置变更后重建 Provider：下一句话就用新模型。"""
        from model import adapter as model_adapter

        agent = getattr(self, "agent", None)
        if agent is None:
            return {"ok": False, "error": "Agent 还没准备好"}
        config = self.config
        companion = model_adapter.build_companion_provider(config)
        task = model_adapter.build_task_provider(config)
        call_log = getattr(agent, "model_call_log", None)
        companion = LoggedProvider.wrap(companion, call_log, "companion")
        task = LoggedProvider.wrap(task, call_log, "task")
        try:
            agent.models.companion.provider = companion
            agent.models.companion.configured = companion is not None
            agent.models.task.provider = task
            agent.models.task.configured = task is not None
            agent.set_model_catalog(model_adapter.build_catalog(config))
        except Exception as exc:
            if self.logger:
                self.logger.warning("reload models failed: %s", exc)
            return {"ok": False, "error": str(exc)}
        # 会话覆盖指向已删除/停用的配置时清掉，别让人切到一个不存在的对象。
        for sid, config_id in list(getattr(agent, "_session_models", {}).items()):
            if config_id not in agent.model_catalog:
                agent.set_session_model(sid, "")
        self._refresh_status()
        try:
            self._chat_status_labels()
        except Exception:
            pass
        return {
            "ok": True,
            "companion": getattr(companion, "model", "") or "",
            "task": (getattr(task, "model", "") or "") if task is not None else "",
        }

    def model_call_rows(self, limit=30):
        service = getattr(getattr(self, "agent", None), "model_call_log", None)
        return service.rows(limit=limit) if service is not None else []

    def model_call_summary(self):
        service = getattr(getattr(self, "agent", None), "model_call_log", None)
        return service.summary() if service is not None else {}

    def clear_model_calls(self):
        service = getattr(getattr(self, "agent", None), "model_call_log", None)
        return service.clear() if service is not None else 0

    def _preference(self, key):
        if self.memory is None:
            return None
        try:
            return self.memory.get_preference(key)
        except Exception:
            return None

    def _preference_enabled(self):
        value = self._preference("proactive_enabled")
        if value is None:
            return bool(self.config.proactive)
        return str(value).strip().lower() not in {"0", "false", "no", "off"}

    def _quiet_enabled(self):
        if self.quiet is not None:
            try:
                return bool(self.quiet.enabled())
            except Exception:
                return False
        value = self._preference("quiet_mode")
        if value is None:
            return False
        return str(value).strip().lower() not in {"0", "false", "no", "off", ""}

    def _suggest_ignored(self):
        """这次打开窗口之后被忽略掉的那些建议。关掉程序就忘。"""
        if getattr(self, "_suggest_ignored_set", None) is None:
            self._suggest_ignored_set = set()
        return self._suggest_ignored_set

    def suggest_tag(self, group, key):
        return "%s:%s" % (str(group or ""), str(key or ""))

    def suggestion_muted(self, group, key):
        """这条建议现在要不要提。忽略过的和以后不提的都不提。"""
        tag = self.suggest_tag(group, key)
        if tag in self._suggest_ignored():
            return True
        raw = self._preference(self.SUGGEST_MUTE_PREFERENCE) or ""
        return tag in [item for item in str(raw).split(",") if item]

    def dismiss_suggestion(self, group, key, forever=False):
        """忽略一条建议。forever=True 表示以后都不提，写进偏好。"""
        tag = self.suggest_tag(group, key)
        self._suggest_ignored().add(tag)
        if not forever:
            return tag
        raw = str(self._preference(self.SUGGEST_MUTE_PREFERENCE) or "")
        items = [item for item in raw.split(",") if item and item != tag]
        items.append(tag)
        self._save_preference(self.SUGGEST_MUTE_PREFERENCE, ",".join(items))
        return tag

    def unmute_suggestions(self):
        """把「以后不提」全部撤销，建议重新出现。"""
        self._suggest_ignored().clear()
        return self._save_preference(self.SUGGEST_MUTE_PREFERENCE, "")

    def muted_suggestions(self):
        """现在被永久忽略的建议，给设置页列出来用。"""
        raw = str(self._preference(self.SUGGEST_MUTE_PREFERENCE) or "")
        return [item for item in raw.split(",") if item]

    def _hint_settings(self, text):
        """设置窗口没开着的时候，提示没有地方显示，写日志就够了。"""
        text = text or ""
        hint = getattr(self, "settings_hint", None)
        if hint is None:
            if self.logger:
                self.logger.info("settings hint: %s", text)
            return None
        try:
            hint.config(text=text)
        except Exception:
            return None
        return text

    # ---------- 生命周期 ----------
    def _on_ui_error(self, exc_type, exc_value, tb):
        """界面回调里没接住的异常：写日志，界面上给一句人话，不弹 traceback。

        打包成窗口程序后 stderr 是空的，tkinter 默认的报错会直接消失，
        所以这里必须自己接住，否则用户只看到一个没反应的桌宠。
        """
        detail = "".join(traceback.format_exception(exc_type, exc_value, tb))
        if self.logger:
            self.logger.error("ui callback failed: %s", detail)
        try:
            self.chat.set_state(chat_view.ERROR, "出错了，详情见日志")
        except Exception:
            pass
        return detail

    def close(self):
        # 托盘退出和窗口关闭可能同时触发，第二次直接跳过，避免重复关资源。
        if getattr(self, "_closed", False):
            return True
        self._closed = True
        # 先等回复线程停手，别让它一边写库一边拆窗口。
        self._join_workers()
        # 背景防抖和动效的定时器也要停，否则窗口销毁后还会触发回调。
        job = getattr(self, "_background_job", None)
        if job is not None:
            try:
                self.card.after_cancel(job)
            except Exception:
                pass
            self._background_job = None
        if getattr(self, "background", None) is not None:
            try:
                self.background.stop()
            except Exception:
                pass
            self.background = None
        skills = getattr(self.agent, "skills", None)
        if skills is not None:
            try:
                skills.cancel()
            except Exception as exc:
                if self.logger:
                    self.logger.warning("skill stop failed: %s", exc)
        if self.live2d is not None:
            try:
                self.live2d.stop()
            except Exception as exc:
                if self.logger:
                    self.logger.warning("live2d stop failed: %s", exc)
            self.live2d = None
        if getattr(self, "subtitle", None) is not None:
            try:
                self.subtitle.stop()
            except Exception as exc:
                if self.logger:
                    self.logger.warning("subtitle stop failed: %s", exc)
            self.subtitle = None
        overlay = getattr(self, "_subtitle_overlay", None)
        if overlay is not None:
            try:
                overlay.destroy()
            except Exception:
                pass
            self._subtitle_overlay = None
        tray_icon = getattr(self, "tray", None)
        if tray_icon is not None:
            try:
                tray_icon.stop()
            except Exception as exc:
                if self.logger:
                    self.logger.warning("tray stop failed: %s", exc)
        if self.speech is not None:
            try:
                self.speech.stop()
            except Exception as exc:
                if self.logger:
                    self.logger.warning("speech stop failed: %s", exc)
        if self.voice is not None:
            try:
                self.voice.stop()
            except Exception as exc:
                if self.logger:
                    self.logger.warning("voice stop failed: %s", exc)
        if self.stt is not None:
            try:
                self.stt.stop()
            except Exception as exc:
                if self.logger:
                    self.logger.warning("stt stop failed: %s", exc)
        if self.wake is not None:
            try:
                self.wake.stop()
            except Exception as exc:
                if self.logger:
                    self.logger.warning("wake stop failed: %s", exc)
        if self.mic is not None:
            self.mic.off()
        if self.worklog is not None:
            try:
                self.worklog.close_current()
            except Exception as exc:
                if self.logger:
                    self.logger.warning("work record stop failed: %s", exc)
        if self.scheduler:
            try:
                self.scheduler.stop()
            except Exception as exc:
                if self.logger:
                    self.logger.warning("scheduler stop failed: %s", exc)
        if self.logger:
            self.logger.info("deskpet ui closing")
        try:
            self._save_position()
            self.hide_chat()
            self.card.destroy()
        except Exception:
            pass
        logging.shutdown()
        try:
            self.root.destroy()
        except Exception as exc:
            if self.logger:
                self.logger.debug("root destroy failed: %s", exc)
        return True

    def run(self):
        if self.bus is not None:
            from events import bus as bus_module

            self.bus.subscribe(bus_module.COMPANION, self.on_companion)
            self.bus.subscribe(bus_module.REMINDER, self.on_reminder)
        if self.scheduler:
            self.scheduler.start()
        self.root.update_idletasks()
        self._apply_saved_position()
        if self._chat_was_open():
            # 上次关掉之前聊天是开着的，这次照旧打开。
            self.show_chat()
        elif self.chat_visible:
            self._place_card()
        self.root.after(200, self._stay_on_top)
        self.root.after(self.SHELL_MS, self._shell_tick)
        self.root.after(self.HIT_MS, self._hit_tick)
        self.root.after(600, self._maybe_first_run)
        self.root.after(self.POLL_MS, self._drain)
        self.root.after(self.IDLE_MS, self._idle_tick)
        if self.activity is not None and self.activity.available():
            self.root.after(self.ACTIVITY_MS, self._activity_tick)
        if self.worklog is not None:
            self.root.after(self.WORK_MS, self._work_tick)
        if self.focus is not None and self.focus.available():
            self.root.after(self.FOCUS_MS, self._focus_tick)
        if self.environment is not None and self.environment.available():
            self.root.after(self.ENVIRONMENT_MS, self._environment_tick)
        if self.screen is not None and self.screen.available():
            self.root.after(self.SCREEN_MS, self._screen_tick)
        if self.detection is not None and self.detection.available():
            self.root.after(self.DETECTION_MS, self._detection_tick)
        if self.awareness is not None and self.awareness.active():
            self.root.after(self.AWARENESS_MS, self._awareness_tick)
        if self.voice is not None and self.voice.available() and self.voice.start():
            self._mic_state(self.wake.armed if self.wake is not None else False)
            self.root.after(self.VOICE_MS, self._voice_tick)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.start_tray()
        self.root.mainloop()
        return 0


def launch(agent, config, memory=None, bus=None, scheduler=None, logger=None, confirmer=None, behavior=None, voice=None, speech=None, stt=None, wake=None, mic=None, activity=None, focus=None, quiet=None, environment=None, screen=None, vision=None, awareness=None, detection=None, body=None, privacy=None, pending_tasks=None, capture=None, knowledge=None, worklog=None):
    # 真要开界面了才声明 DPI 感知：必须赶在建任何窗口之前，否则字会被系统拉伸。
    # 已经有窗口活着就说明不是首次启动，这时声明也不作数，索性跳过。
    try:
        if tk._default_root is None:
            theme.enable_dpi_awareness()
    except Exception:
        pass
    pet = DesktopPet(
        agent,
        config,
        memory=memory,
        bus=bus,
        scheduler=scheduler,
        logger=logger,
        behavior=behavior,
        voice=voice,
        speech=speech,
        stt=stt,
        wake=wake,
        mic=mic,
        activity=activity,
        focus=focus,
        quiet=quiet,
        environment=environment,
        screen=screen,
        vision=vision,
        awareness=awareness,
        detection=detection,
        body=body,
        privacy=privacy,
        pending_tasks=pending_tasks,
        capture=capture,
        knowledge=knowledge,
        worklog=worklog,
    )
    if voice is not None:
        # 听到的话先进唤醒检查，再走聊天通道；Agent Core 不知道语音的存在。
        voice.on_text = pet.receive_speech
    if wake is not None:
        # 唤醒词之后的正式内容走聊天通道。
        wake.on_wake = pet.submit_voice
    if stt is not None:
        # 识别出的文字走同一条聊天通道；Agent Core 不知道语音的存在。
        stt.on_text = pet.submit_voice
    if activity is not None:
        # 用户离开或回来只改行为层节奏，不吵醒模型。
        activity.on_idle = pet.user_away
        activity.on_return = pet.user_back
    if focus is not None:
        # 全屏时自动降低存在感，退出后恢复；不经过模型。
        focus.on_enter = pet.enter_focus
        focus.on_exit = pet.exit_focus
    if confirmer is not None:
        confirmer.ui = pet
    pet.run()
    return 0
