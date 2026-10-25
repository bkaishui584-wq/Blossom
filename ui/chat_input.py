"""输入框控件：多行自适应、Enter 发送、Shift+Enter 换行、占位提示。

从 ui/chat_view.py 整块搬过来的。界面只负责收键盘和排队，发不发出去由外面决定。
"""

import tkinter as tk
from tkinter import font as tkfont

from ui import theme


class ChatInput(tk.Text):
    """多行输入框：Enter 发送，Shift+Enter 换行，随内容长高（最多 MAX_LINES 行）。

    insert / delete / get 认 0 这种简写下标，老的调用方不用改。
    空的时候显示一行灰色提示，聚焦或用键盘打字就消失。
    """

    MAX_LINES = 5
    # 工作台够大时给输入框留几行，别让人对着一条细缝打字。
    WORKBENCH_MIN_LINES = 3
    PLACEHOLDER_SHORT = "说点什么…"
    # 占位提示是一行灰字，不许被编辑掉、更不许被当成消息发出去。
    GHOST_SAFE_KEYS = frozenset({
        "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
        "Caps_Lock", "Num_Lock", "Scroll_Lock", "Win_L", "Win_R",
        "Tab", "Escape", "Up", "Down", "Left", "Right", "Home", "End",
        "Prior", "Next", "Insert", "F1", "F2", "F3", "F4", "F5", "F6",
        "F7", "F8", "F9", "F10", "F11", "F12",
        # 退格和删除键也算安全键：那行灰字不是内容，不能被啃掉。
        "BackSpace", "Delete",
    })
    PLACEHOLDER = "输入消息，或拖入文件……（Enter 发送）"
    PLACEHOLDER_WIDE = "输入消息，或拖入文件……（Enter 发送 · Shift+Enter 换行）"

    def __init__(self, master, on_send=None, on_grow=None, **kw):
        options = {
            "height": 1,
            "width": 1,
            "wrap": "word",
            "bg": theme.FIELD,
            "fg": theme.TEXT,
            "insertbackground": theme.TEXT,
            "selectbackground": theme.SELECT_BG,
            "bd": 0,
            "highlightthickness": 0,
            "font": theme.FONT_INPUT,
            "padx": theme.PAD_XS,
            "pady": theme.PAD_XS,
        }
        options.update(kw)
        super().__init__(master, **options)
        self.on_send = on_send
        self.on_grow = on_grow
        self.min_lines = 1
        self.placeholder = self.PLACEHOLDER
        self._placeholder_on = False
        self.bind("<Return>", self._return)
        self.bind("<Shift-Return>", self._shift_return)
        self.bind("<KeyRelease>", self._changed)
        # 粘贴是 Tcl 直接插的，绕不过下面的 insert；所以先让灰字让开，
        # 否则用户粘进来的东西会和提示文字粘在一起，还发不出去。
        self.bind("<<Paste>>", self._before_paste, add="+")
        self.bind("<<Paste>>", self._after_paste, add="+")
        self.bind("<<PasteSelection>>", self._before_paste, add="+")
        self.bind("<Key>", self._first_key, add="+")
        self.bind("<FocusIn>", self._focus_in)
        self.bind("<FocusOut>", self._focus_out)
        self._style()
        self.show_placeholder()

    # ---------- 外观 ----------
    def _style(self):
        self.configure(
            bg=theme.FIELD,
            fg=theme.TEXT,
            insertbackground=theme.TEXT,
            selectbackground=theme.SELECT_BG,
            font=theme.FONT_INPUT,
        )
        self.tag_config("placeholder", foreground=theme.PLACEHOLDER)

    def apply_theme(self):
        self._style()
        return True

    # ---------- 占位提示 ----------
    def show_placeholder(self):
        self._placeholder_on = False
        self.delete(0, tk.END)
        super().insert("1.0", self.placeholder, "placeholder")
        self._placeholder_on = True
        self._sync_height()
        return True

    def clear_placeholder(self):
        if not self._placeholder_on:
            return False
        self._placeholder_on = False
        self.delete(0, tk.END)
        return True

    def has_placeholder(self):
        return self._placeholder_on

    def fit_placeholder(self):
        """量一下输入框实际宽度，挑一句放得下的提示。不同缩放比例都能自适应。"""
        try:
            width = int(self.winfo_width()) - theme.PAD_XS * 2
        except Exception:
            return self.placeholder
        if width <= 1:
            return self.placeholder
        try:
            font = tkfont.Font(font=self.cget("font"))
        except Exception:
            return self.placeholder
        for text in (self.PLACEHOLDER_WIDE, self.PLACEHOLDER, self.PLACEHOLDER_SHORT):
            try:
                if font.measure(text) <= width:
                    return self.set_placeholder(text)
            except Exception:
                break
        return self.set_placeholder(self.PLACEHOLDER_SHORT)

    def set_placeholder(self, text):
        """换一句占位提示。正显示着就立刻重画。"""
        text = str(text or "").strip() or self.PLACEHOLDER
        if text == self.placeholder:
            return text
        self.placeholder = text
        if self._placeholder_on:
            self.show_placeholder()
        return text

    # ---------- 下标简写：0 当 1.0 用 ----------
    @staticmethod
    def _index(value):
        if value in (0, "0", ""):
            return "1.0"
        return value

    def insert(self, index, chars, *args):
        self.clear_placeholder()
        super().insert(self._index(index), chars, *args)
        self._sync_height()
        return True

    def delete(self, index1, index2=None):
        if self._placeholder_on:
            # 那行灰字只能整行消失，不允许被一个字一个字啃掉。
            return self.clear_placeholder()
        if index2 is None:
            super().delete(self._index(index1))
        else:
            super().delete(self._index(index1), self._index(index2))
        self._sync_height()
        return True

    def get(self, index1=None, index2=None):
        if self._placeholder_on:
            return ""
        if index1 is None:
            return super().get("1.0", "end-1c")
        if index2 is None:
            return super().get(self._index(index1))
        return super().get(self._index(index1), self._index(index2))

    # ---------- 高度 ----------
    def set_min_lines(self, lines):
        """工作台放大以后，输入框至少留这么多行。"""
        try:
            self.min_lines = max(1, min(self.MAX_LINES, int(lines)))
        except (TypeError, ValueError):
            return None
        return self._sync_height()

    def _sync_height(self):
        lines = 1
        if not self._placeholder_on:
            lines = self._measure_lines()
        lines = max(self.min_lines, min(self.MAX_LINES, lines))
        try:
            unchanged = str(self.cget("height")) == str(lines)
        except Exception:
            unchanged = True
        if not unchanged:
            self.configure(height=lines)
            if self.on_grow is not None:
                try:
                    self.on_grow()
                except Exception:
                    pass
        return lines

    def _measure_lines(self):
        # 可见行数（含折行）最准；算不出来就退回逻辑行数。
        try:
            if self.winfo_ismapped():
                self.update_idletasks()
                shown = self.count("1.0", "end-1c", "displaylines")
                if isinstance(shown, (tuple, list)):
                    shown = shown[0]
                if shown:
                    return int(shown)
        except Exception:
            pass
        try:
            return int(str(self.index("end-1c")).split(".")[0])
        except Exception:
            return 1

    # ---------- 键盘 ----------
    def _first_key(self, event=None):
        """占位提示还亮着时的第一下按键：灰字整行让开，按键本身照常生效。"""
        if not self._placeholder_on:
            return None
        keysym = str(getattr(event, "keysym", "") or "")
        if keysym in self.GHOST_SAFE_KEYS:
            return "break"
        self.clear_placeholder()
        return None

    def _return(self, event=None):
        if event is not None and (event.state & 0x0001):
            return self._shift_return(event)
        if self._placeholder_on:
            return "break"
        if self.on_send is not None:
            self.on_send()
        return "break"

    def _shift_return(self, event=None):
        self.clear_placeholder()
        return None

    def _changed(self, event=None):
        self._sync_height()
        # 内容删光又空着了，把那行提示放回去，别只剩一个空框。
        if not self._placeholder_on and not super().get("1.0", "end-1c").strip():
            self.show_placeholder()
        return None

    def _before_paste(self, event=None):
        self.clear_placeholder()
        return None

    def _after_paste(self, event=None):
        try:
            self.after(1, self._sync_height)
        except Exception:
            pass
        return None

    def _focus_in(self, event=None):
        # 光标进来时，空白输入框照样保留那行灰字：它不是内容，不该因为聚焦就消失。
        if not self._placeholder_on and not super().get("1.0", "end-1c").strip():
            self.show_placeholder()
        return None

    def _focus_out(self, event=None):
        if not super().get("1.0", "end-1c").strip():
            self.show_placeholder()
        return None
