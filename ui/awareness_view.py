"""屏幕感知页：开没开、看到什么、排除了谁，都在这一页说清楚。

默认关闭。开关在隐私中心，这一页只读状态并改分级、排除名单和临时缓存，
不会自己偷偷开始看屏幕。
"""

import tkinter as tk
from tkinter import ttk

from behavior.awareness import LEVELS, LEVEL_LABELS
from ui import panel, theme


class AwarenessWindow:
    """屏幕感知与工作上下文。数据只从 DeskPet 上的感知服务读。"""

    TITLE = "屏幕感知"
    GEOMETRY = (900, 600)

    def __init__(self, pet, parent=None):
        self.pet = pet
        # 嵌进主工作台是一块 Frame，单独打开才是一个窗口。
        self.embedded = parent is not None
        self.win = panel.container(pet, self.TITLE, self.GEOMETRY, parent)
        self._build()
        self.refresh()

    # ---------- 数据源 ----------
    def _service(self):
        return getattr(self.pet, "awareness", None)

    def _rows(self):
        try:
            return list(self.pet.awareness_rows())
        except Exception:
            return []

    def _snapshot(self):
        service = self._service()
        if service is None:
            return {"ok": False, "note": "这一层没有接入屏幕感知。"}
        try:
            return service.observe()
        except Exception as exc:
            return {"ok": False, "note": "读的时候出错了：%s" % exc}

    def _describe(self):
        service = self._service()
        if service is None:
            return {}
        try:
            return service.describe()
        except Exception:
            return {}

    # ---------- 搭建 ----------
    def _build(self):
        head = tk.Frame(self.win, bg=theme.BG)
        head.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_MD, theme.PAD_SM))
        theme.label(head, "屏幕感知与工作上下文", kind="title").pack(side="left")
        self.state_label = theme.label(head, "", kind="small", fg=theme.MUTED)
        self.state_label.pack(side="left", padx=theme.PAD_MD)
        theme.button(head, "刷新", self.refresh, kind="primary").pack(side="right")
        theme.button(head, "清掉临时上下文", self.clear_context).pack(
            side="right", padx=theme.PAD_SM
        )

        switch = tk.Frame(self.win, bg=theme.BG)
        switch.pack(fill="x", padx=theme.PAD_MD)
        self.on_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            switch,
            text="允许桌宠知道你正在用什么软件",
            variable=self.on_var,
            command=self.toggle,
            bg=theme.BG,
            fg=theme.TEXT,
            selectcolor=theme.FIELD,
            activebackground=theme.BG,
            activeforeground=theme.ACCENT,
            font=theme.FONT_BODY,
            anchor="w",
        ).pack(side="left")
        theme.label(
            switch,
            "默认关闭。开了也只读应用名和窗口标题，不读画面、不记键盘、不上传。",
            kind="small",
            fg=theme.MUTED,
        ).pack(side="left", padx=theme.PAD_SM)

        level_row = tk.Frame(self.win, bg=theme.BG)
        level_row.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_SM, 0))
        theme.label(level_row, "读取到哪一级", kind="small", fg=theme.MUTED).pack(side="left")
        self.level_buttons = {}
        for name in LEVELS:
            button = theme.button(
                level_row,
                LEVEL_LABELS.get(name, name),
                lambda chosen=name: self.pick_level(chosen),
            )
            button.pack(side="left", padx=(theme.PAD_SM, 0))
            self.level_buttons[name] = button

        self.book = ttk.Notebook(self.win)
        self.book.pack(fill="both", expand=True, padx=theme.PAD_MD, pady=theme.PAD_SM)
        for title, builder in (
            ("当前状态", self._build_state),
            ("排除的应用", self._build_exclusions),
            ("能读到什么", self._build_limits),
        ):
            frame = tk.Frame(self.book, bg=theme.BG)
            self.book.add(frame, text=title)
            builder(frame)

        self.hint = theme.label(self.win, "", kind="small", fg=theme.MUTED, anchor="w")
        self.hint.pack(fill="x", padx=theme.PAD_MD)
        panel.close_button(self.win, self).pack(pady=theme.PAD_MD)

    def _build_state(self, parent):
        box = tk.Frame(parent, bg=theme.BG)
        box.pack(fill="both", expand=True, padx=theme.PAD_SM, pady=theme.PAD_SM)
        self.state_tree = ttk.Treeview(
            box, columns=["label", "value"], show="headings", height=10
        )
        for key, title, width in (("label", "项目", 150), ("value", "现在", 620)):
            self.state_tree.heading(key, text=title)
            self.state_tree.column(key, width=width, anchor="w", stretch=(key == "value"))
        scroll = ttk.Scrollbar(box, orient="vertical", command=self.state_tree.yview)
        self.state_tree.configure(yscrollcommand=scroll.set)
        self.state_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        theme.label(
            parent,
            "「可能是」是按进程名和标题猜的类别，不当作事实；拿不准时桌宠会直说不知道。",
            kind="small",
            fg=theme.MUTED,
            anchor="w",
        ).pack(fill="x", padx=theme.PAD_SM)

    def _build_exclusions(self, parent):
        row = tk.Frame(parent, bg=theme.BG)
        row.pack(fill="x", padx=theme.PAD_SM, pady=theme.PAD_SM)
        theme.label(row, "应用名（如 wechat.exe）", kind="small", fg=theme.MUTED).pack(
            side="left"
        )
        self.exclude_entry = theme.entry(row, width=24)
        self.exclude_entry.pack(side="left", padx=theme.PAD_SM)
        theme.button(row, "加进排除名单", self.add_exclusion, kind="primary").pack(side="left")
        theme.button(row, "加入当前应用", self.exclude_current).pack(
            side="left", padx=theme.PAD_SM
        )
        box = tk.Frame(parent, bg=theme.BG)
        box.pack(fill="both", expand=True, padx=theme.PAD_SM)
        self.exclude_tree = ttk.Treeview(box, columns=["app"], show="headings", height=8)
        self.exclude_tree.heading("app", text="不再读取的应用")
        self.exclude_tree.column("app", width=300, anchor="w")
        scroll = ttk.Scrollbar(box, orient="vertical", command=self.exclude_tree.yview)
        self.exclude_tree.configure(yscrollcommand=scroll.set)
        self.exclude_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        theme.button(parent, "放回读取范围", self.remove_exclusion).pack(
            anchor="w", padx=theme.PAD_SM, pady=theme.PAD_SM
        )
        theme.label(
            parent,
            "聊天、会议这类页面本身带私人内容，即使开着感知也只报应用名，不读里面的内容。",
            kind="small",
            fg=theme.MUTED,
            anchor="w",
        ).pack(fill="x", padx=theme.PAD_SM, pady=(0, theme.PAD_SM))

    def _build_limits(self, parent):
        self.limits_tree = ttk.Treeview(
            parent, columns=["label", "value"], show="headings", height=10
        )
        for key, title, width in (("label", "能力", 150), ("value", "现状", 620)):
            self.limits_tree.heading(key, text=title)
            self.limits_tree.column(key, width=width, anchor="w", stretch=(key == "value"))
        self.limits_tree.pack(fill="both", expand=True, padx=theme.PAD_SM, pady=theme.PAD_SM)

    # ---------- 交互 ----------
    def toggle(self):
        wanted = bool(self.on_var.get())
        service = self._service()
        if service is None:
            self._note("这一层没有接入屏幕感知。")
            self.on_var.set(False)
            return False
        if wanted:
            self.pet.toggle_awareness()
            if not service.active():
                # 没开成（比如隐私开关拦着），勾不要留在选中状态。
                self.on_var.set(False)
        else:
            self.pet.set_awareness_level("off")
        self.refresh()
        return service.active()

    def pick_level(self, name):
        if self._service() is None:
            self._note("这一层没有接入屏幕感知。")
            return ""
        picked = self.pet.set_awareness_level(name)
        self.refresh()
        return picked

    def clear_context(self):
        service = self._service()
        if service is None:
            self._note("这一层没有接入屏幕感知。")
            return False
        self.pet.clear_awareness()
        self.refresh()
        return True

    def add_exclusion(self):
        name = str(self.exclude_entry.get() or "").strip()
        if not name:
            self._note("先写上要排除的应用名。")
            return ()
        if self._service() is None:
            self._note("这一层没有接入屏幕感知。")
            return ()
        self.pet.exclude_awareness_app(name)
        try:
            self.exclude_entry.delete(0, tk.END)
        except Exception:
            pass
        self.refresh()
        return self.pet.awareness.exclusions()

    def exclude_current(self):
        snapshot = self._snapshot()
        app = str(snapshot.get("app") or "").strip()
        if not app:
            self._note("现在读不到应用名，没法加。")
            return ()
        self.pet.exclude_awareness_app(app)
        self.refresh()
        return self.pet.awareness.exclusions()

    def remove_exclusion(self):
        picked = self.exclude_tree.selection()
        if not picked:
            self._note("先在下面挑一条。")
            return ()
        app = str(self.exclude_tree.item(picked[0], "values")[0] or "").strip()
        if self._service() is None:
            return ()
        self.pet.include_awareness_app(app)
        self.refresh()
        return self.pet.awareness.exclusions()

    # ---------- 刷新 ----------
    def refresh(self):
        describe = self._describe()
        status = describe.get("status") or "没接入"
        try:
            self.state_label.configure(text=status)
        except Exception:
            pass
        active = bool(describe.get("active"))
        try:
            self.on_var.set(active)
        except Exception:
            pass
        current = str(describe.get("level") or "off")
        for name, button in getattr(self, "level_buttons", {}).items():
            try:
                button.configure(
                    bg=theme.ACCENT if name == current else theme.PANEL_2,
                    fg=theme.ON_ACCENT if name == current else theme.TEXT,
                )
            except Exception:
                continue
        self._fill(self.state_tree, self._rows())
        self._fill(self.exclude_tree, [(app,) for app in describe.get("exclusions", ())])
        self._fill(self.limits_tree, self._limit_rows(describe))
        self._note("" if self._service() is not None else "这一层没有接入屏幕感知。")

    def _limit_rows(self, describe):
        activity = describe.get("activity") or {}
        semantic = describe.get("semantic") or {}
        return [
            ("应用名与窗口标题", "可以读" if activity.get("available") else "读不到"),
            ("界面语义", semantic.get("note") or "读不到"),
            ("画面与截图", "要单独开「截图分析」，并且只在你手动点一下时截"),
            ("键盘与剪贴板", "不读，也不记录"),
            ("聊天、会议页面", "只报应用名，不读内容"),
            ("读到的内容存哪", "只在本地内存里留一小段，随时可以清掉"),
        ]

    def _fill(self, tree, rows):
        try:
            tree.delete(*tree.get_children())
            for row in rows:
                tree.insert("", "end", values=tuple(row))
        except Exception:
            return 0
        return len(rows)

    def _note(self, message):
        try:
            self.hint.configure(text=str(message or ""))
        except Exception:
            pass
        return message
