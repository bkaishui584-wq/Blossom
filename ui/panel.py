"""功能页的统一容器：既能挂在主工作台中央，也能单独开成窗口。

V8 之后人格、记忆、设置、权限、技能这些页面都进主窗口，所以默认走嵌入；
单独开窗只留给还要兼容的老调用方。业务状态一律放在宿主上，容器不碰。
"""

import tkinter as tk

from ui import theme


def container(pet, title, geometry, parent=None):
    """给功能页一个容器：嵌入时是一块 Frame，单独开时是一个窗口。"""
    if parent is not None:
        frame = tk.Frame(parent, bg=theme.BG)
        frame.pack(fill="both", expand=True)
        return frame
    win = tk.Toplevel(pet.root)
    win.title("桌宠 · %s" % title)
    win.configure(bg=theme.BG)
    try:
        win.attributes("-topmost", True)
    except Exception:
        pass
    width, height = geometry
    try:
        x, y = pet.host.card_position(width, height)
    except Exception:
        x = y = 120
    win.geometry("%dx%d+%d+%d" % (width, height, x, y))
    try:
        from ui import glass_native

        glass_native.apply_window(win, radius=theme.RADIUS_LG, alpha=0.99, tint=theme.CARD)
    except Exception:
        pass
    return win


def is_embedded(panel):
    return bool(getattr(panel, "embedded", False))


def close(panel):
    """关闭：嵌在主窗口里就回聊天，单独开窗才真的关掉窗口。"""
    if is_embedded(panel):
        pet = getattr(panel, "pet", None)
        handler = getattr(pet, "close_panel", None)
        if callable(handler):
            return handler()
        return False
    try:
        panel.win.destroy()
    except Exception:
        return False
    return True


def close_button(master, panel, **kw):
    """容器底部的关闭按钮。

    单开窗口时是「关闭」；嵌在主工作台时顶栏已经有「返回聊天」，
    这里只回一个空占位，调用方照旧 pack，界面上不重复摆按钮。
    """
    if is_embedded(panel):
        return tk.Frame(master, bg=theme.BG, height=1)
    return theme.button(master, "关闭", lambda: close(panel), kind="primary", **kw)
