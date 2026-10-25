"""日夜切换开关：胶囊轨道 + 浮起的圆形旋钮 + 日月图标。

按参考视频做的"分层视差"：旋钮比轨道高一层，外圈有一道浅色环和柔和阴影，
图标压在旋钮里。切换时旋钮滑动、颜色在两帧之间交叉，不做花哨粒子。

它只改界面主题（day/night），不改桌宠模型、动作或渲染进程。
"""

import math
import tkinter as tk

from ui import theme

BASE_WIDTH = 150
BASE_HEIGHT = 54
FRAMES = 12


class DayNightSwitch(tk.Canvas):
    """0 = 白天（light），1 = 夜晚（dark）。"""

    def __init__(self, master, command=None, mode=None, **kw):
        factor = max(0.8, min(2.0, theme.current_scale()))
        self.width = int(BASE_WIDTH * factor)
        self.height = int(BASE_HEIGHT * factor)
        super().__init__(
            master,
            width=self.width,
            height=self.height,
            bg=theme.BG if "bg" not in kw else kw.pop("bg"),
            highlightthickness=1,
            highlightbackground=theme.BG,
            highlightcolor=theme.ACCENT,
            bd=0,
            takefocus=1,
            cursor="hand2",
            **kw,
        )
        self.command = command
        self._mode = "dark" if str(mode or theme.active()) == "dark" else "light"
        self._value = 1.0 if self._mode == "dark" else 0.0
        self._target = self._value
        self._job = None
        self._enabled = True
        self.bind("<Button-1>", self._clicked)
        self.bind("<space>", self._clicked)
        self.bind("<Return>", self._clicked)
        self.bind("<FocusIn>", lambda _e: self._draw())
        self.bind("<FocusOut>", lambda _e: self._draw())
        self._draw()

    # ---------- 对外 ----------
    def mode(self):
        return self._mode

    def set_mode(self, mode, animate=False):
        wanted = "dark" if str(mode or "").lower() == "dark" else "light"
        self._mode = wanted
        target = 1.0 if wanted == "dark" else 0.0
        self._target = target
        if animate and abs(target - self._value) > 0.01:
            self._animate()
        else:
            self._value = target
            self._draw()
        return wanted

    def set_enabled(self, flag):
        self._enabled = bool(flag)
        self.configure(cursor="hand2" if self._enabled else "arrow")
        self._draw()

    # ---------- 交互 ----------
    def _clicked(self, _event=None):
        if not self._enabled:
            return "break"
        wanted = "light" if self._mode == "dark" else "dark"
        self.set_mode(wanted, animate=True)
        if callable(self.command):
            try:
                self.command(wanted)
            except Exception:
                pass
        return "break"

    def _animate(self):
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
            self._job = None
        steps = FRAMES
        start = self._value
        delta = self._target - start

        def frame(index):
            self._job = None
            ratio = index / float(steps)
            eased = 1 - (1 - ratio) ** 3
            self._value = start + delta * eased
            self._draw()
            if index < steps:
                self._job = self.after(16, lambda: frame(index + 1))

        frame(1)

    # ---------- 绘制 ----------
    def _palette(self):
        # 按当前滑块位置在蓝/紫之间过渡，滑动过程中颜色一起走
        t = max(0.0, min(1.0, self._value))
        track = _mix("#2f6bff", "#2a1740", t)
        knob = _mix("#3f7dff", "#3a2158", t)
        ring = _mix("#bfe0ff", "#6f5aa0", t)
        return track, knob, ring

    def _draw(self):
        self.delete("all")
        width, height = self.width, self.height
        pad = int(6 * width / BASE_WIDTH)
        radius = (height - pad * 2) / 2.0
        track, knob, ring = self._palette()

        # 轨道：胶囊
        self._pill(pad, pad, width - pad, height - pad, radius, track)

        # 旋钮位置：0 在最左，1 在最右
        travel = width - pad * 2 - radius * 2
        cx = pad + radius + travel * max(0.0, min(1.0, self._value))
        cy = height / 2.0

        # 阴影 + 外环 + 旋钮：三层叠出"浮起来"的视差
        shadow = _mix(track, "#000000", 0.35)
        self.create_oval(
            cx - radius - 1, cy - radius + 2, cx + radius - 1, cy + radius + 2,
            fill=shadow, outline="",
        )
        self.create_oval(
            cx - radius, cy - radius, cx + radius, cy + radius,
            fill=ring, outline=_mix(ring, "#ffffff", 0.35),
        )
        inset = radius * 0.12
        self.create_oval(
            cx - radius + inset, cy - radius + inset,
            cx + radius - inset, cy + radius - inset,
            fill=knob, outline="",
        )

        # 图标：白天是太阳 + 云，夜里是月亮 + 云
        night = self._value >= 0.5
        self._icon(cx, cy, radius * 0.62, night)

        # 文字放在旋钮的对面
        label = "Dark" if night else "Light"
        text_x = (width - pad - radius) if not night else (pad + radius)
        self.create_text(
            text_x, cy,
            text=label,
            fill="#ffffff",
            font=(theme.FAMILY, max(8, int(11 * self.width / BASE_WIDTH)), "bold"),
        )

        if self.focus_get() is self:
            self.create_rectangle(
                1, 1, width - 2, height - 2,
                outline=_mix(ring, "#ffffff", 0.2), width=1,
            )

    def _icon(self, cx, cy, radius, night):
        if night:
            moon = _mix("#ff9cc8", "#ffd0ea", 0.35)
            self.create_oval(cx - radius, cy - radius, cx + radius, cy + radius,
                             fill=moon, outline="")
            # 用一块和旋钮同色的圆盖出月牙
            track, knob, _ring = self._palette()
            self.create_oval(
                cx - radius * 0.35, cy - radius * 1.05,
                cx + radius * 1.15, cy + radius * 0.55,
                fill=knob, outline="",
            )
            self.create_oval(
                cx - radius * 0.9, cy + radius * 0.25,
                cx + radius * 0.2, cy + radius * 1.05,
                fill=_mix("#7f6bd8", "#4a3a7a", 0.4), outline="",
            )
            self.create_oval(
                cx - radius * 0.1, cy + radius * 0.45,
                cx + radius * 0.95, cy + radius * 1.15,
                fill=_mix("#7f6bd8", "#4a3a7a", 0.4), outline="",
            )
            return
        sun = "#ffd166"
        self.create_oval(
            cx - radius * 0.55, cy - radius * 0.75,
            cx + radius * 0.45, cy + radius * 0.25,
            fill=sun, outline="",
        )
        for index in range(8):
            angle = math.radians(index * 45)
            inner = radius * 0.78
            outer = radius * 1.02
            self.create_line(
                cx - radius * 0.05 + math.cos(angle) * inner,
                cy - radius * 0.25 + math.sin(angle) * inner,
                cx - radius * 0.05 + math.cos(angle) * outer,
                cy - radius * 0.25 + math.sin(angle) * outer,
                fill=sun, width=2, capstyle="round",
            )
        for dx, dy, r in ((-0.35, 0.45, 0.42), (0.1, 0.5, 0.5), (0.5, 0.55, 0.34)):
            self.create_oval(
                cx + radius * dx - radius * r, cy + radius * dy - radius * r * 0.6,
                cx + radius * dx + radius * r, cy + radius * dy + radius * r * 0.6,
                fill=_mix("#ffffff", "#cfe2ff", 0.45), outline="",
            )

    def _pill(self, x1, y1, x2, y2, radius, fill):
        points = [
            x1 + radius, y1, x2 - radius, y1, x2, y1 + radius,
            x2, y2 - radius, x2 - radius, y2, x1 + radius, y2,
            x1, y2 - radius, x1, y1 + radius,
        ]
        self.create_polygon(points, smooth=True, splinesteps=24, fill=fill, outline="")


def _mix(color, other, amount):
    """和 theme._mix 一样：按比例混色，用于过渡和阴影。"""
    first = str(color).lstrip("#")
    second = str(other).lstrip("#")
    try:
        parts = []
        for index in (0, 2, 4):
            left = int(first[index:index + 2], 16)
            right = int(second[index:index + 2], 16)
            parts.append(int(round(left * (1 - amount) + right * amount)))
        return "#%02x%02x%02x" % tuple(parts)
    except Exception:
        return color
