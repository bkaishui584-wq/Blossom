"""任务中心：看得见、查得到、能取消、能接着做。

只读任务库里的记录，动作只有三个：取消正在跑的那一条、把某条的目标放回聊天输入框、
刷新。界面不直接执行任何工具，也不替用户发请求；接着做要用户自己按发送，
照旧过一遍权限与确认。
"""

import os
import re
import tkinter as tk
from tkinter import ttk

from agent.task import TASK_STATE_LABELS, TERMINAL_STATES
from ui import panel, theme

# 从步骤结果里挑路径用的分隔符。只认长得像路径的片段，存在与否另说。
_PATH_SPLIT = re.compile(r"[\s,;\uff0c\uff1b]+")


def paths_in(text):
    """从一段文字里挑出像路径的片段。不猜文件名，只做粗筛。"""
    out = []
    for raw in _PATH_SPLIT.split(str(text or "")):
        token = raw.strip().strip("\"'`()[]{}\uff0c\u3002\uff1a:")
        if len(token) < 3:
            continue
        if "/" in token or "\\" in token:
            out.append(token)
    return out

FILTERS = (
    ("全部", ""),
    ("进行中", "open"),
    ("已完成", "completed"),
    ("失败", "failed"),
    ("已取消", "cancelled"),
)

STEP_STATUS_LABELS = {
    "pending": "待办",
    "running": "进行中",
    "done": "完成",
    "failed": "失败",
    "skipped": "跳过",
}


class TaskWindow:
    """任务中心。数据全部从任务库只读拿。"""
    TITLE = "任务中心"


    GEOMETRY = (980, 620)

    def __init__(self, pet, parent=None):

        self.pet = pet
        # 嵌进主工作台是一块 Frame，单独打开才是一个窗口。
        self.embedded = parent is not None
        self.win = panel.container(pet, self.TITLE, self.GEOMETRY, parent)
        self.filter = ""
        self.selected = ""
        self._items = {}
        self._build()
        self.refresh()

    # ---------- 只读数据源 ----------
    def _store(self):
        return getattr(getattr(self.pet, "agent", None), "task_store", None)

    def tasks(self, limit=50):
        store = self._store()
        if store is None:
            return []
        try:
            return list(store.recent(limit=limit))
        except Exception:
            return []

    def steps_of(self, task_id):
        store = self._store()
        if store is None:
            return []
        try:
            return [dict(row) for row in store.steps(task_id)]
        except Exception:
            return []

    def products_of(self, task_id):
        """这条任务碰过的文件：只列真在磁盘上的路径，编不出来就不列。"""
        found = []
        seen = set()
        for step in self.steps_of(task_id):
            for key in ("result", "verify"):
                for path in paths_in(step.get(key)):
                    real = os.path.abspath(path)
                    if real in seen or not os.path.exists(real):
                        continue
                    seen.add(real)
                    found.append(real)
        return found

    def rows(self, wanted=None):
        """给界面和测试共用的一份扁平结果。"""
        want = self.filter if wanted is None else str(wanted or "")
        out = []
        for record in self.tasks():
            state = str(getattr(record, "state", "") or "")
            if want == "open" and state in TERMINAL_STATES:
                continue
            if want and want != "open" and state != want:
                continue
            out.append(
                {
                    "id": str(getattr(record, "id", "")),
                    "goal": str(getattr(record, "goal", "")),
                    "state": state,
                    "state_label": TASK_STATE_LABELS.get(state, state),
                    "project": str(getattr(record, "project", "") or ""),
                    "session": str(getattr(record, "session", "") or ""),
                    "attempts": int(getattr(record, "attempts", 0) or 0),
                    "error": str(getattr(record, "error", "") or ""),
                    "updated_at": str(getattr(record, "updated_at", "") or ""),
                    "created_at": str(getattr(record, "created_at", "") or ""),
                }
            )
        return out

    def counts(self):
        out = {}
        for row in self.rows(""):
            out[row["state"]] = out.get(row["state"], 0) + 1
        return out

    def in_flight(self):
        checker = getattr(self.pet, "_task_in_flight", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                return False
        return False

    def describe_task(self, task_id):
        """把一条任务摊成给人看的几行字。"""
        row = None
        for item in self.rows(""):
            if item["id"] == task_id:
                row = item
                break
        if row is None:
            return []
        lines = [
            ("目标", row["goal"] or "(没写目标)"),
            ("状态", row["state_label"]),
            ("目录", row["project"] or "没指定"),
            ("会话", row["session"] or "没记录"),
            ("重试", "%d 次" % row["attempts"]),
            ("创建", row["created_at"]),
            ("更新", row["updated_at"]),
        ]
        if row["error"]:
            lines.append(("错误", row["error"]))
        products = self.products_of(task_id)
        for path in products[:8]:
            lines.append(("产物", path))
        steps = self.steps_of(task_id)
        if not steps:
            lines.append(("步骤", "没有分步计划，这条是一步做完的"))
            return lines
        for step in steps:
            status = STEP_STATUS_LABELS.get(str(step.get("status") or ""), str(step.get("status") or ""))
            title = str(step.get("title") or "").strip() or "第 %s 步" % (int(step.get("step_index") or 0) + 1)
            tool = str(step.get("tool") or "")
            head = "第 %s 步 · %s · %s" % (int(step.get("step_index") or 0) + 1, title, status)
            if tool:
                head += " · %s" % tool
            lines.append(("步骤", head))
            for key, label in (("result", "结果"), ("error", "错误"), ("verify", "校验")):
                value = str(step.get(key) or "").strip()
                if value:
                    lines.append(("", "%s：%s" % (label, value)))
        return lines

    # ---------- 动作 ----------
    def cancel_current(self):
        """取消正在跑的那一条。没有在跑的就如实说。"""
        if not self.in_flight():
            self._say("现在没有正在跑的任务。")
            return False
        cancel = getattr(self.pet, "cancel_task", None)
        if not callable(cancel):
            cancel = getattr(getattr(self.pet, "agent", None), "cancel_task", None)
        if not callable(cancel):
            self._say("这一层没有取消入口。")
            return False
        try:
            cancel()
        except Exception as exc:
            self._say("取消失败：%s" % exc)
            return False
        self._say("已取消。")
        self.refresh()
        return True

    def resume(self):
        """把这条的目标放回输入框。发不发由用户决定，程序不替发。"""
        task_id = self.selected
        row = None
        for item in self.rows(""):
            if item["id"] == task_id:
                row = item
                break
        if row is None:
            self._say("先在上面选一条任务。")
            return False
        goal = row["goal"].strip()
        if not goal:
            self._say("这条任务没有留下目标，没法接着做。")
            return False
        put = getattr(self.pet, "prefill_chat", None)
        if not callable(put):
            self._say("拿不到聊天输入框。")
            return False
        put(goal)
        self._say("目标已放进输入框，确认后按发送。")
        return goal

    def detail_text(self):
        if not self.selected:
            return "左边选一条任务，这里显示它的目标和每一步。"
        lines = self.describe_task(self.selected)
        if not lines:
            return "这条任务的记录读不到了。"
        return "\n".join(
            ("%s：%s" % (label, value)) if label else value for label, value in lines
        )

    # ---------- 界面 ----------
    def _build(self):
        header = theme.card(self.win)
        header.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_MD, theme.PAD_SM))
        top = tk.Frame(header, bg=theme.CARD)
        top.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_SM, theme.PAD_XS))
        theme.label(top, "任务中心", kind="title").pack(side="left")
        self.counts_label = theme.label(top, "", kind="small", fg=theme.MUTED)
        self.counts_label.pack(side="left", padx=theme.PAD_MD)
        theme.button(top, "计划任务", self._open_schedule).pack(side="right")

        row = tk.Frame(header, bg=theme.CARD)
        row.pack(fill="x", padx=theme.PAD_MD, pady=(0, theme.PAD_SM))
        for label, value in FILTERS:
            theme.button(
                row,
                label,
                lambda chosen=value: self.set_filter(chosen),
                kind="primary" if value == self.filter else "ghost",
            ).pack(side="left", padx=(0, theme.PAD_SM))
        self.cancel_button = theme.button(row, "取消正在跑的任务", self.cancel_current, kind="danger")
        self.cancel_button.pack(side="right")

        body = tk.Frame(self.win, bg=theme.BG)
        body.pack(fill="both", expand=True, padx=theme.PAD_MD, pady=theme.PAD_SM)

        left = theme.card(body)
        left.pack(side="left", fill="both", expand=True, padx=(0, theme.PAD_SM))
        columns = ("state", "goal", "updated")
        self.list = ttk.Treeview(left, columns=columns, show="headings", height=14)
        for name, title, width in (
            ("state", "状态", 72),
            ("goal", "目标", 320),
            ("updated", "更新", 130),
        ):
            self.list.heading(name, text=title)
            self.list.column(name, width=width, anchor="w")
        self.list.pack(side="left", fill="both", expand=True)
        bar = ttk.Scrollbar(left, orient="vertical", command=self.list.yview)
        bar.pack(side="right", fill="y")
        self.list.configure(yscrollcommand=bar.set)
        self.list.bind("<<TreeviewSelect>>", self._on_pick)

        right = theme.card(body)
        right.pack(side="left", fill="both", expand=True)
        self.detail = tk.Text(
            right,
            wrap="word",
            height=14,
            bg=theme.FIELD,
            fg=theme.TEXT,
            bd=0,
            font=theme.FONT_BODY,
        )
        self.detail.pack(fill="both", expand=True)
        self.detail.configure(state="disabled")

        foot = theme.card(self.win)
        foot.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_SM, theme.PAD_MD))
        inner = tk.Frame(foot, bg=theme.CARD)
        inner.pack(fill="x", padx=theme.PAD_MD, pady=theme.PAD_SM)
        foot = inner
        theme.button(foot, "接着做（放回输入框）", self.resume, kind="primary").pack(side="left")
        theme.button(foot, "刷新", self.refresh).pack(side="left", padx=theme.PAD_SM)
        self.hint = theme.label(foot, "", kind="small", fg=theme.MUTED)
        self.hint.pack(side="left", padx=theme.PAD_SM)
        panel.close_button(foot, self).pack(side="right")
        theme.cardify(header)
        theme.cardify(left)
        theme.cardify(right)
        theme.cardify(inner)

    def _open_schedule(self):
        opener = getattr(self.pet, "open_schedule", None)
        return opener() if callable(opener) else None

    def _say(self, text):
        try:
            self.hint.configure(text=str(text))
        except Exception:
            pass
        return text

    def set_filter(self, value):
        self.filter = str(value or "")
        self.refresh()
        return self.filter

    def _on_pick(self, event=None):
        picked = self.list.selection()
        if not picked:
            return None
        self.selected = str(picked[0])
        self._paint_detail()
        return self.selected

    def _paint_detail(self):
        try:
            self.detail.configure(state="normal")
            self.detail.delete("1.0", "end")
            self.detail.insert("1.0", self.detail_text())
            self.detail.configure(state="disabled")
        except Exception:
            pass

    def refresh(self):
        """重画列表。选中过的那条还在就继续选中。"""
        try:
            self.list.delete(*self.list.get_children())
        except Exception:
            pass
        self._items = {}
        rows = self.rows()
        for row in rows:
            goal = row["goal"] or "(没写目标)"
            self._items[row["id"]] = row
            try:
                self.list.insert(
                    "",
                    "end",
                    iid=row["id"],
                    values=(row["state_label"], goal[:60], row["updated_at"]),
                )
            except Exception:
                continue
        if self.selected in self._items:
            try:
                self.list.selection_set(self.selected)
            except Exception:
                pass
        elif rows:
            self.selected = rows[0]["id"]
            try:
                self.list.selection_set(self.selected)
            except Exception:
                pass
        else:
            self.selected = ""
        counts = self.counts()
        try:
            self.counts_label.configure(
                text="共 %d 条（完成 %d，失败 %d，取消 %d，其他 %d）"
                % (
                    len(self.rows("")),
                    counts.get("completed", 0),
                    counts.get("failed", 0),
                    counts.get("cancelled", 0),
                    len(self.rows("")) - counts.get("completed", 0) - counts.get("failed", 0) - counts.get("cancelled", 0),
                )
            )
        except Exception:
            pass
        self._paint_detail()
        return len(rows)
