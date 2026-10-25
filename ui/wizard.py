"""第一次打开时的向导：说清数据放哪、权限怎么给、模型可以不配。

只做说明和跳转，不替用户改任何设置。看过一次就记下来，不再打扰。
"""

import tkinter as tk

from ui import theme

PREFERENCE = "first_run.done"
GEOMETRY = (560, 500)


def _prefs(pet):
    return getattr(pet, "memory", None)


def is_done(pet):
    prefs = _prefs(pet)
    if prefs is None:
        return True
    try:
        stored = prefs.get_preference(PREFERENCE)
    except Exception:
        return True
    if stored is None:
        return False
    return str(stored).strip().lower() not in ("", "0", "false", "no")


def mark_done(pet):
    prefs = _prefs(pet)
    if prefs is None:
        return False
    try:
        prefs.set_preference(PREFERENCE, "1")
        return True
    except Exception:
        return False


def reset(pet):
    """把「已经看过」这一笔擦掉，下次启动会再弹一次。"""
    prefs = _prefs(pet)
    if prefs is None:
        return False
    try:
        prefs.set_preference(PREFERENCE, "0")
        return True
    except Exception:
        return False


def looks_used(pet):
    """已经有记忆或聊过天的库，说明是老用户，不再弹向导。"""
    prefs = _prefs(pet)
    if prefs is None:
        return False
    try:
        if int(prefs.stats().get("total") or 0) > 0:
            return True
        return bool(prefs.sessions(limit=1))
    except Exception:
        return False


def body_text(pet, config):
    """向导正文。路径全部来自真实配置，不写死。"""
    from logkit.report import version_text

    lines = [
        "欢迎使用 %s。" % version_text(),
        "",
        "数据放在哪",
        "  数据目录：%s" % getattr(config, "data_dir", "未配置"),
        "  记忆库：%s" % getattr(config, "db_path", "未配置"),
        "  日志：%s" % getattr(config, "log_dir", "未配置"),
        "  这些都放在用户自己的目录里，不会写进程序目录，卸载程序也不会带走。",
        "",
        "权限怎么给",
        "  默认只读。写文件、跑命令、开程序这类操作会先问你一次。",
        "  授权只对当时那个工具、那个路径有效，可以随时在「能力」里撤销。",
        "  模型不能直接操作系统，所有动作都要过工具和权限这两道。",
        "",
        "模型可以不配",
        "  不配模型也能用：时间、日期、计时器、番茄钟、待办、本地文件、记忆和人格。",
        "  配了模型才会聊天。陪伴模型和任务模型可以分开配，一个都不影响启动。",
        "",
        "角色画面",
        "  程序自带一个角色，第一次启动就放进 %s，想换自己的模型把文件夹丢进去就行。"
        % getattr(config, "live2d_dir", "数据目录"),
        "  画面环境：%s" % environment_text(),
        "  画面起不来时只保留透明拖动区域，聊天、记忆、工具一样都不受影响。",
        "",
        "隐私",
        "  不采集分析数据，不上传聊天内容。密钥只从环境变量或本机配置文件读，",
        "  不写进代码、不进日志，也不会随程序一起发出去。",
    ]
    return "\n".join(lines)


def environment_text():
    """角色画面在这台机器上到底能不能起来：只写查清了的结论，查不出来就直说。"""
    try:
        from avatar.web2d import webview2_note

        return webview2_note()
    except Exception:
        return "查不出渲染环境的状态"


class FirstRunWizard:
    """一个说明窗口，两个出口：去设置，或者知道了。"""

    GEOMETRY = GEOMETRY

    def __init__(self, pet):
        self.pet = pet
        self.config = pet.config
        self.win = tk.Toplevel(pet.root)
        self.win.title("桌宠 · 第一次使用")
        self.win.configure(bg=theme.BG)
        try:
            self.win.attributes("-topmost", True)
        except Exception:
            pass
        width, height = self.GEOMETRY
        try:
            x, y = pet.host.card_position(width, height)
        except Exception:
            x, y = 80, 80
        self.win.geometry("%dx%d+%d+%d" % (width, height, x, y))
        self._build()

    def _build(self):
        wrap = self.GEOMETRY[0] - theme.PAD_MD * 2
        theme.label(self.win, "第一次使用", kind="title").pack(
            anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_MD, theme.PAD_XS)
        )
        area = theme.text_area(self.win, read_only=True, height=18, width=60)
        area.configure(state="normal")
        area.insert("1.0", body_text(self.pet, self.config))
        area.configure(state="disabled")
        area.pack(fill="both", expand=True, padx=theme.PAD_MD)
        self.hint = theme.label(self.win, "", kind="small", fg=theme.MUTED, anchor="w")
        self.hint.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_XS, 0))
        self.hint.configure(text=self._shortcut_state_text())
        foot = tk.Frame(self.win, bg=theme.BG)
        foot.pack(fill="x", padx=theme.PAD_MD, pady=theme.PAD_MD)
        theme.button(foot, "打开设置", self.open_settings).pack(side="left")
        self.shortcut_button = theme.button(foot, "创建桌面快捷方式", self.make_shortcut)
        self.shortcut_button.pack(side="left", padx=(theme.PAD_SM, 0))
        theme.button(
            foot,
            "知道了",
            self.finish,
            kind="primary",
        ).pack(side="right")
        theme.label(
            foot,
            "看过一次就不再弹",
            kind="small",
            fg=theme.MUTED,
        ).pack(side="right", padx=theme.PAD_SM)
        theme.label(self.win, "", kind="small", wraplength=wrap).pack()

    def _shortcut_state_text(self):
        """桌面有没有图标的真实状态。读不到就不说话，不编。"""
        try:
            from security import startup

            state = startup.describe_shortcut()
        except Exception:
            return ""
        if not state.get("supported"):
            return "桌面图标：当前系统不支持，可以跳过。"
        if state.get("exists"):
            return "桌面图标已经在了。"
        return "桌面上还没有图标，点「创建桌面快捷方式」就能建一个。"

    def make_shortcut(self):
        """补一个桌面图标。做不成如实说原因，不假装成功。"""
        try:
            from security import startup

            result = startup.create_desktop_shortcut(
                logger=getattr(self.pet, "logger", None)
            )
        except Exception as exc:
            self.hint.configure(text="没能建快捷方式：%s" % exc)
            return False
        if result.get("ok"):
            self.hint.configure(text="桌面图标已建好：%s" % result.get("path", ""))
            return True
        self.hint.configure(text="没能建快捷方式：%s" % result.get("error", "原因不明"))
        return False

    def open_settings(self):
        pet = self.pet
        try:
            pet.open_settings()
        except Exception as exc:
            self.hint.configure(text="设置打不开：%s" % exc)
            return False
        return True

    def finish(self):
        mark_done(self.pet)
        self.win.destroy()
        return True
