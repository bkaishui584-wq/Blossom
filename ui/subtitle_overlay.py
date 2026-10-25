"""独立悬浮字幕窗口。只显示字幕，不控制采集和翻译。"""

import ctypes
import sys
import tkinter as tk
import tkinter.font as tkfont

from ui import glass_native, theme


TEXT_COLOR_LABELS = {
    "theme": "跟随主题",
    "contrast": "高对比",
    "sky": "天蓝",
    "cyan": "浅青",
    "pink": "粉红",
}


class SubtitleOverlay:
    """置顶、可拖动、位置记忆的字幕浮窗。关闭按钮只隐藏窗口。"""

    WIDTH = 760
    HEIGHT = 190
    PREFERENCE = "subtitle.position"
    MODES = ("source", "translation", "bilingual")
    FONT_PREFERENCE = "subtitle.overlay.font_size"
    FONT_FAMILY_PREFERENCE = "subtitle.overlay.font_family"
    SPACING_PREFERENCE = "subtitle.overlay.line_spacing"
    ROWS_PREFERENCE = "subtitle.overlay.rows"
    COLOR_PREFERENCE = "subtitle.overlay.text_color"
    SCROLL_PREFERENCE = "subtitle.overlay.auto_scroll"
    ALPHA_PREFERENCE = "subtitle.overlay.alpha"
    TOPMOST_PREFERENCE = "subtitle.overlay.topmost"
    CLICK_THROUGH_PREFERENCE = "subtitle.overlay.click_through"
    MIN_ALPHA = 0.82
    ACTIONS = (
        ("总结选中字幕", "summary"),
        ("提取关键词", "keywords"),
        ("解释术语和背景", "explain"),
        ("翻译选中字幕", "translate"),
        ("整理学习笔记", "notes"),
    )

    def __init__(self, owner, on_hide=None):
        self.owner = owner
        self.on_hide = on_hide
        self.mode = "bilingual"
        self._segments = {}
        self._drag = None
        self._resize = None
        self._save_job = None
        self.font_family = str(self._read(self.FONT_FAMILY_PREFERENCE) or "").strip()
        if not self.font_family:
            self.font_family = theme.FAMILY
        self.font_size = self._read_int(
            self.FONT_PREFERENCE, max(12, int(theme.FONT_BODY[1]) + 1), 9, 30
        )
        self.line_spacing = self._read_int(self.SPACING_PREFERENCE, 3, 0, 12)
        self.recent_lines = self._read_int(self.ROWS_PREFERENCE, 4, 1, 12)
        self.text_color = str(self._read(self.COLOR_PREFERENCE) or "theme").strip()
        if self.text_color not in TEXT_COLOR_LABELS:
            self.text_color = "theme"
        self.auto_scroll = self._read_bool(self.SCROLL_PREFERENCE, True)
        self.alpha = self._read_float(
            self.ALPHA_PREFERENCE, 0.96, self.MIN_ALPHA, 1.0
        )
        self.topmost = self._read_bool(self.TOPMOST_PREFERENCE, True)
        self.click_through = self._read_bool(self.CLICK_THROUGH_PREFERENCE, False)
        self.win = tk.Toplevel(owner.root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.configure(bg=theme.BG)
        self._card = theme.card(self.win, radius=theme.RADIUS_LG)
        self._card.pack(fill="both", expand=True, padx=8, pady=8)
        self._header = tk.Frame(self._card, bg=theme.CARD)
        self._header.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_SM, 0))
        self._title = theme.label(
            self._header, "实时字幕", kind="small", fg=theme.ACCENT, bg=theme.CARD
        )
        self._title.pack(side="left")
        self._session = theme.label(
            self._header, "", kind="small", fg=theme.MUTED, bg=theme.CARD
        )
        self._session.pack(side="left", padx=(theme.PAD_SM, 0))
        self._state = theme.label(
            self._header, "未开始", kind="small", fg=theme.MUTED, bg=theme.CARD
        )
        self._state.pack(side="left", padx=theme.PAD_SM)
        self._click_badge = theme.label(
            self._header, "", kind="small", fg=theme.WARN, bg=theme.CARD
        )
        self._click_badge.pack(side="left", padx=(0, theme.PAD_SM))
        self._close = tk.Label(
            self._header,
            text="×",
            bg=theme.CARD,
            fg=theme.MUTED,
            font=theme.FONT_BODY,
            cursor="hand2",
        )
        self._close.pack(side="right")
        self._close.bind("<Button-1>", lambda event: self.hide())
        self._actions = tk.Menubutton(
            self._header,
            text="交给小蝶 ▾",
            bg=theme.PANEL_2,
            fg=theme.TEXT,
            activebackground=theme.SIDEBAR_ACTIVE,
            activeforeground=theme.TEXT,
            font=theme.FONT_SMALL,
            relief="flat",
            bd=0,
            padx=theme.PAD_SM,
            pady=1,
            cursor="hand2",
        )
        self._menu = tk.Menu(
            self._actions,
            tearoff=False,
            bg=theme.PANEL,
            fg=theme.TEXT,
            activebackground=theme.ACCENT,
            activeforeground=theme.ON_ACCENT,
            font=theme.FONT_SMALL,
        )
        for label, action in self.ACTIONS:
            self._menu.add_command(
                label=label, command=lambda name=action: self._hand_to_agent(name)
            )
        self._actions.configure(menu=self._menu)
        self._actions.pack(side="right", padx=(0, theme.PAD_SM))
        self._tools = tk.Menubutton(
            self._header,
            text="操作 ▾",
            bg=theme.PANEL_2,
            fg=theme.TEXT,
            activebackground=theme.SIDEBAR_ACTIVE,
            activeforeground=theme.TEXT,
            font=theme.FONT_SMALL,
            relief="flat",
            bd=0,
            padx=theme.PAD_SM,
            pady=1,
            cursor="hand2",
        )
        self._tool_menu = tk.Menu(
            self._tools,
            tearoff=False,
            bg=theme.PANEL,
            fg=theme.TEXT,
            activebackground=theme.ACCENT,
            activeforeground=theme.ON_ACCENT,
            font=theme.FONT_SMALL,
        )
        self._tool_menu.add_command(label="复制当前字幕", command=self.copy_current)
        self._tool_menu.add_command(label="暂停滚动", command=self.toggle_auto_scroll)
        self._tool_menu.add_separator()
        self._tool_menu.add_command(label="隐藏窗口", command=self.hide)
        self._tools.configure(menu=self._tool_menu)
        self._tools.pack(side="right", padx=(0, theme.PAD_SM))
        for widget in (self._header, self._session, self._state):
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
            widget.bind("<ButtonRelease-1>", self._drag_end)
        self._header.bind("<Button-3>", self._popup_tools)
        self.text = tk.Text(
            self._card,
            height=4,
            wrap="word",
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            bg=theme.CARD,
            fg=theme.TEXT,
            insertbackground=theme.TEXT,
            selectbackground=theme.SELECT_BG,
            font=self._body_font(),
            padx=theme.PAD_MD,
            pady=theme.PAD_XS,
        )
        self.text.pack(fill="both", expand=True, padx=theme.PAD_XS, pady=(0, theme.PAD_XS))
        self.text.bind("<Button-3>", self._popup_tools)
        self._style_text()
        self.text.configure(state="disabled")
        self._grip = tk.Label(
            self._card,
            text="◢",
            bg=theme.CARD,
            fg=theme.MUTED,
            font=theme.FONT_SMALL,
            cursor="bottom_right_corner",
        )
        self._grip.place(relx=1.0, rely=1.0, anchor="se", x=-10, y=-6)
        self._grip.bind("<ButtonPress-1>", self._resize_start)
        self._grip.bind("<B1-Motion>", self._resize_move)
        self._grip.bind("<ButtonRelease-1>", self._resize_end)
        self._update_click_badge()
        self._update_tool_menu()
        self._restore_geometry()

    # ---------- 外观 ----------
    def apply_theme(self):
        self.win.configure(bg=theme.BG)
        self._card.configure(bg=theme.CARD, highlightbackground=theme.BORDER)
        self._header.configure(bg=theme.CARD)
        self._title.configure(bg=theme.CARD, fg=theme.ACCENT)
        self._session.configure(bg=theme.CARD, fg=theme.MUTED)
        self._state.configure(bg=theme.CARD, fg=theme.MUTED)
        self._click_badge.configure(bg=theme.CARD, fg=theme.WARN)
        self._close.configure(bg=theme.CARD, fg=theme.MUTED)
        self._actions.configure(
            bg=theme.PANEL_2,
            fg=theme.TEXT,
            activebackground=theme.SIDEBAR_ACTIVE,
            activeforeground=theme.TEXT,
        )
        self._tools.configure(
            bg=theme.PANEL_2,
            fg=theme.TEXT,
            activebackground=theme.SIDEBAR_ACTIVE,
            activeforeground=theme.TEXT,
        )
        self._menu.configure(
            bg=theme.PANEL,
            fg=theme.TEXT,
            activebackground=theme.ACCENT,
            activeforeground=theme.ON_ACCENT,
        )
        self._tool_menu.configure(
            bg=theme.PANEL,
            fg=theme.TEXT,
            activebackground=theme.ACCENT,
            activeforeground=theme.ON_ACCENT,
        )
        self.text.configure(
            bg=theme.CARD,
            fg=self._color_for("source"),
            insertbackground=theme.TEXT,
            selectbackground=theme.SELECT_BG,
        )
        self._grip.configure(bg=theme.CARD, fg=theme.MUTED)
        self._style_text()
        self._render()
        return True

    def set_font_family(self, family, save=True):
        value = str(family or "").strip()
        if not value:
            return False
        try:
            installed = {name.lower() for name in tkfont.families(self.win)}
        except Exception:
            installed = set()
        if (
            installed
            and value.lower() not in installed
            and value.lower() != theme.FAMILY.lower()
        ):
            if callable(getattr(self.owner, "_hint", None)):
                self.owner._hint("系统里没有这个字体：%s" % value)
            return False
        self.font_family = value
        self.text.configure(font=self._body_font())
        self._style_text()
        if save:
            self._save(self.FONT_FAMILY_PREFERENCE, self.font_family)
        return True

    def set_font_size(self, size, save=True):
        self.font_size = max(9, min(30, int(size)))
        self.text.configure(font=self._body_font())
        self._style_text()
        if save:
            self._save(self.FONT_PREFERENCE, str(self.font_size))
        return self.font_size

    def set_line_spacing(self, spacing, save=True):
        self.line_spacing = max(0, min(12, int(spacing)))
        self._style_text()
        if save:
            self._save(self.SPACING_PREFERENCE, str(self.line_spacing))
        return self.line_spacing

    def set_recent_lines(self, count, save=True):
        self.recent_lines = max(1, min(12, int(count)))
        self._render()
        if save:
            self._save(self.ROWS_PREFERENCE, str(self.recent_lines))
        return self.recent_lines

    def set_text_color(self, name, save=True):
        value = str(name or "").strip().lower()
        if value not in TEXT_COLOR_LABELS:
            return False
        self.text_color = value
        self._style_text()
        self._render()
        if save:
            self._save(self.COLOR_PREFERENCE, value)
        return True

    def set_auto_scroll(self, enabled, save=True):
        self.auto_scroll = bool(enabled)
        self._update_tool_menu()
        self._render()
        if save:
            self._save(self.SCROLL_PREFERENCE, "1" if self.auto_scroll else "0")
        return self.auto_scroll

    def toggle_auto_scroll(self):
        enabled = self.set_auto_scroll(not self.auto_scroll)
        if callable(getattr(self.owner, "_hint", None)):
            self.owner._hint("字幕自动滚动已暂停。" if not enabled else "字幕已恢复自动滚动。")
        return enabled

    def set_opacity(self, alpha, save=True):
        self.alpha = max(self.MIN_ALPHA, min(1.0, float(alpha)))
        self._apply_alpha()
        if save:
            self._save(self.ALPHA_PREFERENCE, "%.2f" % self.alpha)
        return self.alpha

    def set_topmost(self, enabled, save=True):
        self.topmost = bool(enabled)
        try:
            self.win.attributes("-topmost", self.topmost)
        except Exception:
            pass
        if save:
            self._save(self.TOPMOST_PREFERENCE, "1" if self.topmost else "0")
        return self.topmost

    def set_click_through(self, enabled, save=True):
        enabled = bool(enabled)
        if not self._apply_click_through(enabled):
            if callable(getattr(self.owner, "_hint", None)):
                self.owner._hint("当前系统不支持字幕点击穿透，设置没有生效。")
            return False
        self.click_through = enabled
        self._update_click_badge()
        if save:
            self._save(self.CLICK_THROUGH_PREFERENCE, "1" if enabled else "0")
        return True

    def selected_text(self):
        try:
            text = self.text.get(tk.SEL_FIRST, tk.SEL_LAST)
        except Exception:
            text = ""
        return str(text or "").strip()

    def current_text(self):
        return "\n".join(
            line
            for item in self._visible_rows()
            for line in self._lines_for(item)
            if line
        ).strip()

    def copy_current(self):
        text = self.current_text()
        if not text:
            if callable(getattr(self.owner, "_hint", None)):
                self.owner._hint("现在还没有可复制的字幕。")
            return False
        try:
            self.win.clipboard_clear()
            self.win.clipboard_append(text)
            self.win.update_idletasks()
        except Exception:
            return False
        if callable(getattr(self.owner, "_hint", None)):
            self.owner._hint("已复制当前字幕。")
        return True

    def _hand_to_agent(self, action):
        text = self.selected_text()
        if not text:
            if callable(getattr(self.owner, "_hint", None)):
                self.owner._hint("先在字幕窗里选中要处理的文字，再点「交给小蝶」。")
            return False
        handler = getattr(self.owner, "hand_subtitle_to_chat", None)
        if not callable(handler):
            return False
        return bool(handler(action, text))

    def set_mode(self, mode):
        mode = str(mode or "").strip().lower()
        if mode not in self.MODES:
            return False
        self.mode = mode
        self._render()
        return True

    def set_status(self, text):
        try:
            self._state.configure(text=str(text or ""))
        except tk.TclError:
            pass

    def set_session(self, source="", source_language="auto", target_language=""):
        """显示这次字幕会话使用的音源和语言。"""
        name = str(source or "").strip()
        if len(name) > 18:
            name = name[:17] + "…"
        source_language = str(source_language or "auto").strip() or "auto"
        if source_language == "auto":
            source_language = "自动"
        target_language = str(target_language or "").strip()
        languages = source_language
        if target_language:
            languages += "→" + target_language
        text = " · ".join(item for item in (name, languages) if item)
        try:
            self._session.configure(text=text)
        except tk.TclError:
            pass

    def update_segment(self, segment):
        data = segment.to_dict() if hasattr(segment, "to_dict") else dict(segment or {})
        segment_id = str(data.get("segment_id") or "")
        if not segment_id:
            return
        self._segments[segment_id] = data
        if len(self._segments) > 40:
            older = sorted(
                self._segments.values(),
                key=lambda item: float(item.get("created_at") or 0),
            )[:-40]
            for item in older:
                self._segments.pop(str(item.get("segment_id") or ""), None)
        self._render()

    def clear(self):
        self._segments.clear()
        self._render()

    def show(self):
        self.win.deiconify()
        self.win.lift()
        try:
            self.win.attributes("-topmost", self.topmost)
        except Exception:
            pass
        self._apply_alpha()
        self._apply_click_through(self.click_through)
        self._update_click_badge()

    def hide(self):
        self.win.withdraw()
        if callable(self.on_hide):
            self.on_hide()

    def visible(self):
        try:
            return bool(self.win.winfo_viewable())
        except tk.TclError:
            return False

    def destroy(self):
        try:
            self.win.destroy()
        except tk.TclError:
            pass

    def _visible_rows(self):
        rows = sorted(
            self._segments.values(),
            key=lambda item: (
                float(item.get("start_time") or 0),
                float(item.get("created_at") or 0),
            ),
        )
        return rows[-self.recent_lines :]

    def _lines_for(self, item):
        source = str(item.get("source_text") or "").strip()
        translated = str(item.get("translated_text") or "").strip()
        final = bool(item.get("is_final"))
        lines = []
        if self.mode in ("source", "bilingual") and source:
            lines.append(source + ("" if final else " …"))
        if self.mode in ("translation", "bilingual") and translated:
            lines.append(translated)
        return lines

    def _render(self):
        rows = self._visible_rows()
        keep_y = None
        if not self.auto_scroll:
            try:
                keep_y = float(self.text.yview()[0])
            except Exception:
                keep_y = None
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        wrote = False
        for item in rows:
            source = str(item.get("source_text") or "").strip()
            translated = str(item.get("translated_text") or "").strip()
            final = bool(item.get("is_final"))
            if self.mode in ("source", "bilingual") and source:
                wrote = True
                self.text.insert(
                    "end",
                    source + ("" if final else " …") + "\n",
                    ("source", "partial") if not final else ("source",),
                )
            if self.mode in ("translation", "bilingual") and translated:
                wrote = True
                self.text.insert("end", translated + "\n", ("translation",))
        if not wrote:
            hint = "原文和译文会显示在这里"
            if self.mode == "source":
                hint = "识别出的原文会显示在这里"
            elif self.mode == "translation":
                hint = "稳定句子翻译后会显示在这里"
            self.text.insert("end", hint + "\n", ("partial",))
        self.text.configure(state="disabled")
        if self.auto_scroll:
            self.text.see("end")
        elif keep_y is not None:
            self.text.yview_moveto(keep_y)

    def _body_font(self):
        return (self.font_family, int(self.font_size))

    def _color_for(self, role):
        if self.text_color == "contrast":
            return self._best_contrast_color()
        if self.text_color == "theme":
            token = theme.ACCENT if role == "translation" else theme.TEXT
            if theme.contrast_ratio(token, theme.CARD) >= 4.5:
                return token
            return theme.TEXT
        token = {
            "sky": theme.SKY,
            "cyan": theme.CYAN,
            "pink": theme.PINK,
        }.get(self.text_color, "")
        if token and theme.contrast_ratio(token, theme.CARD) >= 4.5:
            return token
        return self._best_contrast_color()

    def _best_contrast_color(self):
        light, dark = "#ffffff", "#12101c"
        if theme.contrast_ratio(theme.CARD, light) >= theme.contrast_ratio(
            theme.CARD, dark
        ):
            return light
        return dark

    def _style_text(self):
        self.text.tag_configure(
            "source",
            foreground=self._color_for("source"),
            font=self._body_font(),
            spacing1=self.line_spacing,
            spacing3=1,
        )
        self.text.tag_configure(
            "translation",
            foreground=self._color_for("translation"),
            font=(self.font_family, int(self.font_size), "bold")
            if self.text_color == "contrast"
            else self._body_font(),
            spacing1=1,
            spacing3=self.line_spacing + 2,
        )
        self.text.tag_configure("partial", foreground=theme.MUTED)

    def _update_tool_menu(self):
        try:
            self._tool_menu.entryconfigure(
                1, label="暂停滚动" if self.auto_scroll else "继续滚动"
            )
        except Exception:
            pass

    def _popup_tools(self, event):
        try:
            self._tool_menu.tk_popup(int(event.x_root), int(event.y_root))
        finally:
            try:
                self._tool_menu.grab_release()
            except Exception:
                pass
        return "break"

    def _apply_alpha(self):
        try:
            glass_native.apply_window(
                self.win,
                radius=theme.RADIUS_LG,
                alpha=self.alpha,
                tint=theme.CARD,
            )
        except Exception:
            pass

    def _apply_click_through(self, enabled):
        if sys.platform != "win32":
            return not bool(enabled)
        try:
            user32 = ctypes.windll.user32
            handle = int(self.win.winfo_id())
            parent = int(user32.GetParent(handle) or 0)
            handle = parent or handle
            ex_style = int(user32.GetWindowLongW(handle, -20))
            transparent = 0x00000020
            layered = 0x00080000
            wanted = ex_style | transparent | layered
            if not enabled:
                wanted = ex_style & ~transparent
            user32.SetWindowLongW(handle, -20, wanted)
            user32.SetWindowPos(
                handle,
                0,
                0,
                0,
                0,
                0,
                0x0001 | 0x0002 | 0x0004 | 0x0020,
            )
            return True
        except Exception:
            return False

    def _update_click_badge(self):
        try:
            self._click_badge.configure(text="点击穿透" if self.click_through else "")
        except Exception:
            pass

    # ---------- 设置读取 ----------
    def _read_int(self, key, default, low, high):
        raw = self._read(key)
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            value = int(default)
        return max(low, min(high, value))

    def _read_float(self, key, default, low, high):
        raw = self._read(key)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = float(default)
        return max(low, min(high, value))

    def _read_bool(self, key, default):
        raw = self._read(key)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        return str(raw).strip().lower() in ("1", "true", "yes", "on")

    def _read(self, key):
        try:
            return self.owner._preference(key)
        except Exception:
            return None

    def _save(self, key, value):
        try:
            return bool(self.owner._save_preference(key, value))
        except Exception:
            return False

    # ---------- 窗口位置 ----------
    def _restore_geometry(self):
        raw = ""
        try:
            raw = str(self.owner._preference(self.PREFERENCE) or "")
        except Exception:
            raw = ""
        values = []
        try:
            values = [int(item) for item in raw.split(",")]
        except Exception:
            values = []
        if len(values) == 4:
            width = max(420, min(values[2], 1400))
            height = max(120, min(values[3], 360))
            x, y = values[0], values[1]
        else:
            width, height = self.WIDTH, self.HEIGHT
            x = max(20, (self.win.winfo_screenwidth() - width) // 2)
            y = max(20, self.win.winfo_screenheight() - height - 90)
        self.win.geometry("%dx%d+%d+%d" % (width, height, x, y))

    def _drag_start(self, event):
        self._drag = (
            int(event.x_root),
            int(event.y_root),
            int(self.win.winfo_x()),
            int(self.win.winfo_y()),
        )

    def _drag_move(self, event):
        if self._drag is None:
            return
        start_x, start_y, win_x, win_y = self._drag
        self.win.geometry(
            "+%d+%d"
            % (
                win_x + int(event.x_root) - start_x,
                win_y + int(event.y_root) - start_y,
            )
        )

    def _drag_end(self, event=None):
        self._drag = None
        self._schedule_save()

    def _resize_start(self, event):
        self._resize = (
            int(event.x_root),
            int(event.y_root),
            int(self.win.winfo_width()),
            int(self.win.winfo_height()),
            int(self.win.winfo_x()),
            int(self.win.winfo_y()),
        )

    def _resize_move(self, event):
        if self._resize is None:
            return
        start_x, start_y, width, height, win_x, win_y = self._resize
        new_width = max(420, width + int(event.x_root) - start_x)
        new_height = max(120, height + int(event.y_root) - start_y)
        self.win.geometry("%dx%d+%d+%d" % (new_width, new_height, win_x, win_y))

    def _resize_end(self, event=None):
        self._resize = None
        self._schedule_save()

    def _schedule_save(self):
        if self._save_job is not None:
            try:
                self.win.after_cancel(self._save_job)
            except Exception:
                pass
        self._save_job = self.win.after(300, self._save_geometry)

    def _save_geometry(self):
        self._save_job = None
        try:
            raw = "%d,%d,%d,%d" % (
                int(self.win.winfo_x()),
                int(self.win.winfo_y()),
                int(self.win.winfo_width()),
                int(self.win.winfo_height()),
            )
            self.owner._save_preference(self.PREFERENCE, raw)
        except Exception:
            pass
