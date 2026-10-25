"""计划任务页：持久任务、下一次执行时间和真实运行记录。"""

from datetime import datetime
import tkinter as tk
from tkinter import ttk

from ui import panel, theme


def _stamp(value):
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return ""
    if number <= 0:
        return ""
    return datetime.fromtimestamp(number).strftime("%m-%d %H:%M:%S")


class ScheduleWindow:
    TITLE = "计划任务"
    GEOMETRY = (940, 620)

    def __init__(self, pet, parent=None):
        self.pet = pet
        self.embedded = parent is not None
        self.win = panel.container(pet, self.TITLE, self.GEOMETRY, parent)
        self._jobs = {}
        self._build()
        self.refresh()

    def scheduler(self):
        return getattr(self.pet, "scheduler", None)

    def _build(self):
        header = theme.card(self.win)
        header.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_MD, theme.PAD_SM))
        head = tk.Frame(header, bg=theme.CARD)
        head.pack(fill="x", padx=theme.PAD_MD, pady=theme.PAD_SM)
        theme.label(head, "计划任务", kind="title", bg=theme.CARD).pack(side="left")
        self.stats = theme.label(head, "", kind="small", fg=theme.MUTED, bg=theme.CARD)
        self.stats.pack(side="left", padx=theme.PAD_MD)
        theme.button(head, "刷新", self.refresh, kind="primary").pack(side="right")
        theme.button(head, "取消选中", self.cancel_selected, kind="danger").pack(
            side="right", padx=theme.PAD_SM
        )
        theme.button(head, "确认重试", self.resume_selected).pack(
            side="right", padx=theme.PAD_SM
        )

        body = tk.Frame(self.win, bg=theme.BG)
        body.pack(fill="both", expand=True, padx=theme.PAD_MD, pady=theme.PAD_SM)

        left = theme.card(body)
        left.pack(side="left", fill="both", expand=True, padx=(0, theme.PAD_SM))
        columns = ("name", "state", "due", "attempts", "error")
        self.jobs = ttk.Treeview(left, columns=columns, show="headings", height=16)
        for key, label, width in (
            ("name", "任务", 180),
            ("state", "状态", 80),
            ("due", "下次执行", 120),
            ("attempts", "尝试", 55),
            ("error", "说明", 230),
        ):
            self.jobs.heading(key, text=label)
            self.jobs.column(key, width=width, anchor="w", stretch=(key in ("name", "error")))
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.jobs.yview)
        self.jobs.configure(yscrollcommand=scroll.set)
        self.jobs.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.jobs.bind("<<TreeviewSelect>>", self._picked)

        right = theme.card(body)
        right.pack(side="left", fill="both")
        right.configure(width=330)
        right.pack_propagate(False)
        theme.label(right, "执行历史", kind="body", bg=theme.CARD).pack(
            anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_MD, theme.PAD_XS)
        )
        self.runs = ttk.Treeview(
            right, columns=("at", "state", "attempt", "error"), show="headings", height=14
        )
        for key, label, width in (
            ("at", "时间", 92),
            ("state", "状态", 80),
            ("attempt", "次", 36),
            ("error", "结果", 110),
        ):
            self.runs.heading(key, text=label)
            self.runs.column(key, width=width, anchor="w", stretch=(key == "error"))
        self.runs.pack(fill="both", expand=True, padx=theme.PAD_SM)

        self.hint = theme.label(
            header, "", kind="small", fg=theme.MUTED, bg=theme.CARD,
            wraplength=850, justify="left",
        )
        self.hint.pack(fill="x", padx=theme.PAD_MD, pady=(0, theme.PAD_SM))
        panel.close_button(self.win, self).pack(pady=theme.PAD_SM)

    def refresh(self):
        scheduler = self.scheduler()
        self.jobs.delete(*self.jobs.get_children())
        self._jobs = {}
        if scheduler is None:
            self.stats.configure(text="没有接上调度器")
            self.hint.configure(text="当前运行没有持久调度器，不能查看计划任务。")
            return 0
        rows = list(scheduler.job_rows(limit=100))
        for row in rows:
            job_id = str(row.get("id") or "")
            self._jobs[job_id] = row
            when = _stamp(row.get("next_run_at") or row.get("due_at"))
            error = str(row.get("error") or "")
            self.jobs.insert(
                "",
                "end",
                iid=job_id,
                values=(
                    row.get("name") or row.get("event") or job_id,
                    row.get("state_label") or row.get("state") or "",
                    when,
                    "%s/%s" % (row.get("attempts", 0), row.get("max_attempts", 0)),
                    error[:120],
                ),
            )
        stats = scheduler.job_stats()
        self.stats.configure(
            text="共 %d 条 · 待执行 %d · 失败 %d · 等待确认 %d"
            % (
                stats.get("total", 0),
                stats.get("pending", 0),
                stats.get("failed", 0),
                stats.get("waiting_confirmation", 0),
            )
        )
        self.hint.configure(
            text="计划任务保存在本地 SQLite。完成后不会重复派发；非幂等任务中断后不会自动重跑。"
        )
        return len(rows)

    def _picked(self, event=None):
        picked = self.jobs.selection()
        if not picked:
            return None
        job_id = str(picked[0])
        self._paint_runs(job_id)
        return job_id

    def _paint_runs(self, job_id):
        self.runs.delete(*self.runs.get_children())
        scheduler = self.scheduler()
        if scheduler is None:
            return []
        rows = scheduler.job_runs(job_id=job_id, limit=100)
        for row in rows:
            self.runs.insert(
                "",
                "end",
                values=(
                    _stamp(row.get("finished_at") or row.get("started_at")),
                    row.get("state") or "",
                    row.get("attempt") or 0,
                    (row.get("error") or row.get("result") or "")[:80],
                ),
            )
        return rows

    def cancel_selected(self):
        picked = self.jobs.selection()
        if not picked:
            self.hint.configure(text="先在左边选一条待执行的计划任务。")
            return False
        scheduler = self.scheduler()
        if scheduler is None:
            return False
        job_id = str(picked[0])
        ok = scheduler.cancel_job(job_id)
        self.hint.configure(text="已取消。" if ok else "这条任务已经不在待执行状态。")
        self.refresh()
        return ok

    def resume_selected(self):
        picked = self.jobs.selection()
        if not picked:
            self.hint.configure(text="先在左边选一条失败或等待确认的任务。")
            return False
        scheduler = self.scheduler()
        if scheduler is None:
            return False
        job_id = str(picked[0])
        ok = scheduler.resume_job(job_id)
        self.hint.configure(
            text="已重新排队。" if ok else "只有失败或等待确认的任务需要你手动重试。"
        )
        self.refresh()
        return ok


__all__ = ["ScheduleWindow"]
