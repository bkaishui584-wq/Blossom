"""透明桌宠宿主：窗口只是角色的透明载体，用户看到的是角色本身。

无边框、透明、置顶、可拖动、不越出屏幕、可隐藏。
角色画什么由调用方决定（现在是占位小人，以后换成 Live2D 不用改这里）。
语言模型、记忆、权限都不在这里，宿主只负责"身体"。
"""

import ctypes
import tkinter as tk

# 这个颜色被系统当成"透明"：不显示，也不挡鼠标。
TRANSPARENT_KEY = "#010203"
# 贴边：松手时离屏幕边缘这么近就吸附上去。
DOCK_SNAP = 56
# 拖着走的时候碰到这条线就立刻吸上去，不用等松手。
DOCK_TOUCH = 18
# 吸附后离屏幕边缘留的空隙，完全贴着会显得很挤。
DOCK_GAP = 8
# 缩入时至少留这么多像素在外面，保证还能点回来。
DOCK_PEEK_MIN = 16
DOCK_PEEK_RATIO = 0.32
# 鼠标离开多久才缩回去，给一点缓冲，别碰一下就缩。
DOCK_PEEK_DELAY = 700
DOCK_EDGES = ("left", "right", "top", "bottom")
DOT_SIZE = 44
DIM_ALPHA = 0.35  # 专注模式下变淡一点
EDGE_MARGIN = 0
# 角色本身的碰撞体积：颜色键透明区不接鼠标，只画出来的地方接。
# 这一层用 1/255 的透明度，看不见，但窗口照样能被鼠标点中。
HIT_ALPHA = 1 / 255
HIT_TAG = "hitbody"
HIT_FILL = "#000000"


def virtual_screen(root):
    """整个虚拟桌面的范围（含多显示器）。取不到就退回主屏。"""
    try:
        user32 = ctypes.windll.user32
        return (
            user32.GetSystemMetrics(76),  # SM_XVIRTUALSCREEN
            user32.GetSystemMetrics(77),  # SM_YVIRTUALSCREEN
            user32.GetSystemMetrics(78),  # SM_CXVIRTUALSCREEN
            user32.GetSystemMetrics(79),  # SM_CYVIRTUALSCREEN
        )
    except Exception:
        return (0, 0, root.winfo_screenwidth(), root.winfo_screenheight())

# ---------- 任务栏 ----------
GWL_EXSTYLE = -20
GWL_HWNDPARENT = -8
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
SW_HIDE = 0
SW_SHOW = 5
SW_MINIMIZE = 6
SW_RESTORE = 9
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020


def hide_from_taskbar(window):
    """把这个窗口从任务栏里摘掉。

    桌宠和它的聊天浮层是常驻的小窗口，不该在任务栏占位置。
    只改自己窗口的扩展样式，不动别的程序。
    """
    try:
        user32 = ctypes.windll.user32
        handle = int(window.winfo_id())
    except Exception:
        return False
    try:
        parent = int(user32.GetParent(handle) or 0)
    except Exception:
        parent = 0
    done = False
    for target in (handle, parent):
        if not target:
            continue
        try:
            style = user32.GetWindowLongW(target, GWL_EXSTYLE)
            style = (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
            user32.SetWindowLongW(target, GWL_EXSTYLE, style)
            done = True
        except Exception:
            continue
    return done


def _window_handles(window):
    """返回 (Tk 句柄, 真正的顶层句柄)。第二个为空时退回第一个。"""
    try:
        handle = int(window.winfo_id())
    except Exception:
        return 0, 0
    try:
        parent = int(ctypes.windll.user32.GetParent(handle) or 0)
    except Exception:
        parent = 0
    return handle, parent or handle


def show_in_taskbar(window):
    """让这个窗口出现在任务栏。聊天工作台需要，桌宠窗口不需要。"""
    handle, top = _window_handles(window)
    if not handle:
        return False
    try:
        user32 = ctypes.windll.user32
    except Exception:
        return False
    done = False
    for target in (handle, top):
        try:
            style = user32.GetWindowLongW(target, GWL_EXSTYLE)
            style = (style | WS_EX_APPWINDOW) & ~WS_EX_TOOLWINDOW
            user32.SetWindowLongW(target, GWL_EXSTYLE, style)
            done = True
        except Exception:
            continue
    try:
        # 有 owner 的窗口不会出现在任务栏，把 owner 清掉。
        user32.SetWindowLongPtrW(ctypes.c_void_p(top), GWL_HWNDPARENT, 0)
    except Exception:
        pass
    try:
        # Re-apply the frame style in place. Hiding and showing a visible window
        # made the chat flash every time its taskbar entry was repaired.
        user32.SetWindowPos(
            top, 0, 0, 0, 0, 0,
            SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_FRAMECHANGED,
        )
    except Exception:
        pass
    return done


def minimize_window(window):
    """真的最小化到任务栏，而不是 withdraw 掉。"""
    _handle, top = _window_handles(window)
    if not top:
        return False
    try:
        ctypes.windll.user32.ShowWindow(top, SW_MINIMIZE)
        return True
    except Exception:
        return False


def restore_window(window):
    _handle, top = _window_handles(window)
    if not top:
        return False
    try:
        ctypes.windll.user32.ShowWindow(top, SW_RESTORE)
        return True
    except Exception:
        return False


def hide_process_windows():
    """把本进程所有顶层窗口都从任务栏摘掉，返回这轮动了几个。

    有些窗口是按需建的，建的时候来不及逐个处理，按进程扫一遍最省事。
    只认自己进程的窗口，绝不碰别的程序。
    """
    try:
        user32 = ctypes.windll.user32
        owner_id = int(ctypes.windll.kernel32.GetCurrentProcessId())
    except Exception:
        return 0
    touched = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def visit(hwnd, _param):
        owner = ctypes.c_ulong()
        try:
            user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(owner))
            if int(owner.value) != owner_id:
                return True
            style = user32.GetWindowLongW(ctypes.c_void_p(hwnd), GWL_EXSTYLE)
            wanted = (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
            if wanted != style:
                user32.SetWindowLongW(ctypes.c_void_p(hwnd), GWL_EXSTYLE, wanted)
                touched.append(int(hwnd))
        except Exception:
            return True
        return True

    callback = proto(visit)
    try:
        user32.EnumWindows(callback, None)
    except Exception:
        return 0
    return len(touched)


class PetHost:
    """承载桌宠形象的透明窗口。"""

    CLICK_SLOP = 4
    # 按住多久算长按。短于这个时间的按压只当普通点击。
    LONG_PRESS_MS = 600

    def __init__(self, root, build_canvas, key=TRANSPARENT_KEY, logger=None):
        self.root = root
        self.key = key
        self.logger = logger
        self.on_click = None  # 单击（没有拖动）时回调
        self.on_double_click = None  # 双击时回调
        self.on_right_click = None  # 右键时回调；没设就退回 on_click
        self.on_pointer_near = None  # 鼠标移进桌宠时回调
        self.on_pointer_away = None  # 鼠标移出桌宠时回调
        self.on_moved = None  # 拖动结束后回调，参数是新坐标
        self.on_long_press = None  # 按住不动够久时回调（还没松手）
        self.on_drag_end = None  # 拖到别处再松手时回调，参数是新坐标
        self.on_geometry = None  # 窗口位置或大小变了就回调 (x, y, w, h)
        self.on_visibility = None  # 显示/隐藏时回调，参数是现在的 hidden
        self.hidden = False
        self.dimmed = False
        self.topmost = True
        self._drag = None
        self._press_timer = None
        self._dot = None
        self._hit = None
        self._overlay_dropped = False  # 透明状态不对时先把碰撞层撤下来
        self._pos = (0, 0)  # 自己记住位置，窗口还没映射时也准确
        # 贴边：拖到边上吸附，吸附后可以自动缩入，鼠标碰一下再滑出来。
        self.docking = True
        self.auto_hide = True
        self.dock_edge = ""
        self._dock_rest = None
        self._peek = False
        self._peek_timer = None

        self.canvas = build_canvas(root)
        root.overrideredirect(True)
        root.configure(bg=key)
        self._transparent()
        hide_from_taskbar(root)
        self.canvas.pack()
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Button-3>", self._right_click)
        self.canvas.bind("<Double-Button-1>", self._double_click)
        self.canvas.bind("<Enter>", self._pointer_near)
        self.canvas.bind("<Leave>", self._pointer_away)
        self.canvas.bind("<Configure>", self._on_canvas_configure, add="+")
        self.move_to(*self._default_position())

    # ---------- 窗口外观 ----------
    def _transparent(self):
        try:
            self.root.wm_attributes("-transparentcolor", self.key)
        except Exception as exc:
            # 个别环境不支持色键透明，退化成不透明小窗口，功能不受影响。
            if self.logger:
                self.logger.warning("transparent window unsupported: %s", exc)
        try:
            self.root.wm_attributes("-topmost", True)
        except Exception:
            pass

    def _on_canvas_configure(self, event=None):
        """画布尺寸定下来后再画一次命中区，1x1 的早期布局画不出格子。"""
        self._paint_hit()
        return None

    def _default_position(self):
        left, top, width, height = virtual_screen(self.root)
        w, h = self.size()
        return (left + width - w - 48, top + height - h - 90)

    def default_position(self):
        """默认落点：右下角留点边距。用户点「复位」时用它。"""
        return self._default_position()

    # ---------- 位置 ----------
    def size(self):
        w = self.canvas.winfo_reqwidth() or 1
        h = self.canvas.winfo_reqheight() or 1
        return (w, h)

    def resize_body(self, width, height):
        """把透明载体改成角色需要的大小；位置保持不变，改完重新贴边。"""
        width = max(DOT_SIZE, int(width or DOT_SIZE))
        height = max(DOT_SIZE, int(height or DOT_SIZE))
        self.canvas.configure(width=width, height=height)
        self.canvas.pack_propagate(False)
        keep = self._pos
        self.move_to(*keep)
        if self.dock_edge and not self._peek:
            self.snap_to_edge(self.dock_edge)
        self._paint_hit()
        return (width, height)

    def position(self):
        return self._pos

    def window_size(self):
        """窗口当前该多大：隐藏时只剩一个小点。"""
        return (DOT_SIZE, DOT_SIZE) if self.hidden else self.size()

    def bounds(self):
        """当前应该在的边界，拖到哪都不会掉出屏幕。"""
        left, top, width, height = virtual_screen(self.root)
        w, h = self.window_size()
        return (left, top, left + width - w, top + height - h)

    def move_to(self, x, y):
        left, top, right, bottom = self.bounds()
        x = max(left, min(int(x), right - EDGE_MARGIN))
        y = max(top, min(int(y), bottom - EDGE_MARGIN))
        w, h = self.window_size()
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self._pos = (x, y)
        if self.on_geometry is not None:
            try:
                self.on_geometry(x, y, w, h)
            except Exception as exc:
                if self.logger:
                    self.logger.warning("geometry callback failed: %s", exc)
        return (x, y)

    def anchor(self):
        """聊天卡片贴着哪：桌宠当前的左上角与大小。"""
        x, y = self.position()
        w, h = self.window_size()
        return (x, y, w, h)

    def card_position(self, card_width, card_height, gap=12):
        """浮层优先放右边，右边放不下就翻到左边，上下不出屏。"""
        x, y, w, h = self.anchor()
        left, top, width, height = virtual_screen(self.root)
        right_edge = left + width
        bottom_edge = top + height
        cx = x + w + gap
        if cx + card_width > right_edge:
            cx = x - card_width - gap
        cx = max(left, min(cx, right_edge - card_width))
        cy = max(top, min(y + h - card_height, bottom_edge - card_height))
        return (cx, cy)

    # ---------- 贴边与缩入 ----------
    def nearest_edge(self, x=None, y=None):
        """离哪条边最近，顺带给出距离。"""
        left, top, width, height = virtual_screen(self.root)
        right = left + width
        bottom = top + height
        w, h = self.window_size()
        if x is None or y is None:
            x, y = self._pos
        gaps = (
            ("left", x - left),
            ("right", right - (x + w)),
            ("top", y - top),
            ("bottom", bottom - (y + h)),
        )
        return min(gaps, key=lambda item: item[1])

    def _flush_position(self, edge):
        """贴边后该在的位置：在最近那条边上，另一条轴不动。"""
        left, top, width, height = virtual_screen(self.root)
        w, h = self.window_size()
        x, y = self._pos
        if edge == "left":
            return (left + DOCK_GAP, y)
        if edge == "right":
            return (left + width - w - DOCK_GAP, y)
        if edge == "top":
            return (x, top + DOCK_GAP)
        if edge == "bottom":
            return (x, top + height - h - DOCK_GAP)
        return (x, y)

    def _place(self, x, y):
        """直接摆到指定坐标，不做屏幕内夹取。

        缩入时窗口要大部分跑到屏幕外，所以这一条必须绕开 move_to 的边界限制。
        """
        w, h = self.window_size()
        x, y = int(x), int(y)
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
        self._pos = (x, y)
        if self.on_geometry is not None:
            try:
                self.on_geometry(x, y, w, h)
            except Exception as exc:
                if self.logger:
                    self.logger.warning("geometry callback failed: %s", exc)
        return (x, y)

    def docked_edge(self):
        return self.dock_edge

    def snap_to_edge(self, edge=None):
        """吸附到指定边；不给边就挑最近的那条。"""
        if edge is None:
            edge, _ = self.nearest_edge()
        if edge not in DOCK_EDGES:
            return ""
        self._cancel_peek_timer()
        self.dock_edge = edge
        self._peek = False
        x, y = self._flush_position(edge)
        self._dock_rest = (x, y)
        self._place(x, y)
        return edge

    def undock(self):
        """取消贴边，停在现在的位置，不会突然跳走。"""
        self._cancel_peek_timer()
        was_peeked = self._peek
        self._peek = False
        self.dock_edge = ""
        self._dock_rest = None
        if was_peeked:
            self.move_to(*self._pos)
        return True

    def peeked(self):
        return bool(self._peek)

    def _visible_span(self):
        w, h = self.window_size()
        if self.dock_edge in ("left", "right"):
            return max(DOCK_PEEK_MIN, int(w * DOCK_PEEK_RATIO))
        if self.dock_edge in ("top", "bottom"):
            return max(DOCK_PEEK_MIN, int(h * DOCK_PEEK_RATIO))
        return 0

    def _peek_position(self):
        left, top, width, height = virtual_screen(self.root)
        w, h = self.window_size()
        rest_x, rest_y = self._dock_rest or self._pos
        span = self._visible_span()
        if self.dock_edge == "left":
            return (left - w + span, rest_y)
        if self.dock_edge == "right":
            return (left + width - span, rest_y)
        if self.dock_edge == "top":
            return (rest_x, top - h + span)
        if self.dock_edge == "bottom":
            return (rest_x, top + height - span)
        return (rest_x, rest_y)

    def slide_in(self):
        """缩到边上，只留一条可见。没贴边就什么都不做。"""
        if not self.dock_edge or self._peek:
            return bool(self._peek)
        if self._dock_rest is None:
            self._dock_rest = self._pos
        self._cancel_peek_timer()
        self._peek = True
        self._place(*self._peek_position())
        return True

    def slide_out(self):
        """从边上滑回来，恢复完整角色。"""
        if not self._peek:
            return False
        self._cancel_peek_timer()
        self._peek = False
        self._place(*(self._dock_rest or self._pos))
        return True

    def toggle_peek(self):
        return self.slide_out() if self._peek else self.slide_in()

    def _arm_peek(self):
        self._cancel_peek_timer()
        try:
            self._peek_timer = self.root.after(DOCK_PEEK_DELAY, self._fire_peek)
        except Exception as exc:
            if self.logger:
                self.logger.warning("peek timer failed: %s", exc)
            self._peek_timer = None
        return self._peek_timer

    def _cancel_peek_timer(self):
        timer = getattr(self, "_peek_timer", None)
        self._peek_timer = None
        if timer is None:
            return False
        try:
            self.root.after_cancel(timer)
        except Exception:
            pass
        return True

    def _fire_peek(self):
        self._peek_timer = None
        if self.hidden or not self.dock_edge:
            return False
        return self.slide_in()

    def snap_while_dragging(self):
        """拖动途中碰到屏幕边就直接贴上，走远了再松开。"""
        if not self.docking or self.hidden:
            return ""
        edge, distance = self.nearest_edge()
        if distance <= DOCK_TOUCH:
            if self.dock_edge != edge or self._peek:
                self.snap_to_edge(edge)
            # 手还按着，别在这个时候缩进去。
            self._cancel_peek_timer()
            return edge
        if self.dock_edge and distance > DOCK_SNAP:
            self.undock()
        return ""

    def snap_after_drag(self):
        """松手时按距离决定贴不贴边。角色窗口转发来的拖动也要走这条。"""
        if not self.docking:
            return ""
        edge, distance = self.nearest_edge()
        if distance > DOCK_SNAP:
            self.undock()
            return ""
        self.snap_to_edge(edge)
        if self.auto_hide:
            self._arm_peek()
        return edge

    def dock_rest_position(self):
        """贴边时的完整位置。存位置时用这个，别存缩入后的坐标。"""
        if self._dock_rest is not None:
            return self._dock_rest
        return self._pos

    # ---------- 拖动与点击 ----------
    def _press(self, event):
        # 先把自己抬到最前并拿到焦点：别的窗口刚被点过时，第一下也得能拖得动。
        if self.topmost:
            try:
                self.root.lift()
                self.root.focus_force()
            except Exception:
                pass
        x, y = self._pos
        self._drag = (
            event.x_root - x,
            event.y_root - y,
            event.x_root,
            event.y_root,
        )
        self._arm_long_press()

    def _motion(self, event):
        if not self._drag:
            return
        if (
            abs(event.x_root - self._drag[2]) + abs(event.y_root - self._drag[3])
            > self.CLICK_SLOP
        ):
            # 已经拖起来了，就不再算长按。
            self._disarm_long_press()
        self.move_to(event.x_root - self._drag[0], event.y_root - self._drag[1])
        if self._drag_moved(event):
            self.snap_while_dragging()

    def _drag_moved(self, event):
        """这一下算不算真的在拖：超过点击容差才算。"""
        if not self._drag:
            return False
        return (
            abs(event.x_root - self._drag[2]) + abs(event.y_root - self._drag[3])
            > self.CLICK_SLOP
        )

    def _release(self, event):
        if not self._drag:
            return
        start = self._drag
        self._drag = None
        self._disarm_long_press()
        moved = abs(event.x_root - start[2]) + abs(event.y_root - start[3])
        self.move_to(*self.position())
        if moved > self.CLICK_SLOP and self.docking:
            self.snap_after_drag()
        if moved <= self.CLICK_SLOP:
            if self.on_click:
                self.on_click(event)
            return
        # 两个回调都要发：一个做放下时的动作反应，一个负责记住新位置。
        if self.on_drag_end:
            self.on_drag_end(self.position())
        if self.on_moved:
            self.on_moved(self.position())

    # ---------- 长按 ----------
    def _arm_long_press(self):
        """按住不动够久就报一次长按。松开或拖起来就取消。"""
        self._disarm_long_press()
        if self.on_long_press is None:
            return None
        try:
            self._press_timer = self.root.after(
                self.LONG_PRESS_MS, self._fire_long_press
            )
        except Exception as exc:  # 没有 after 的宿主（测试替身）就只是不做长按
            if self.logger:
                self.logger.warning("long press timer failed: %s", exc)
            self._press_timer = None
        return self._press_timer

    def _disarm_long_press(self):
        timer = getattr(self, "_press_timer", None)
        self._press_timer = None
        if timer is None:
            return False
        try:
            self.root.after_cancel(timer)
        except Exception:
            pass
        return True

    def _fire_long_press(self):
        self._press_timer = None
        if not self._drag or self.on_long_press is None:
            return None
        return self.on_long_press(self.position())

    def _double_click(self, event):
        if self.on_double_click:
            self.on_double_click(event)

    def _right_click(self, event):
        # 右键优先弹菜单；没接菜单才退回普通点击。
        if self.on_right_click:
            self.on_right_click(event)
            return
        if self.on_click:
            self.on_click(event)

    def _pointer_near(self, event):
        # 缩入状态下一碰就滑出来，用户不用猜它跑到哪去了。
        if self._peek:
            self.slide_out()
        if self.on_pointer_near:
            self.on_pointer_near(event)

    def _pointer_away(self, event):
        if self.dock_edge and self.auto_hide and not self.hidden:
            self._arm_peek()
        if self.on_pointer_away:
            self.on_pointer_away(event)

    # ---------- 隐藏 / 显示 ----------
    def set_hidden(self, hidden):
        """隐藏后只留一个小点，点它就能把桌宠叫回来，不会找不回来。"""
        hidden = bool(hidden)
        if hidden and self._peek:
            self.slide_out()
        x, y = self.position()
        if hidden:
            self.canvas.pack_forget()
            dot = self._ensure_dot()
            dot.pack()
            self.hidden = True
            self.move_to(x, y)
        else:
            if self._dot is not None:
                self._dot.pack_forget()
            self.hidden = False
            self.canvas.pack()
            self.move_to(x, y)
        self._paint_hit()
        try:
            self.root.lift()
        except Exception:
            pass
        if self.on_visibility is not None:
            try:
                self.on_visibility(self.hidden)
            except Exception as exc:
                if self.logger:
                    self.logger.warning("visibility callback failed: %s", exc)
        return self.hidden

    def set_dim(self, dim):
        """专注模式下变淡，降低存在感；退出后恢复原样。"""
        self.dimmed = bool(dim)
        if not self._apply_alpha():
            # 改透明度失败就如实说没改成，界面不会显示一个没生效的状态。
            return False
        return self.dimmed

    # ---------- 碰撞体积 ----------
    def set_hit_mask(self, cols, rows, bits):
        """按角色当前画面给窗口一个真实碰撞体积。

        掩码是角色本体在画面里的格子；鼠标落在本体上窗口才接，透明处照旧穿过去。
        形状不合适就什么都不改，桌宠不会因此变得点不中。
        """
        try:
            cols = int(cols)
            rows = int(rows)
            bits = bytes(bits)
        except (TypeError, ValueError):
            return False
        if cols <= 0 or rows <= 0 or len(bits) * 8 < cols * rows:
            return False
        self._hit = (cols, rows, bits)
        self._paint_hit()
        return True

    def clear_hit_mask(self):
        """回到占位形象：占位图自己就是碰撞体积，不需要额外一层。"""
        self._hit = None
        self._paint_hit()
        return True

    def hit_mask(self):
        return self._hit

    def has_hit(self):
        """现在有没有碰撞体积。没有的话鼠标会直接穿过去，桌宠就点不着了。"""
        return self._hit is not None and not self.hidden

    def set_fallback_hit(self):
        """兜底碰撞体积。

        角色在画、可形状掩码一时算不出来时，先让中间一块区域接鼠标，
        保证桌宠任何时候都拖得动。真实掩码回来会被它顶掉。
        """
        cols = rows = 12
        bits = bytearray((cols * rows + 7) // 8)
        for row in range(3, 9):
            for col in range(3, 9):
                index = row * cols + col
                bits[index >> 3] |= 1 << (index & 7)
        return self.set_hit_mask(cols, rows, bytes(bits))

    def _hit_on(self, col, row):
        cols, rows, bits = self._hit
        index = row * cols + col
        return bool((bits[index >> 3] >> (index & 7)) & 1)

    def _paint_hit(self):
        """把碰撞体积画到载体上：同一行里连着的格子合成一条，省得画上百个方块。"""
        try:
            self.canvas.delete(HIT_TAG)
            if self._hit is None or self.hidden:
                return
            cols, rows, _bits = self._hit
            width, height = self.size()
            for row in range(rows):
                top = int(row * height / rows)
                bottom = int((row + 1) * height / rows)
                if bottom <= top:
                    continue
                col = 0
                while col < cols:
                    if not self._hit_on(col, row):
                        col += 1
                        continue
                    start = col
                    while col < cols and self._hit_on(col, row):
                        col += 1
                    left = int(start * width / cols)
                    right = int(col * width / cols)
                    if right > left:
                        self.canvas.create_rectangle(
                            left, top, right, bottom, fill=HIT_FILL, outline="", tags=HIT_TAG
                        )
        except Exception as exc:
            if self.logger:
                self.logger.warning("hit volume paint failed: %s", exc)
        finally:
            self.assert_alpha()

    def _apply_alpha(self):
        """窗口整体的透明度：有碰撞体积时几乎全透，只剩"能点中"这一件事。"""
        if self._hit is not None and not self.hidden:
            alpha = HIT_ALPHA
        else:
            alpha = DIM_ALPHA if self.dimmed else 1.0
        try:
            self.root.wm_attributes("-alpha", alpha)
        except Exception as exc:
            if self.logger:
                self.logger.warning("alpha change failed: %s", exc)
            return False
        return True

    # ---------- 透明状态自检 ----------
    def _wanted_alpha(self):
        """现在这个窗口应该有多透明。"""
        if self._hit is not None and not self.hidden:
            return HIT_ALPHA
        return DIM_ALPHA if self.dimmed else 1.0

    def _layered_state(self):
        """读回系统里真实的窗口状态：是不是分层窗口、色键对不对、透明度多少。

        取不到就返回 None，代表这台机器问不出来，此时不硬改窗口。
        """
        try:
            user32 = ctypes.windll.user32
            handle = int(self.root.winfo_id())
            target = int(user32.GetParent(handle) or 0) or handle
            key = ctypes.c_ulong()
            alpha = ctypes.c_ubyte()
            flags = ctypes.c_ulong()
            ok = user32.GetLayeredWindowAttributes(
                target, ctypes.byref(key), ctypes.byref(alpha), ctypes.byref(flags)
            )
        except Exception:
            return None
        if not ok:
            return (False, False, 1.0)
        try:
            rgb = int(str(self.key).lstrip("#"), 16)
        except ValueError:
            rgb = -1
        # 系统里存的是 COLORREF（BGR 排列），两种写法都认。
        bgr = ((rgb & 0xFF) << 16) | (rgb & 0xFF00) | ((rgb >> 16) & 0xFF) if rgb >= 0 else -1
        key_ok = bool(flags.value & 0x1) and key.value in (rgb, bgr)
        return (True, key_ok, alpha.value / 255.0)

    def _reapply_transparent(self):
        """把色键和透明度一起重设一遍，Tk 会把两者重新推给系统。"""
        try:
            self.root.wm_attributes("-transparentcolor", self.key)
        except Exception as exc:
            if self.logger:
                self.logger.warning("transparent color reapply failed: %s", exc)
        return self._apply_alpha()

    def _resume_overlay(self):
        """透明恢复之后，把撤掉的碰撞层补回来。"""
        if not self._overlay_dropped:
            return False
        self._overlay_dropped = False
        self._paint_hit()
        return True

    def assert_alpha(self):
        """确认「窗口是隐形的」这件事还在。

        系统更新、切显示器、别的程序动过窗口样式，都可能把色键或透明度清掉，
        碰撞层就会整块显形。这里读回真实状态，不对就补；补不回来先撤碰撞层，
        宁可爱坏一次点击，也不在桌面上留一块不透明的方块，下一轮自检再补。
        """
        want = self._wanted_alpha()
        state = self._layered_state()
        if state is None:
            return self._apply_alpha()
        layered, key_ok, alpha = state
        if layered and key_ok and abs(alpha - want) <= 0.03:
            self._resume_overlay()
            return True
        if self.logger:
            self.logger.info(
                "pet transparency lost (layered=%s key=%s alpha=%.3f) -> restoring",
                layered,
                key_ok,
                alpha,
            )
        self._reapply_transparent()
        state = self._layered_state()
        if state is not None:
            layered, key_ok, alpha = state
            if layered and key_ok and abs(alpha - want) <= 0.03:
                self._resume_overlay()
                return True
        if not self._overlay_dropped:
            self._overlay_dropped = True
            try:
                self.canvas.delete(HIT_TAG)
            except Exception:
                pass
            if self.logger:
                self.logger.warning("window transparency lost and not restorable; hit layer withdrawn")
        return False

    def keep_on_top(self):
        """把桌宠重新抬到最前。

        别的窗口（尤其自己就置顶的）会把它压下去，压下去就点不着了，
        所以这个动作要反复做，不能只在启动时做一次。
        """
        if not self.topmost:
            return False
        try:
            self.root.attributes("-topmost", True)
            self.root.lift()
            return True
        except Exception as exc:
            if self.logger:
                self.logger.warning("raise pet window failed: %s", exc)
            return False

    def set_topmost(self, enabled):
        """置顶开关。关掉之后窗口可以被别的窗口正常压住。"""
        enabled = bool(enabled)
        try:
            self.root.wm_attributes("-topmost", enabled)
        except Exception as exc:
            if self.logger:
                self.logger.warning("topmost change failed: %s", exc)
            return self.topmost
        self.topmost = enabled
        return self.topmost

    def toggle_hidden(self):
        return self.set_hidden(not self.hidden)

    def _ensure_dot(self):
        if self._dot is None:
            self._dot = tk.Canvas(
                self.root,
                width=DOT_SIZE,
                height=DOT_SIZE,
                bg=self.key,
                highlightthickness=0,
                bd=0,
            )
            self._dot.create_oval(6, 6, DOT_SIZE - 6, DOT_SIZE - 6, fill="#3b3f4d", outline="")
            self._dot.bind("<Button-1>", lambda event: self.set_hidden(False))
        return self._dot
