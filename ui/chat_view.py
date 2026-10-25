"""聊天工作台：顶栏、会话抽屉、气泡、代码块、任务面板与输入区。

只管界面，不调用模型：发送、取消、清空、重试、切会话都通过回调交回宿主。
消息用文本标签表现气泡，代码块单独用等宽字体和独立底色。
布局从上到下：顶栏 → 状态行 → 空会话问候 → 消息区 → 任务面板 → 状态 → 输入区。
"""

import re
import time
import tkinter as tk
from tkinter import font as tkfont

from ui import theme
from ui.chat_input import ChatInput  # noqa: F401  (外部按老路径引用)
from ui.chat_build import ChatBuild

# 界面状态名，配合 theme.STATE_COLORS
IDLE = "idle"
BUSY = "busy"
OK = "ok"
WARN = "warn"
ERROR = "error"

# 围栏标记用转义写法，免得源码里出现连续三个反引号。
FENCE = "\x60\x60\x60"
CODE_FENCE = re.compile(
    re.escape(FENCE) + r"([A-Za-z0-9_+-]*)[ \t]*\n(.*?)" + re.escape(FENCE), re.DOTALL
)




class ChatView(ChatBuild):
    """聊天浮层的控件集合。宿主拿到控件后自己决定业务。"""

    SIDEBAR_WIDTH = 96
    MIN_WRAP = 200
    SIDEBAR_PIN_WIDTH = 520  # 窗口够宽时抽屉占一列，不再盖住消息
    WORKBENCH_WIDTH = 720    # 到这个宽度就按工作台来：输入框留三行
    MAX_TEXT_WIDTH = theme.CONTENT_MAX_WIDTH  # 正文最宽 900，再宽就两侧留白
    ME_BUBBLE_WIDTH = 460    # 自己的话单独占一条，太宽就显不出「是我说的」
    DEFAULT_GREETING = "我在这里。直接说你想做什么，任务类请求我会先问一声。"
    QUICK_ACTIONS = (("附件", "_attach"), ("技能", "_skills"), ("设置", "_settings"))
    # 窗口比这个窄，导航自动收起，别把中央区挤没了。
    NAV_AUTO_COLLAPSE_WIDTH = 900
    # 右面板要留够中央区的宽度才允许展开。
    PANEL_MIN_WINDOW_WIDTH = 1040
    # 已经展开后，窗口要再窄一点才收起，避免在临界宽度来回切换。
    PANEL_HIDE_WINDOW_WIDTH = 1000
    # 空会话的能力卡片，按定稿图排成 2×2：图标、标题、一句说明。
    # 四张卡各用一种点缀色，正好是点名要的四支：天蓝、浅青、粉红、白。
    EMPTY_CARDS = (
        ("code", "代码与项目", "检查、测试、修复", "SKY"),
        ("research", "研究与检索", "资料、来源、摘要", "CYAN"),
        ("files", "文件与知识", "阅读、整理、归档", "PINK"),
        ("plan", "日程与计划", "提醒、计划、跟进", "WHITE"),
    )
    EMPTY_TITLE = "今天想一起完成什么？"
    CARD_ICONS = {
        "code": "<>",
        "research": "\u2315",
        "files": "\u25a4",
        "plan": "\u25a6",
    }
    # 首页信息流太矮会挤掉消息区，低于这个高度就先不摆。
    FEED_MIN_HEIGHT = 520
    # 窗口矮到这个份上，四张能力卡先收起来，把地方还给消息区。
    CARDS_MIN_HEIGHT = 420
    # 收起后要再矮一点才决定不摆，避免在临界高度反复闪动。
    CARDS_HIDE_HEIGHT = 380
    # 左侧导航固定这些入口，顺序按 V8 规范，不许动。
    NAV_ITEMS = (
        ("home", "首页"),
        ("new", "新建对话"),
        ("sessions", "最近会话"),
        ("search", "搜索历史"),
        ("calendar", "日历日程"),
        ("ideas", "灵感建议"),
        ("goals", "目标与追踪"),
        ("work", "工作记录"),
        ("tasks", "任务中心"),
        ("knowledge", "文件与知识"),
        ("skills", "Skills"),
        ("persona", "人格与记忆"),
        ("capabilities", "本地能力与模型"),
        ("devices", "设备中心"),
        ("awareness", "屏幕感知"),
        ("settings", "权限与设置"),
    )
    NAV_ICONS = {
        "home": "\u2302",
        "new": "\u271a",
        "search": "\u2315",
        "sessions": "\u2630",
        "calendar": "\u25a6",
        "ideas": "\u25c8",
        "goals": "\u25ce",
        "work": "\u25f4",
        "tasks": "\u2714",
        "skills": "\u2726",
        "knowledge": "\u25a4",
        "persona": "\u25c7",
        "capabilities": "\u25e7",
        "devices": "\u25a3",
        "awareness": "\u25c9",
        "settings": "\u2699",
    }
    # 左侧导航的分组标题，按定稿图的版式摆；键是按规范排好的入口名。
    NAV_GROUPS = {
        "home": "工作空间",
        "sessions": "最近对话",
        "settings": "系统",
    }
    STEP_MARKS = {
        "done": "\u2714",
        "running": "\u25cf",
        "waiting": "\u2026",
        "failed": "\u2715",
        "todo": "\u25cb",
    }
    ATTACH_STATES = {
        "pending": "待解析",
        "reading": "解析中",
        "ok": "已解析",
        "error": "解析失败",
    }

    def __init__(
        self,
        parent,
        on_send=None,
        on_cancel=None,
        on_clear=None,
        on_settings=None,
        on_close=None,
        wraplength=320,
        logger=None,
        on_attach=None,
        on_skills=None,
        on_tasks=None,
        on_session=None,
        on_new_session=None,
        on_toggle_sidebar=None,
        on_event=None,
        timestamps=True,
    ):
        self.parent = parent
        self.on_send = on_send
        self.on_cancel = on_cancel
        self.on_clear = on_clear
        self.on_settings = on_settings
        self.on_close = on_close
        self.on_attach = on_attach
        self.on_skills = on_skills
        self.on_tasks = on_tasks
        self.on_session = on_session
        self.on_new_session = on_new_session
        self.on_toggle_sidebar = on_toggle_sidebar
        self.on_event = on_event
        self.logger = logger
        self.wraplength = wraplength
        self.timestamps = bool(timestamps)
        self.sidebar_visible = False
        self._sidebar_mode = None
        self._sessions = []
        self._layout_job = None
        self._layout_size = None
        self._fit_job = None
        self._density_job = None
        self._destroyed = False
        self._empty_shown = True
        # 导航收没收起、右面板开没开，都由宿主持久化。
        self.nav_collapsed = False
        self.panel_shown = False
        self.commands = []
        # 流式回复：先起一个气泡，边生成边补字，收尾时按最终正文重画一遍。
        self._streaming = False
        self._stream_start = "1.0"
        self._build()
        self._configure_bind = None
        try:
            self._configure_bind = self.parent.bind(
                "<Configure>", self._on_configure, add="+"
            )
        except Exception:
            pass
        try:
            self._wrap_bind = self.wrap.bind(
                "<Configure>", self._on_content_configure, add="+"
            )
        except Exception:
            self._wrap_bind = None
        try:
            self._destroy_bind = self.parent.bind(
                "<Destroy>", self._on_parent_destroy, add="+"
            )
        except Exception:
            self._destroy_bind = None

    # ---------- 搭建 ----------
    def _stash_chat_area(self):
        """把聊天区收起来：记住原来的排布，回来时原样摊开。"""
        self._stashed_chat = []
        for widget in self.main.pack_slaves():
            if widget is self.page_host:
                continue
            info = widget.pack_info()
            keep = {k: info[k] for k in ("side", "fill", "expand", "anchor", "padx", "pady") if k in info}
            self._stashed_chat.append((widget, keep))
            widget.pack_forget()
        return len(self._stashed_chat)

    def _restore_chat_area(self):
        for widget, info in self._stashed_chat:
            try:
                widget.pack(**info)
            except Exception:
                continue
        self._stashed_chat = []
        return True

    def open_page(self, key, title=""):
        """打开中央功能页。已经开着同一页就只换标题。"""
        if not self._page_open:
            self._stash_chat_area()
            self._page_open = True
            self.page_host.pack(side="top", fill="both", expand=True)
            try:
                self.empty.place_forget()
            except Exception:
                pass
        self._empty_shown = False
        self.page_key = str(key or "")
        try:
            self.page_title.configure(text=str(title or ""))
        except Exception:
            pass
        return self.page_body

    def register_page(self, key, frame):
        """把一页挂进内容区，其它页先收起来。"""
        self.page_key = str(key or self.page_key)
        for child in list(self.page_body.winfo_children()):
            if child is frame:
                continue
            if str(child.winfo_manager()) == "pack":
                child.pack_forget()
        try:
            if str(frame.winfo_manager()) != "pack":
                frame.pack(fill="both", expand=True)
        except Exception:
            return None
        theme.bind_wheel(frame, getattr(self.page_body, "canvas", None))
        return frame

    def close_page(self):
        """关掉功能页，回到聊天。"""
        if not self._page_open:
            return False
        self._page_open = False
        self.page_key = ""
        try:
            self.page_host.pack_forget()
        except Exception:
            pass
        self._restore_chat_area()
        self.set_nav_active("chat")
        self.show_empty(bool(self._page_wanted_empty))
        return True

    def page_is_open(self):
        return bool(self._page_open)
    # ---------- 尺寸自适应 ----------
    def _on_configure(self, event=None):
        """window resized: recompute wrap, nav, panel and gutters."""
        if getattr(self, "_destroyed", False):
            return None
        if event is not None and event.widget is not self.parent:
            return None
        if event is None:
            self._cancel_layout_job()
            return self._run_layout()
        try:
            if not self.nav.winfo_exists():
                return None
        except Exception:
            return None
        try:
            width = int(self.parent.winfo_width())
            height = int(self.parent.winfo_height())
        except Exception:
            return None
        if width <= 1 or height <= 1:
            return None
        size = (width, height)
        if size == self._layout_size and self._layout_job is None:
            return None
        self._schedule_layout()
        return None

    def _schedule_layout(self):
        if getattr(self, "_destroyed", False):
            return None
        self._cancel_layout_job()
        try:
            self._layout_job = self.parent.after_idle(self._flush_layout)
        except Exception:
            self._layout_job = None
        return self._layout_job

    def _cancel_layout_job(self):
        job = getattr(self, "_layout_job", None)
        if job is None:
            return None
        try:
            self.parent.after_cancel(job)
        except Exception:
            pass
        self._layout_job = None
        return None

    def _flush_layout(self):
        self._layout_job = None
        try:
            if not self.parent.winfo_exists():
                return None
        except Exception:
            return None
        return self._run_layout()

    def _run_layout(self):
        """Apply one settled geometry pass; Configure only schedules this."""
        try:
            if not self.nav.winfo_exists():
                return None
            width = int(self.parent.winfo_width())
            height = int(self.parent.winfo_height())
        except Exception:
            return None
        if width <= 1 or height <= 1:
            return None
        self._layout_size = (width, height)
        self._apply_nav()
        self._apply_panel()
        self._apply_sidebar()
        self._apply_feed_box()
        self._apply_cards_box()
        inner = max(self.MIN_WRAP, width - self._chrome_width() - theme.PAD_MD * 2 - theme.PAD_SM)
        try:
            self.status.configure(wraplength=inner)
            self.task_label.configure(wraplength=max(self.MIN_WRAP, inner - theme.PAD_SM * 2))
            self.perm_label.configure(wraplength=max(self.MIN_WRAP, inner - theme.PAD_SM * 4))
            self.empty_text.configure(wraplength=min(inner, theme.CONTENT_MAX_WIDTH))
            self._schedule_fit_placeholder()
            self.input.set_min_lines(
                self.input.WORKBENCH_MIN_LINES if width >= self.WORKBENCH_WIDTH else 1
            )
            self._apply_text_width(inner)
            self._apply_composer_width(inner)
        except Exception:
            return None
        self._schedule_density()
        return inner

    def _on_content_configure(self, event=None):
        if getattr(self, "_destroyed", False):
            return None
        if event is not None and getattr(event, "widget", None) is not self.wrap:
            return None
        return self._schedule_density()

    def _schedule_density(self):
        if getattr(self, "_destroyed", False):
            return None
        old = getattr(self, "_density_job", None)
        if old is not None:
            try:
                self.parent.after_cancel(old)
            except Exception:
                pass
        try:
            self._density_job = self.parent.after_idle(self._apply_density)
        except Exception:
            self._density_job = None
        return self._density_job

    def _apply_density(self):
        self._density_job = None
        if not getattr(self, "_empty_shown", False):
            return None
        try:
            if not self.parent.winfo_exists():
                return None
        except Exception:
            return None
        self._apply_feed_box()
        return None

    def _schedule_fit_placeholder(self):
        if getattr(self, "_destroyed", False):
            return None
        old = getattr(self, "_fit_job", None)
        if old is not None:
            try:
                self.parent.after_cancel(old)
            except Exception:
                pass
        try:
            self._fit_job = self.parent.after_idle(self._fit_placeholder_now)
        except Exception:
            self._fit_job = None
        return self._fit_job

    def _fit_placeholder_now(self):
        self._fit_job = None
        try:
            if not self.parent.winfo_exists() or not self.input.winfo_exists():
                return None
        except Exception:
            return None
        return self.input.fit_placeholder()

    def _apply_composer_width(self, inner):
        gutter = max(0, int((inner - theme.CONTENT_MAX_WIDTH) / 2))
        pad = theme.PAD_MD + gutter
        for widget in (self.field, self.input_row, self.attach_box, self.action_bar):
            try:
                if str(widget.winfo_manager()) == "pack":
                    widget.pack_configure(padx=pad)
            except Exception:
                continue
        return gutter

    def _chrome_width(self):
        nav = theme.NAV_COLLAPSED_WIDTH if self.nav_collapsed else theme.NAV_WIDTH
        try:
            panel = str(self.panel.winfo_manager()) == "pack"
        except Exception:
            panel = bool(self.panel_shown)
        extra = theme.PANEL_WIDTH if panel else 0
        if self._sidebar_mode == "pinned":
            extra += self.SIDEBAR_WIDTH
        return nav + extra

    def _apply_text_width(self, inner):
        """窗口很宽时给消息两侧留白：正文不铺满整行，气泡也跟着内缩。"""
        gutter = max(0, int((inner - self.MAX_TEXT_WIDTH) / 2))
        pad = theme.BUBBLE_PAD
        left = pad + gutter
        # 自己说的话靠右站：气泡左边线从这儿起，短句就是右侧一个小气泡。
        me_left = left
        if inner - left * 2 > self.ME_BUBBLE_WIDTH:
            me_left = inner - left - self.ME_BUBBLE_WIDTH
        for tag in list(theme.MESSAGE_STYLES) + ["code", "code_lang", "stamp"]:
            margin = me_left if tag == "me" else left
            try:
                self.transcript.tag_config(
                    tag,
                    lmargin1=margin,
                    lmargin2=margin,
                    rmargin=left,
                )
            except Exception:
                continue
        return gutter

    def _stamp(self):
        return time.strftime("%H:%M")

    # ---------- 主题 ----------
    def apply_theme(self):
        """就地换皮。文字内容和状态都不动，只改颜色与字体。"""
        self.parent.configure(bg=theme.BG)
        self.bar.configure(bg=theme.TOPBAR_BG)
        self.brand.configure(bg=theme.TOPBAR_BG, fg=theme.ACCENT)
        self.brand_icon.configure(bg=theme.TOPBAR_BG, fg=theme.ACCENT)
        self.title.configure(bg=theme.TOPBAR_BG, fg=theme.MUTED)
        self.mode_label.configure(bg=theme.TOPBAR_BG, fg=theme.MUTED)
        self.body.configure(bg=theme.BG)
        self.nav.configure(bg=theme.SIDEBAR_BG, highlightbackground=theme.BORDER)
        self.panel.configure(bg=theme.CARD, highlightbackground=theme.BORDER)
        self.panel_title.configure(bg=theme.CARD, fg=theme.TEXT)
        self.panel_text.configure(bg=theme.CARD, fg=theme.TEXT, insertbackground=theme.TEXT)
        self.main.configure(bg=theme.BG)
        self.side.configure(bg=theme.SIDEBAR_BG, highlightbackground=theme.BORDER)
        self.side_title.configure(bg=theme.SIDEBAR_BG, fg=theme.MUTED)
        self.session_list.configure(
            bg=theme.SIDEBAR_BG,
            fg=theme.TEXT,
            selectbackground=theme.SIDEBAR_ACTIVE,
            selectforeground=theme.TEXT,
        )
        self.transcript.configure(bg=theme.FIELD, fg=theme.TEXT, insertbackground=theme.TEXT)
        self.field.configure(bg=theme.FIELD, highlightbackground=theme.BORDER, highlightcolor=theme.ACCENT)
        self.input.apply_theme()
        self.composer.configure(bg=theme.BG)
        self.attach_box.configure(bg=theme.BG)
        self.action_bar.configure(bg=theme.BG)
        self.input_row.configure(bg=theme.BG)
        self.task_box.configure(bg=theme.BUBBLE_SYS)
        self.task_label.configure(bg=theme.BUBBLE_SYS, fg=theme.WARN)
        self.perm_box.configure(bg=theme.BUBBLE_SYS)
        self.perm_label.configure(bg=theme.BUBBLE_SYS, fg=theme.WARN)
        self.status.configure(bg=theme.BG)
        self.state.configure(bg=theme.BG)
        self.grip.configure(bg=theme.BG)
        self.grip_label.configure(bg=theme.BG)
        self.empty.configure(bg=theme.BG)
        self.empty_icon.configure(bg=theme.BG, fg=theme.ACCENT)
        self.empty_title.configure(bg=theme.BG, fg=theme.TEXT)
        self.empty_text.configure(bg=theme.BG, fg=theme.MUTED)
        self.empty_actions.configure(bg=theme.BG)
        self._recolor_tree(self.bar)
        self._recolor_tree(self.nav)
        self._recolor_tree(self.panel)
        self._recolor_tree(self.side)
        self._recolor_tree(self.main)
        self._recolor_tree(self.composer)
        self._recolor_tree(self.empty)
        self._paint_empty_cards()
        self._paint_nav_highlight()
        self._paint_transcript_bg()
        # 换皮会把留白重置回默认值，这里再铺一次。
        self._on_configure()
        return theme.active()

    def _frame_bg(self, widget):
        """每个容器的底色由它在界面里的位置决定，不能一律刷成主背景。"""
        if widget is self.field:
            return theme.FIELD
        if widget is self.task_box or widget is self.perm_box:
            return theme.BUBBLE_SYS
        master = getattr(widget, "master", None)
        if master is self.bar:
            return theme.TOPBAR_BG
        if master is self.nav or master is self.side:
            return theme.SIDEBAR_BG
        if master is self.panel:
            return theme.CARD
        if widget in getattr(self, "empty_cards", {}).values():
            return theme.CARD
        return theme.BG

    def _paint_empty_cards(self):
        """四张卡的图标各用一种点缀色：天蓝、浅青、粉红、白。"""
        for _key, (icon, colour) in getattr(self, "empty_card_icons", {}).items():
            try:
                icon.configure(bg=theme.CARD, fg=getattr(theme, colour, theme.ACCENT))
            except Exception:
                continue

    def _paint_transcript_bg(self):
        """消息区空着时把它融进主背景，别在首屏留一块空框。"""
        try:
            blank = not self.transcript.get("1.0", "end-1c").strip()
            bg = theme.BG if (blank and self.is_empty_shown()) else theme.FIELD
            self.transcript.configure(bg=bg)
            theme.style_messages(self.transcript, margin=bg)
        except Exception:
            return None
        return bg

    def _paint_nav_highlight(self):
        """「新建对话」是导航顶上的主按钮，换皮之后还得亮着。"""
        widgets = [self.nav_rows.get("new"), self.nav_icons.get("new"),
                   self.nav_texts.get("new")]
        for widget in widgets:
            if widget is None:
                continue
            try:
                widget.configure(bg=theme.SIDEBAR_ACTIVE)
            except Exception:
                continue
        icon = self.nav_icons.get("new")
        if icon is not None:
            try:
                icon.configure(fg=theme.ACCENT)
            except Exception:
                pass

    def _card_frames(self):
        return tuple(getattr(self, "empty_cards", {}).values())

    def _recolor_tree(self, widget):
        """把子控件按类型重新上色。认不出的控件原样跳过。"""
        cards = self._card_frames()
        for child in list(widget.winfo_children()):
            try:
                if isinstance(child, tk.Button):
                    kind = getattr(child, "_theme_kind", "ghost")
                    bg, fg, active_bg, active_fg = theme._BUTTON_KINDS.get(
                        kind, theme._BUTTON_KINDS["ghost"]
                    )
                    child.configure(
                        bg=bg, fg=fg, activebackground=active_bg, activeforeground=active_fg
                    )
                elif isinstance(child, tk.Label):
                    base = theme.BG
                    master = child.master
                    if master is self.side or master is self.nav:
                        base = theme.SIDEBAR_BG
                    elif master is self.bar:
                        base = theme.TOPBAR_BG
                    elif master is self.panel:
                        base = theme.CARD
                    elif master in cards:
                        base = theme.CARD
                    elif child is self.task_label or child is self.perm_label:
                        base = theme.BUBBLE_SYS
                    child.configure(bg=base)
                elif isinstance(child, tk.Frame):
                    child.configure(bg=self._frame_bg(child))
                self._recolor_tree(child)
            except Exception:
                continue

    # ---------- 会话抽屉 ----------
    def set_sessions(self, sessions, active_id=""):
        """sessions: [(session_id, label)]。列表里标出当前会话。"""
        self._sessions = list(sessions or [])
        self.session_list.delete(0, tk.END)
        for session_id, label in self._sessions:
            mark = "* " if session_id == active_id else "  "
            self.session_list.insert(tk.END, mark + str(label))
        return len(self._sessions)

    def show_sidebar(self, flag=True):
        """窗口宽就占一列，窗口窄就浮在消息上面，两种情况都不遮住输入区。"""
        before = self._sidebar_mode
        self.sidebar_visible = bool(flag)
        mode = self._apply_sidebar()
        if mode != before:
            self._schedule_layout()
        return self.sidebar_visible

    def _apply_sidebar(self):
        try:
            width = int(self.parent.winfo_width())
        except Exception:
            width = 0
        if not self.sidebar_visible:
            mode = "hidden"
        elif width >= self.SIDEBAR_PIN_WIDTH:
            mode = "pinned"
        else:
            mode = "overlay"
        if mode == self._sidebar_mode:
            return mode
        self._sidebar_mode = mode
        try:
            if mode == "hidden":
                self.side.place_forget()
                self.side.pack_forget()
            elif mode == "pinned":
                self.side.place_forget()
                self.side.pack(side="left", fill="y", before=self.main)
            else:
                self.side.pack_forget()
                # 浮层贴在导航右边，别把左边这条常驻导航挡住。
                try:
                    offset = max(0, int(self.nav.winfo_width()))
                except Exception:
                    offset = theme.NAV_COLLAPSED_WIDTH
                self.side.place(x=offset, y=0, relheight=1.0, width=self.SIDEBAR_WIDTH)
                self.side.lift()
        except Exception as exc:
            if self.logger:
                self.logger.warning("sidebar layout failed: %s", exc)
        return mode

    def sidebar_mode(self):
        return self._sidebar_mode

    def toggle_sidebar(self):
        shown = self.show_sidebar(not self.sidebar_visible)
        if self.on_toggle_sidebar is not None:
            try:
                self.on_toggle_sidebar(shown)
            except Exception as exc:
                if self.logger:
                    self.logger.warning("sidebar toggle failed: %s", exc)
        return shown

    def _session_picked(self, event=None):
        if self.on_session is None:
            return None
        selection = self.session_list.curselection()
        if not selection:
            return None
        index = int(selection[0])
        if index >= len(self._sessions):
            return None
        return self.on_session(self._sessions[index][0])

    def _new_session(self):
        if self.on_new_session is not None:
            return self.on_new_session()
        return None

    def detach(self):
        """重建界面前先把挂在外层窗口上的回调拆掉，避免旧对象引用已销毁的控件。"""
        self._destroyed = True
        self._cancel_layout_job()
        job = getattr(self, "_density_job", None)
        if job is not None:
            try:
                self.parent.after_cancel(job)
            except Exception:
                pass
            self._density_job = None
        job = getattr(self, "_fit_job", None)
        if job is not None:
            try:
                self.parent.after_cancel(job)
            except Exception:
                pass
            self._fit_job = None
        job = getattr(self, "_nav_leave_job", None)
        if job is not None:
            try:
                self.parent.after_cancel(job)
            except Exception:
                pass
            self._nav_leave_job = None
        bind_id = getattr(self, "_configure_bind", None)
        if bind_id:
            try:
                self.parent.unbind("<Configure>", bind_id)
            except Exception:
                pass
        self._configure_bind = None
        bind_id = getattr(self, "_wrap_bind", None)
        if bind_id:
            try:
                self.wrap.unbind("<Configure>", bind_id)
            except Exception:
                pass
        self._wrap_bind = None
        bind_id = getattr(self, "_destroy_bind", None)
        if bind_id:
            try:
                self.parent.unbind("<Destroy>", bind_id)
            except Exception:
                pass
        self._destroy_bind = None
        return True

    def _on_parent_destroy(self, event=None):
        if event is not None and getattr(event, "widget", None) is not self.parent:
            return None
        self.detach()
        return None

    # ---------- 空状态 ----------
    def show_empty(self, flag=True):
        flag = bool(flag)
        try:
            manager = self.empty.winfo_manager()
        except Exception:
            return flag
        self._page_wanted_empty = flag
        self._empty_shown = bool(flag and not self._page_open)
        if self._page_open:
            # 功能页占着中央区域，聊天区先让开；关页面时再按记下的意图恢复。
            if manager != "":
                self.empty.place_forget()
                self.empty.pack_forget()
            return flag
        if flag:
            # 欢迎页浮在消息区上，不再按自身内容高度挤压聊天区。
            if manager == "pack":
                self.empty.pack_forget()
            if manager != "place":
                self.empty.place(
                    in_=self.wrap, x=0, y=0, relwidth=1.0, relheight=1.0
                )
            self.empty.lift()
        elif manager != "":
            self.empty.place_forget()
            self.empty.pack_forget()
        self._apply_action_bar()
        self._apply_feed_box()
        self._paint_transcript_bg()
        return flag

    def is_empty_shown(self):
        return bool(getattr(self, "_empty_shown", False))

    def set_greeting(self, text):
        text = str(text or "").strip() or self.DEFAULT_GREETING
        self.empty_text.config(text=text)
        return text

    # ---------- 任务面板 ----------
    def set_task(self, text, active=True):
        text = str(text or "").strip()
        if not text:
            self.task_box.pack_forget()
            return False
        self.task_label.config(text=text)
        self.task_box.pack(side="bottom", fill="x", padx=theme.PAD_MD, pady=(theme.PAD_XS, 0))
        if active:
            self.task_cancel.pack(side="right", padx=theme.PAD_XS, pady=theme.PAD_XS)
        else:
            self.task_cancel.pack_forget()
        return True

    def task_text(self):
        return str(self.task_label.cget("text") or "")

    # ---------- 内容 ----------
    def append(self, tag, text, stamp=False):
        """换行不带消息标签，否则气泡底色会铺满整行，看不出靠左还是靠右。"""
        if tag in ("me", "pet", "task"):
            self.show_empty(False)
        self.transcript.config(state="normal")
        body = str(text or "")
        # 两侧各留一个空格当内边距，气泡里的字不贴边。
        self.transcript.insert("end", " %s " % body, tag)
        if stamp and self.timestamps and tag in ("me", "pet"):
            self.transcript.insert("end", "   " + self._stamp(), "stamp")
        self.transcript.insert("end", "\n")
        # 消息之间留一条空隙，连续回复不再糊成一整块。
        self.transcript.insert("end", "\n", "gap")
        self.transcript.see("end")
        self.transcript.config(state="disabled")
        self._apply_action_bar()
        return text

    def append_markdown(self, tag, text, stamp=True):
        """把围栏代码块单独画出来，其余部分仍走气泡标签。

        返回值表示「这段正文已经画过了」，调用方不要再补一次。
        """
        text = str(text or "")
        if not text:
            return False
        if tag in ("me", "pet", "task"):
            self.show_empty(False)
        position = 0
        wrote = False
        for match in CODE_FENCE.finditer(text):
            head = text[position : match.start()]
            if head.strip():
                # 代码块前后的空行别带进消息标签，不然会多出一条整行宽的底色。
                self.append(tag, head.strip())
                wrote = True
            self.append_code(match.group(2), match.group(1))
            wrote = True
            position = match.end()
        tail = text[position:]
        if position == 0:
            self.append(tag, text, stamp=stamp)
            wrote = True
        elif tail.strip():
            self.append(tag, tail.strip(), stamp=stamp)
            wrote = True
        elif stamp:
            self.append("hint", self._stamp())
            wrote = True
        return wrote

    # ---------- 流式回复 ----------
    def begin_stream(self, speaker="桌宠"):
        """开一条流式气泡。先写好说话人，用户立刻能看到"正在说"。"""
        self.show_empty(False)
        self.transcript.config(state="normal")
        self._stream_start = self.transcript.index("end-1c")
        self.transcript.insert("end", "%s：" % speaker, "pet")
        self.transcript.see("end")
        self.transcript.config(state="disabled")
        self._streaming = True
        return self._stream_start

    def push_stream(self, text):
        """往流式气泡里补一段字。没有开头就不补，避免散落在历史里。"""
        text = str(text or "")
        if not self._streaming or not text:
            return ""
        self.transcript.config(state="normal")
        self.transcript.insert("end", text, "pet")
        self.transcript.see("end")
        self.transcript.config(state="disabled")
        return text

    def end_stream(self, body=None, tag="pet"):
        """收尾：把流式草稿擦掉，按最终正文重画，保证正文和存储一致。

        body 为空就什么都不留（例如被取消、或者模型最终没给内容）。
        """
        if not self._streaming:
            return False
        self._streaming = False
        try:
            self.transcript.config(state="normal")
            self.transcript.delete(self._stream_start, "end-1c")
            self.transcript.config(state="disabled")
        except tk.TclError:
            return False
        text = "" if body is None else str(body)
        if text.strip():
            if not self.append_markdown(tag, text):
                self.append(tag, text, stamp=True)
        return True

    def streaming(self):
        return self._streaming

    def append_code(self, code, language=""):
        self.show_empty(False)
        self.transcript.config(state="normal")
        if language:
            self.transcript.insert("end", "代码（%s）" % language, "code_lang")
            self.transcript.insert("end", "\n")
        block = str(code).rstrip("\n")
        self.transcript.insert("end", block, "code")
        # 换行留在标签外面，否则代码块底色会多铺一整行空的。
        self.transcript.insert("end", "\n")
        self.transcript.see("end")
        self.transcript.config(state="disabled")
        return block

    def history_text(self):
        return self.transcript.get("1.0", "end")

    def locate(self, needle):
        """在历史里定位一段文字：选中它并滚过去。找不到返回 False。"""
        needle = str(needle or "").strip()
        if not needle:
            return False
        self.transcript.tag_remove("search_hit", "1.0", "end")
        index = self.transcript.search(needle, "1.0", stopindex="end")
        if not index:
            return False
        end = "%s+%dc" % (index, len(needle))
        self.transcript.tag_add("search_hit", index, end)
        self.transcript.see(index)
        return True

    def clear(self):
        self.transcript.config(state="normal")
        self.transcript.delete("1.0", "end")
        self.transcript.config(state="disabled")
        self._apply_action_bar()
        self.set_task("", active=False)
        self.set_task_steps([])
        self.set_state(IDLE, "")
        self.show_empty(True)

    def pack_grip(self):
        self.grip.pack(fill="x", side="bottom")

    # ---------- 输入 ----------
    def read_input(self):
        return self.input.get().strip()

    def clear_input(self):
        self.input.show_placeholder()
        return ""

    def insert_input(self, text):
        self.input.clear_placeholder()
        self.input.insert(tk.END, str(text or ""))
        self.input._sync_height()
        return self.input.get()

    def focus_input(self):
        try:
            self.input.focus_set()
        except Exception:
            return None
        return True

    # ---------- 状态 ----------
    def set_status(self, text):
        self.status.config(text=text or "")
        return text

    def set_state(self, kind, text):
        self.state.config(text=text or "", fg=theme.STATE_COLORS.get(kind, theme.MUTED))
        return text

    def set_busy(self, busy):
        busy = bool(busy)
        self._busy = busy
        self._apply_action_bar()
        self.send_button.config(state="disabled" if busy else "normal")
        if busy:
            self.retry_button.pack_forget()
            self.cancel_button.pack(side="right", padx=(0, theme.PAD_SM))
        else:
            self.cancel_button.pack_forget()
        return busy

    def show_retry(self, show=True, retry_text="重试"):
        self.retry_button.config(text=retry_text)
        if show:
            self.retry_button.pack(side="right", padx=(0, theme.PAD_SM))
        else:
            self.retry_button.pack_forget()
        return bool(show)

    def busy(self):
        return str(self.send_button.cget("state")) == "disabled"

    # ---------- 剪贴板 ----------
    def copy_selection(self):
        try:
            text = self.transcript.get("sel.first", "sel.last")
        except tk.TclError:
            text = ""
        if text:
            self._to_clipboard(text)
        return text

    def copy_all(self):
        text = self.history_text().strip()
        if text:
            self._to_clipboard(text)
        return text

    # ---------- 单条消息 ----------
    def _line_tag(self, line):
        """某一行属于哪一类消息。空行、时间戳行返回空字符串。"""
        try:
            names = self.transcript.tag_names("%d.0" % int(line))
        except tk.TclError:
            return ""
        for tag in ("me", "pet", "task", "notice", "proactive", "err", "hint", "code", "code_lang"):
            if tag in names:
                return "code" if tag == "code_lang" else tag
        return ""

    def message_at(self, index):
        """定位右键点到的那条消息：把同一标签的相邻行合成一块。

        返回 (类别, 正文, 首行, 末行)。点在空白处返回空。
        """
        try:
            line = int(str(index or "").split(".")[0])
            total = int(self.transcript.index("end-1c").split(".")[0])
        except (TypeError, ValueError, tk.TclError):
            return "", "", 0, 0
        tag = self._line_tag(line) if 1 <= line <= total else ""
        if not tag:
            return "", "", 0, 0
        first = last = line
        while first > 1 and self._line_tag(first - 1) == tag:
            first -= 1
        while last < total and self._line_tag(last + 1) == tag:
            last += 1
        text = self.transcript.get("%d.0" % first, "%d.end" % last).strip()
        return tag, text, first, last

    def can_regenerate(self, tag, last_line):
        """只有最后一条回复能重来。旧回复重来会把后面的对话搅乱。"""
        if tag != "pet" or self._streaming:
            return False
        ranges = self.transcript.tag_ranges("pet")
        if not ranges:
            return False
        return int(str(ranges[-1]).split(".")[0]) <= int(last_line or 0)

    def copy_message_at(self, index=None):
        """复制这一条，代码块一起带上。"""
        line = self._menu_line if index is None else index
        _tag, text, _first, _last = self.message_at(line)
        if text:
            self._to_clipboard(text)
        return text

    def regenerate_message_at(self, index=None):
        """重发上一句，等价于再想一次。不是最后一条就什么都不做。"""
        line = self._menu_line if index is None else index
        tag, _text, _first, last = self.message_at(line)
        if not self.can_regenerate(tag, last):
            return False
        self._send()
        return True

    def _menu_state(self):
        """按点中的位置决定菜单哪些能点。"""
        tag, text, _first, last = self.message_at(self._menu_line)
        try:
            self.menu.entryconfig("复制这条", state="normal" if text else "disabled")
            self.menu.entryconfig(
                "重新生成这条",
                state="normal" if self.can_regenerate(tag, last) else "disabled",
            )
        except tk.TclError:
            pass

    def _to_clipboard(self, text):
        try:
            self.parent.clipboard_clear()
            self.parent.clipboard_append(text)
        except Exception as exc:
            if self.logger:
                self.logger.warning("clipboard failed: %s", exc)
            return False
        return True

    def _popup(self, event):
        try:
            self._menu_line = self.transcript.index("@%d,%d" % (event.x, event.y))
        except tk.TclError:
            self._menu_line = None
        self._menu_state()
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    # ---------- 回调 ----------
    def _send(self):
        if self.on_send is not None:
            self.on_send()

    def _retry(self):
        if self.on_send is not None:
            self.on_send()

    def _cancel(self):
        if self.on_cancel is not None:
            self.on_cancel()

    def _clear(self):
        if self.on_clear is not None:
            self.on_clear()

    def _tasks(self):
        if self.on_tasks is not None:
            return self.on_tasks()
        return None

    def _settings(self):
        if self.on_settings is not None:
            self.on_settings()

    def _close(self):
        if self.on_close is not None:
            self.on_close()

    def toggle_tools(self, force=None):
        """「＋」：收起或展开分组工具面板。"""
        want = (not self.tool_open) if force is None else bool(force)
        self.tool_open = bool(want)
        try:
            if self.tool_open:
                self.tool_box.pack(fill="x", before=self.input_row)
            else:
                self.tool_box.pack_forget()
        except Exception:
            pass
        return self.tool_open

    def tools_open(self):
        return bool(self.tool_open)

    def _attach(self):
        if self.on_attach is not None:
            return self.on_attach()
        return None

    def _attach_image(self):
        return self._event("attach_image")

    def _knowledge(self):
        return self._event("knowledge")

    def _skills(self):
        if self.on_skills is not None:
            return self.on_skills()
        return None

    # ---------- 左侧导航 ----------
    def toggle_nav(self, force=None):
        self.nav_expanded = (not self.nav_expanded) if force is None else bool(force)
        # 点击是"固定展开/收起"：写偏好；悬停只是临时预览。
        self.nav_pinned = self.nav_expanded
        self._nav_hover = False
        self._apply_nav()
        self._schedule_layout()
        self._event("nav_toggle", self.nav_expanded)
        return self.nav_expanded

    def _apply_nav(self):
        try:
            if not self.nav.winfo_exists():
                return self.nav_collapsed
        except Exception:
            return self.nav_collapsed
        try:
            width = int(self.parent.winfo_width())
        except Exception:
            width = 0
        narrow = 0 < width < self.NAV_AUTO_COLLAPSE_WIDTH
        pinned_expanded = bool(getattr(self, "nav_pinned", self.nav_expanded)) and not narrow
        # 悬停只是临时铺开：导航浮在内容上，中央区不跟着动。
        expanded = pinned_expanded or (bool(getattr(self, "_nav_hover", False)) and not narrow)
        collapsed = not expanded
        self.nav_collapsed = collapsed
        nav_width = theme.NAV_WIDTH if expanded else theme.NAV_COLLAPSED_WIDTH
        if nav_width != getattr(self, "_nav_width_applied", None):
            try:
                self.nav.place_configure(width=nav_width)
                self.nav.lift()
            except Exception as exc:
                if self.logger:
                    self.logger.warning("nav width failed: %s", exc)
            self._nav_width_applied = nav_width
        # 固定展开时中央区让出一列；只悬停时不改，避免挤压和闪动。
        gap = theme.PAD_SM + 4
        main_pad = (theme.NAV_WIDTH if pinned_expanded else theme.NAV_COLLAPSED_WIDTH) + gap
        if main_pad != getattr(self, "_main_pad_applied", None):
            try:
                self.main.pack_configure(padx=(main_pad, 0))
            except Exception:
                pass
            self._main_pad_applied = main_pad
        try:
            self.nav.configure(width=nav_width)
            for key, text in self.nav_texts.items():
                if collapsed:
                    if str(text.winfo_manager()) == "pack":
                        text.pack_forget()
                elif str(text.winfo_manager()) != "pack":
                    text.pack(side="left", padx=(theme.PAD_XS, 0))
            # 收起时那一列放不下「工作空间」这种标题，留着只会被裁成半个字。
            for label in getattr(self, "nav_group_labels", {}).values():
                if collapsed:
                    if str(label.winfo_manager()) == "pack":
                        label.pack_forget()
                elif str(label.winfo_manager()) != "pack":
                    label.pack(
                        anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_SM, theme.PAD_XS)
                    )
            self._center_nav_icons(collapsed)
        except Exception as exc:
            if self.logger:
                self.logger.warning("nav layout failed: %s", exc)
        return collapsed

    # ---------- 悬停临时展开 ----------
    def _nav_enter(self, _event=None):
        job = getattr(self, "_nav_leave_job", None)
        if job is not None:
            try:
                self.parent.after_cancel(job)
            except Exception:
                pass
            self._nav_leave_job = None
        if not getattr(self, "nav_pinned", True) and not getattr(self, "_nav_hover", False):
            self._nav_hover = True
            self._apply_nav()
        return None

    def _nav_leave(self, _event=None):
        if getattr(self, "nav_pinned", True):
            return None
        if getattr(self, "_nav_leave_job", None) is not None:
            return None
        try:
            # 挂在会活下来的父窗口上：重建时导航会被销毁，回调本身还得能安全跑完。
            self._nav_leave_job = self.parent.after(140, self._nav_settle)
        except Exception:
            self._nav_leave_job = None
        return None

    def _nav_settle(self):
        """离开后等一下再收：指针从图标移到文字上时不该闪一下。"""
        self._nav_leave_job = None
        if getattr(self, "nav_pinned", True):
            return None
        try:
            if not self.nav.winfo_exists():
                return None
        except Exception:
            return None
        inside = False
        try:
            x, y = self.nav.winfo_pointerxy()
            widget = self.nav.winfo_containing(x, y)
        except Exception:
            widget = None
        node = widget
        while node is not None:
            if node is self.nav:
                inside = True
                break
            node = getattr(node, "master", None)
        if inside:
            return None
        if getattr(self, "_nav_hover", False):
            self._nav_hover = False
            self._apply_nav()
        return None

    def _center_nav_icons(self, collapsed):
        """收起时图标居中并加大一档，展开时靠左和文字对齐。"""
        for key, icon in getattr(self, "nav_icons", {}).items():
            try:
                icon.configure(font=theme.icon_font(5 if collapsed else 2))
            except Exception:
                pass
            if collapsed:
                try:
                    icon.pack_configure(side="top", fill="x", pady=theme.PAD_SM)
                except Exception:
                    continue
            else:
                try:
                    pad = theme.PAD_SM if key == "new" else theme.PAD_XS
                    icon.pack_configure(side="left", fill="", padx=(pad, 0), pady=theme.PAD_SM)
                except Exception:
                    continue
        return collapsed

    def nav_is_collapsed(self):
        return bool(self.nav_collapsed)

    def set_nav_active(self, key):
        key = str(key or "")
        for name, row in self.nav_rows.items():
            lit = name == key or name == "new"
            base = theme.SIDEBAR_ACTIVE if lit else theme.SIDEBAR_BG
            row.configure(bg=base)
            self.nav_icons[name].configure(
                bg=base, fg=theme.ACCENT if lit else theme.MUTED
            )
            self.nav_texts[name].configure(bg=base, fg=theme.TEXT)
        self.nav_active = key
        return key

    def _nav_clicked(self, key):
        if key == "sessions":
            return self.toggle_sidebar()
        self.set_nav_active(key)
        return self._event("nav", key)

    def _event(self, name, payload=None):
        if self.on_event is None:
            return None
        try:
            return self.on_event(name, payload)
        except Exception as exc:
            if self.logger:
                self.logger.warning("chat event %s failed: %s", name, exc)
            return None

    # ---------- 右侧任务面板 ----------
    def toggle_panel(self):
        return self.set_panel(not self.panel_shown)

    def set_panel(self, shown):
        self.panel_shown = bool(shown)
        self._apply_panel()
        self._schedule_layout()
        return self.panel_shown

    def panel_is_shown(self):
        return bool(self.panel_shown)

    def set_task_mode(self, flag):
        """任务模式：进来把右侧面板摆出来，按钮文字跟着变。"""
        self._task_mode = bool(flag)
        try:
            if self._task_mode:
                self.set_panel(True)
            self.mode_toggle.config(
                text="任务模式：开" if self._task_mode else "任务模式"
            )
        except Exception:
            pass
        return self._task_mode

    def task_mode(self):
        return bool(self._task_mode)

    def _apply_panel(self):
        try:
            width = int(self.parent.winfo_width())
        except Exception:
            width = 0
        manager = str(self.panel.winfo_manager())
        limit = (
            self.PANEL_HIDE_WINDOW_WIDTH
            if manager == "pack"
            else self.PANEL_MIN_WINDOW_WIDTH
        )
        wanted = self.panel_shown and (width == 0 or width >= limit)
        try:
            if wanted and manager != "pack":
                self.panel.pack(side="right", fill="y")
                self.set_panel_text(self._panel_cache)
            elif not wanted and manager != "":
                self.panel.pack_forget()
            self.panel_button.configure(text="\u25a5" if wanted else "\u25a4")
        except Exception as exc:
            if self.logger:
                self.logger.warning("panel layout failed: %s", exc)
        return wanted

    def set_panel_text(self, text):
        self._panel_cache = str(text or "").strip()
        body = self._panel_cache or self.panel_placeholder
        self.panel_text.configure(state="normal")
        self.panel_text.delete("1.0", "end")
        self.panel_text.insert("1.0", body)
        self.panel_text.configure(state="disabled")
        return body

    def panel_body(self):
        return self.panel_text.get("1.0", "end").strip()

    # ---------- 顶栏状态 ----------
    def set_mode(self, text):
        self.mode_label.configure(text=str(text or ""))
        return text

    def set_model(self, text):
        shown = str(text or "").strip() or "模型：未配置"
        self.model_button.configure(text=shown)
        return shown

    def set_hero_image(self, photo):
        """空会话顶部的背景横幅。传 None 就收起来。"""
        try:
            if photo is None:
                self.hero_banner.pack_forget()
                self.hero_banner.configure(image="")
                self._hero_photo = None
                return False
            self._hero_photo = photo
            self.hero_banner.configure(image=photo)
            if str(self.hero_banner.winfo_manager()) != "pack":
                self.hero_banner.pack(
                    fill="x",
                    padx=theme.PAD_MD,
                    pady=(0, theme.PAD_SM),
                    before=self.empty_hero,
                )
            return True
        except Exception:
            return False

    def model_text(self):
        return str(self.model_button.cget("text"))

    def _model_menu(self):
        return self._event("model_menu")

    def _search(self):
        return self._event("search")

    # ---------- 消息操作 ----------
    def _card_clicked(self, key):
        return self._event("card", key)

    def _status_detail(self):
        return self._event("status_detail")

    def _mode_toggle(self):
        return self._event("mode_toggle", not self._task_mode)

    def set_feed(self, cards):
        """首页信息流：每张卡是 {title, body, actions:[(文字, 动作)]}。

        actions 里以 open: 开头的是切界面，其余是填进输入框的话。
        内容全部由宿主从真实存储取，这里只负责摆。
        """
        for child in list(self.feed_box.winfo_children()):
            try:
                child.destroy()
            except Exception:
                continue
        self.feed_cards = []
        rows = list(cards or [])
        if not rows:
            self._apply_feed_box()
            return 0
        for card in rows:
            frame = tk.Frame(
                self.feed_box, bg=theme.CARD, highlightthickness=1,
                highlightbackground=theme.BORDER,
            )
            frame.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_SM, 0))
            theme.label(
                frame, str(card.get("title") or ""), kind="title", bg=theme.CARD, fg=theme.TEXT
            ).pack(anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_SM, 0))
            theme.label(
                frame, str(card.get("body") or ""), kind="small", bg=theme.CARD, fg=theme.MUTED,
                anchor="w", justify="left",
            ).pack(anchor="w", fill="x", padx=theme.PAD_MD)
            buttons = list(card.get("actions") or [])
            if buttons:
                line = tk.Frame(frame, bg=theme.CARD)
                line.pack(
                    anchor="w", fill="x", padx=theme.PAD_MD, pady=(theme.PAD_XS, theme.PAD_SM)
                )
                for label, body in buttons:
                    chip = theme.button(
                        line, label, lambda act=body: self._feed_action(act),
                        kind="chip", font=theme.FONT_SMALL,
                    )
                    chip.pack(side="left", padx=(0, theme.PAD_XS))
            self.feed_cards.append(frame)
        self._apply_feed_box()
        return len(self.feed_cards)

    def feed_titles(self):
        return [str(card.winfo_children()[0].cget("text")) for card in self.feed_cards]

    def _feed_action(self, action):
        """卡片上的按钮：open:xxx 切界面，dismiss:xxx 先不看了，其余填进输入框。"""
        body = str(action or "")
        if body.startswith("open:"):
            return self._event("open", body.split(":", 1)[1].strip())
        if body.startswith("dismiss:"):
            return self._event("dismiss", body.split(":", 1)[1].strip())
        return self._tip_clicked(body)

    def _apply_feed_box(self):
        """有内容、窗口也够高才摆，免得把消息区挤没。"""
        try:
            height = int(self.wrap.winfo_height())
            if height <= 1:
                height = int(self.parent.winfo_height())
        except Exception:
            height = 0
        roomy = height == 0 or height >= self.FEED_MIN_HEIGHT
        try:
            packed = str(self.feed_box.winfo_manager()) == "pack"
            if self.feed_cards and roomy and not packed:
                self.feed_box.pack(anchor="w", fill="x", before=self.empty_notes)
            elif (not self.feed_cards or not roomy) and packed:
                self.feed_box.pack_forget()
        except Exception:
            pass
        self._apply_cards_box()

    def _apply_cards_box(self):
        """窗口太矮就先把四张能力卡收起来，地方留给消息区和输入区。"""
        try:
            height = int(self.wrap.winfo_height())
            if height <= 1:
                height = int(self.parent.winfo_height())
        except Exception:
            height = 0
        try:
            packed = str(self.empty_cards_box.winfo_manager()) == "pack"
            limit = self.CARDS_HIDE_HEIGHT if packed else self.CARDS_MIN_HEIGHT
            roomy = height == 0 or height >= limit
            if roomy and not packed:
                anchor = None
                for widget in (self.feed_box, self.empty_notes):
                    if str(widget.winfo_manager()) == "pack":
                        anchor = widget
                        break
                if anchor is None:
                    self.empty_cards_box.pack(fill="x")
                else:
                    self.empty_cards_box.pack(fill="x", before=anchor)
            elif not roomy and packed:
                self.empty_cards_box.pack_forget()
        except Exception:
            pass

    def set_tips(self, items):
        """推荐操作：每项 (按钮文字, 要填进输入框的话)。空列表就整块不显示。"""
        for child in list(self.empty_notes.winfo_children()):
            try:
                child.destroy()
            except Exception:
                continue
        self.tip_chips = []
        rows = list(items or [])
        if not rows:
            self._apply_notes_box()
            return 0
        theme.label(self.empty_notes, "推荐操作", kind="small", fg=theme.MUTED, bg=theme.BG).pack(
            anchor="w", pady=(theme.PAD_SM, 0)
        )
        row = tk.Frame(self.empty_notes, bg=theme.BG)
        row.pack(anchor="w", fill="x", pady=(theme.PAD_XS, 0))
        for label, text in rows:
            chip = theme.button(
                row, label, lambda body=text: self._tip_clicked(body), kind="chip",
                font=theme.FONT_SMALL,
            )
            chip.pack(side="left", padx=(0, theme.PAD_XS))
            self.tip_chips.append(chip)
        self._apply_notes_box()
        return len(rows)

    def tip_texts(self):
        return [str(chip.cget("text")) for chip in self.tip_chips]

    def _tip_clicked(self, text):
        """点推荐就是把它填进输入框，不替用户发送。"""
        body = str(text or "")
        self.insert_input(body)
        self.focus_input()
        return body

    def _apply_notes_box(self):
        """有推荐才占地方。"""
        try:
            packed = str(self.empty_notes.winfo_manager()) == "pack"
            if self.tip_chips and not packed:
                self.empty_notes.pack(anchor="w", fill="x", padx=theme.PAD_MD)
            elif not self.tip_chips and packed:
                self.empty_notes.pack_forget()
        except Exception:
            pass

    def _edit_resend(self):
        return self._event("edit_resend")

    def _regen_last(self):
        return self._event("regenerate")

    def _feedback_good(self):
        return self._event("feedback", True)

    def _feedback_bad(self):
        return self._event("feedback", False)

    def set_actions_enabled(self, flag):
        flag = bool(flag)
        state = "normal" if flag else "disabled"
        for widget in (self.action_copy, self.action_edit, self.action_regen,
                       self.action_good, self.action_bad):
            widget.config(state=state)
        return flag

    # ---------- 语音与网页搜索 ----------
    def _voice(self):
        return self._event("voice")

    def _web_search(self):
        return self._event("web_search")

    def voice_available(self):
        return str(self.voice_button.cget("state")) != "disabled"

    def bar_buttons(self):
        """输入区那排图标：附件、网页、工具、语音。"""
        return (self.bar_attach, self.bar_web, self.bar_tools, self.bar_voice, self.bar_knowledge)

    def web_search_available(self):
        return str(self.web_button.cget("state")) != "disabled"

    def set_voice_available(self, flag, tip=""):
        flag = bool(flag)
        self.voice_button.config(state="normal" if flag else "disabled")
        self.bar_voice.config(state="normal" if flag else "disabled")
        theme.tooltip(self.voice_button, tip or ("按下说话" if flag else "没有检测到可用的语音输入"))
        return flag

    def set_web_search_available(self, flag, tip=""):
        flag = bool(flag)
        self.web_button.config(state="normal" if flag else "disabled")
        self.bar_web.config(state="normal" if flag else "disabled")
        theme.tooltip(
            self.web_button, tip or ("在网页上找资料" if flag else "没有配置搜索服务，暂时不可用")
        )
        return flag

    # ---------- 附件 ----------
    def set_attachments(self, items):
        """items 每项：{"id": str, "name": str, "state": str}。宿主给真状态。"""
        for chip in self.attach_chips.values():
            try:
                chip.destroy()
            except Exception:
                continue
        self.attach_chips = {}
        items = list(items or [])
        for item in items:
            ident = str(item.get("id") or item.get("path") or "")
            chip = tk.Frame(
                self.attach_box, bg=theme.CARD, highlightthickness=1,
                highlightbackground=theme.BORDER,
            )
            chip.pack(side="left", padx=(0, theme.PAD_XS), pady=theme.PAD_XS)
            name = str(item.get("name") or ident)
            state = self.ATTACH_STATES.get(str(item.get("state") or ""), "待解析")
            label = theme.label(chip, "%s · %s" % (name, state), kind="small", bg=theme.CARD,
                                fg=theme.MUTED)
            label.pack(side="left", padx=(theme.PAD_SM, theme.PAD_XS), pady=theme.PAD_XS)
            drop = theme.button(
                chip, "\u2715", lambda name=ident: self._remove_attachment(name),
                kind="ghost", font=theme.FONT_SMALL,
            )
            drop.pack(side="left", padx=(0, theme.PAD_XS))
            self.attach_chips[ident] = chip
        self._apply_attach_box()
        self._apply_action_bar()
        return len(items)

    def attachment_count(self):
        return len(self.attach_chips)

    def _apply_attach_box(self):
        try:
            packed = str(self.attach_box.winfo_manager()) == "pack"
            if self.attach_chips and not packed:
                self.attach_box.pack(
                    side="top", fill="x", before=self.field, padx=theme.PAD_MD,
                    pady=(theme.PAD_XS, 0),
                )
            elif not self.attach_chips and packed:
                self.attach_box.pack_forget()
        except Exception as exc:
            if self.logger:
                self.logger.warning("attachment layout failed: %s", exc)

    def _apply_action_bar(self):
        """消息操作栏：聊过天、还在生成、或者挂了附件才摆出来。"""
        try:
            has_text = bool(self.transcript.get("1.0", "end").strip())
        except Exception:
            has_text = False
        wanted = has_text or bool(getattr(self, "_busy", False)) or bool(self.attach_chips)
        try:
            packed = str(self.action_bar.winfo_manager()) == "pack"
            if wanted and not packed:
                # 放在输入框上方紧挨着消息区，别掉到窗口最底下。
                self.action_bar.pack(
                    side="top", fill="x", before=self.field, padx=theme.PAD_MD,
                    pady=(theme.PAD_XS, 0),
                )
            elif not wanted and packed:
                self.action_bar.pack_forget()
        except Exception as exc:
            if self.logger:
                self.logger.warning("action bar layout failed: %s", exc)
        return wanted

    def _remove_attachment(self, ident):
        return self._event("remove_attachment", ident)

    # ---------- 等待授权 ----------
    def show_permission(self, tool="", level="", reason=""):
        text = "桌宠想执行：%s（权限 %s）" % (tool or "一个操作", level or "普通")
        if reason:
            text += "\n" + str(reason)
        self.perm_label.configure(text=text)
        try:
            if str(self.perm_box.winfo_manager()) != "pack":
                self.perm_box.pack(
                    side="bottom", fill="x", padx=theme.PAD_MD, pady=(theme.PAD_XS, 0)
                )
            self.perm_deny.pack(side="right", padx=(0, theme.PAD_XS), pady=theme.PAD_XS)
            self.perm_allow.pack(side="right", padx=theme.PAD_XS, pady=theme.PAD_XS)
        except Exception as exc:
            if self.logger:
                self.logger.warning("permission card failed: %s", exc)
            return None
        self._permission_open = True
        self.show_empty(False)
        return text

    def clear_permission(self):
        self._permission_open = False
        try:
            self.perm_allow.pack_forget()
            self.perm_deny.pack_forget()
            self.perm_box.pack_forget()
        except Exception:
            return False
        return True

    def permission_open(self):
        return bool(self._permission_open)

    def permission_text(self):
        return str(self.perm_label.cget("text") or "")

    def _perm_allow(self):
        return self._permission_answer(True)

    def _perm_deny(self):
        return self._permission_answer(False)

    def _permission_answer(self, approved):
        self.clear_permission()
        return self._event("permission", bool(approved))

    # ---------- 多步任务 ----------
    def set_task_steps(self, steps, title=""):
        """steps 每项：(步骤名, 状态, 说明)，状态取 done/running/todo/failed/waiting。"""
        rows = []
        for item in list(steps or []):
            parts = list(item) + ["", "", ""]
            name, state, detail = str(parts[0]), str(parts[1]), str(parts[2])
            line = "%s %s" % (self.STEP_MARKS.get(state, "\u25cb"), name)
            if detail:
                line += "  · %s" % detail
            rows.append(line)
        if not rows:
            return self.set_task("", active=False)
        text = "\n".join(rows)
        if title:
            text = "%s\n%s" % (title, text)
        self._panel_cache = text
        if self.panel_shown:
            self.set_panel_text(text)
        return self.set_task(rows[0], active=True)

    # ---------- 斜杠命令 ----------
    def set_commands(self, commands):
        """commands 每项：(说明, 键)。输入 / 时提示，回车走第一条匹配。"""
        self.commands = [(str(a), str(b)) for a, b in (commands or [])]
        return len(self.commands)

    def command_matches(self, text):
        word = str(text or "").lstrip("/").strip().lower()
        picks = []
        for label, key in self.commands:
            if not word or word in label.lower() or word in key.lower():
                picks.append((label, key))
        return picks

    def command_hint_text(self):
        picks = self.command_matches(self.input.get())
        if not picks:
            return "没有匹配的命令"
        return "命令：" + " · ".join(label for label, _key in picks[:4])

    def _slash_changed(self, event=None):
        try:
            text = self.input.get()
        except Exception:
            return None
        if not text.startswith("/"):
            self._hide_commands()
            return None
        self.command_hint.configure(text=self.command_hint_text())
        try:
            if str(self.command_hint.winfo_manager()) != "pack":
                self.command_hint.pack(side="bottom", fill="x", padx=theme.PAD_MD)
        except Exception:
            return None
        self._event("slash", text)
        return text

    def _hide_commands(self):
        try:
            if str(self.command_hint.winfo_manager()) == "pack":
                self.command_hint.pack_forget()
        except Exception:
            return None
        return True

    # ---------- 会话列表 ----------
    def selected_session(self):
        selection = self.session_list.curselection()
        if not selection:
            return ""
        index = int(selection[0])
        if index >= len(self._sessions):
            return ""
        return self._sessions[index][0]

    def _session_menu(self):
        return self._event("session_menu")

    def _session_rename_request(self, event=None):
        return self._event("session_rename", self.selected_session())

    # ---------- 窗口控制 ----------
    def minimize(self):
        return self._event("minimize")

    def toggle_maximize(self):
        return self._event("maximize")
