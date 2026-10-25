"""目标与追踪页：把还没做完的任务当目标看，每一步都是里程碑。

只摆任务库里真有的事，取不到就摆空状态。接着做要任务模型，缺了就直说。
"""

import tkinter as tk

from ui import panel, theme

# 里程碑前面的记号：做完了、正在做、还没轮到。
MARKS = {"done": "\u2714", "running": "\u25cf", "todo": "\u25cb"}


class GoalsWindow:
    """在追的目标。数据和状态都从宿主上的任务库读。"""

    TITLE = "目标与追踪"
    GEOMETRY = (880, 620)

    def __init__(self, pet, parent=None):
        self.pet = pet
        # 嵌进主工作台是一块 Frame，单独打开才是一个窗口。
        self.embedded = parent is not None
        self.win = panel.container(pet, self.TITLE, self.GEOMETRY, parent)
        self.goals = []
        self._build()
        self.refresh()

    # ---------- 搭建 ----------
    def _build(self):
        head = tk.Frame(self.win, bg=theme.BG)
        head.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_MD, theme.PAD_SM))
        theme.label(head, "目标与追踪", kind="title").pack(side="left")
        self.state_label = theme.label(head, "", kind="small", fg=theme.MUTED)
        self.state_label.pack(side="left", padx=theme.PAD_MD)
        theme.button(head, "刷新", self.refresh, kind="primary").pack(side="right")

        theme.label(
            self.win,
            "没做完的事就是现在在追的目标。每一步都按任务库里的真实状态摆，不猜。",
            kind="small",
            fg=theme.MUTED,
            anchor="w",
        ).pack(fill="x", padx=theme.PAD_MD)

        self.body = theme.scroll_area(self.win)
        self.hint = theme.label(self.win, "", kind="small", fg=theme.MUTED, anchor="w")
        self.hint.pack(fill="x", padx=theme.PAD_MD)
        panel.close_button(self.win, self).pack(pady=theme.PAD_MD)

    # ---------- 数据 ----------
    def refresh(self):
        """重新读目标。读不到就摆空状态，不编。"""
        for child in list(self.body.winfo_children()):
            try:
                child.destroy()
            except Exception:
                continue
        try:
            self.goals = list(self.pet.goal_cards() or [])
        except Exception as exc:
            self.goals = []
            self._hint("读目标失败：%s" % exc)
        for goal in self.goals:
            self._paint(goal)
        if not self.goals:
            self._paint_empty()
        try:
            self.state_label.config(text="在追 %d 件" % len(self.goals))
        except Exception:
            pass
        return self.goals

    def _paint_empty(self):
        box = theme.card(self.body)
        box.pack(fill="x", padx=theme.PAD_MD, pady=theme.PAD_SM)
        theme.label(box, "还没有在追的目标", kind="title", bg=theme.CARD).pack(
            anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_SM, 0)
        )
        theme.label(
            box,
            "说清你想要什么，我会先给计划再动手。计划一落库，这里就有目标了。",
            kind="small",
            bg=theme.CARD,
            fg=theme.MUTED,
            anchor="w",
            justify="left",
        ).pack(anchor="w", fill="x", padx=theme.PAD_MD, pady=(0, theme.PAD_SM))
        return box

    def _paint(self, goal):
        box = theme.card(self.body)
        box.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_SM, 0))
        title = theme.label(
            box,
            "%s（%s）" % (goal.get("goal") or "（没写目标）", goal.get("state_label") or ""),
            kind="title",
            bg=theme.CARD,
            fg=theme.TEXT,
            anchor="w",
            justify="left",
        )
        title.pack(anchor="w", fill="x", padx=theme.PAD_MD, pady=(theme.PAD_SM, 0))
        theme.label(
            box,
            self.progress_text(goal),
            kind="small",
            bg=theme.CARD,
            fg=theme.MUTED,
            anchor="w",
        ).pack(anchor="w", fill="x", padx=theme.PAD_MD)
        for line in self.milestone_lines(goal):
            theme.label(
                box, line, kind="small", bg=theme.CARD, fg=theme.MUTED, anchor="w"
            ).pack(anchor="w", fill="x", padx=theme.PAD_MD)
        row = tk.Frame(box, bg=theme.CARD)
        row.pack(anchor="w", fill="x", padx=theme.PAD_MD, pady=(theme.PAD_XS, theme.PAD_SM))
        theme.button(row, "打开任务中心", lambda: self.open_tasks(goal)).pack(side="left")
        theme.button(row, "接着做", lambda: self.continue_goal(goal)).pack(
            side="left", padx=theme.PAD_SM
        )
        return box

    # ---------- 文案 ----------
    def progress_text(self, goal):
        total = int(goal.get("total") or 0)
        done = int(goal.get("done") or 0)
        if not total:
            return "还没有列步骤。"
        return "已完成 %d / 共 %d 步。" % (done, total)

    def milestone_lines(self, goal):
        """里程碑那一块，按真实状态取记号。"""
        rows = []
        for item in goal.get("milestones") or []:
            status = "done" if item.get("done") else ("running" if item.get("running") else "todo")
            rows.append("%s %s" % (MARKS[status], item.get("title")))
        if rows:
            return rows
        return ["还没有列步骤。"]

    # ---------- 动作 ----------
    def open_tasks(self, goal):
        """去任务中心看这条的详细记录。"""
        return self.pet._chat_open("tasks")

    def continue_goal(self, goal):
        """接着做：没有任务模型就如实说，不假装已经开工。"""
        if goal.get("needs_model"):
            self._hint("要接着做需要先配好任务模型，配好之后点这里就能继续。")
            return False
        text = "接着做上次没做完的那件事：%s" % str(goal.get("goal") or "")[:40]
        self.pet.prefill_chat(text)
        self._hint("已经把这件事放回输入框，你看一眼再发。")
        return text

    # ---------- 给测试看的内容 ----------
    def lines(self):
        """这一页真正写出来的字，一行一条目标。"""
        rows = []
        for goal in self.goals:
            rows.append(
                " / ".join(
                    [str(goal.get("goal") or ""), str(goal.get("state_label") or "")]
                    + [self.progress_text(goal)]
                    + self.milestone_lines(goal)
                )
            )
        return rows

    def text(self):
        """这一页的字，连空状态一起。测试拿它检查页面到底写了什么。"""
        rows = self.lines()
        if rows:
            return rows
        return ["还没有在追的目标"]

    def _hint(self, text):
        try:
            self.hint.config(text=str(text or ""))
        except Exception:
            return None
        return text