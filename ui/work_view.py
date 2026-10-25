"""Work mode page: explicit consent, local intervals, reports and deletion."""

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ui import panel, theme


class WorkWindow:
    TITLE = "工作记录"
    GEOMETRY = (920, 640)

    def __init__(self, pet, parent=None):
        self.pet = pet
        self.embedded = parent is not None
        self.win = panel.container(pet, self.TITLE, self.GEOMETRY, parent)
        self._build()
        self.refresh()

    def _service(self):
        return getattr(self.pet, "worklog", None)

    def _build(self):
        pad = theme.PAD_MD
        head = tk.Frame(self.win, bg=theme.BG)
        head.pack(fill="x", padx=pad, pady=(pad, theme.PAD_SM))
        theme.label(head, "工作记录", kind="title").pack(side="left")
        self.state_label = theme.label(head, "", kind="small", fg=theme.MUTED)
        self.state_label.pack(side="left", padx=pad)
        theme.button(head, "刷新", self.refresh, kind="primary").pack(side="right")

        self.on_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            head,
            text="允许记录前台应用与时长",
            variable=self.on_var,
            command=self.toggle,
            bg=theme.BG,
            fg=theme.TEXT,
            selectcolor=theme.FIELD,
            activebackground=theme.BG,
            activeforeground=theme.ACCENT,
            font=theme.FONT_BODY,
        ).pack(side="right", padx=pad)

        theme.label(
            self.win,
            "默认关闭。开启后只记录前台应用、窗口标题、开始与结束时间；聊天类应用只留应用名。"
            "不截屏、不录音、不记键盘、不读网页正文，也不上传。",
            kind="small",
            fg=theme.MUTED,
            wraplength=860,
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=pad)

        controls = tk.Frame(self.win, bg=theme.BG)
        controls.pack(fill="x", padx=pad, pady=theme.PAD_SM)
        theme.button(controls, "暂停记录", self.pause).pack(side="left")
        theme.button(controls, "恢复记录", self.resume).pack(side="left", padx=theme.PAD_SM)
        theme.button(controls, "关闭并停止采集", self.stop, kind="danger").pack(side="left")

        fields = tk.Frame(self.win, bg=theme.BG)
        fields.pack(fill="x", padx=pad, pady=(0, theme.PAD_SM))
        theme.label(fields, "当前项目", kind="small", fg=theme.MUTED).pack(side="left")
        self.project_var = tk.StringVar(value="")
        theme.entry(fields, textvariable=self.project_var, width=20).pack(
            side="left", padx=theme.PAD_SM
        )
        theme.button(fields, "应用项目", self.save_project).pack(side="left")

        theme.label(fields, "保留天数", kind="small", fg=theme.MUTED).pack(
            side="left", padx=(theme.PAD_MD, theme.PAD_SM)
        )
        self.retention_var = tk.StringVar(value="30")
        theme.entry(fields, textvariable=self.retention_var, width=6).pack(side="left")
        theme.button(fields, "应用", self.save_retention).pack(side="left", padx=theme.PAD_SM)

        exclude_row = tk.Frame(self.win, bg=theme.BG)
        exclude_row.pack(fill="x", padx=pad, pady=(0, theme.PAD_SM))
        theme.label(exclude_row, "排除应用", kind="small", fg=theme.MUTED).pack(side="left")
        self.exclude_var = tk.StringVar(value="")
        theme.entry(exclude_row, textvariable=self.exclude_var, width=18).pack(
            side="left", padx=theme.PAD_SM
        )
        theme.button(exclude_row, "加入排除", self.add_exclusion).pack(side="left")
        theme.button(exclude_row, "移除排除", self.remove_exclusion).pack(
            side="left", padx=theme.PAD_SM
        )
        self.exclude_label = theme.label(exclude_row, "", kind="small", fg=theme.MUTED)
        self.exclude_label.pack(side="left", padx=theme.PAD_SM)

        self.book = ttk.Notebook(self.win)
        self.book.pack(fill="both", expand=True, padx=pad, pady=(0, theme.PAD_SM))
        self.today_area = self._report_tab("今日")
        self.week_area = self._report_tab("本周")
        self.timeline_tree = self._timeline_tab()
        self.audit_area = self._audit_tab()

        bottom = tk.Frame(self.win, bg=theme.BG)
        bottom.pack(fill="x", padx=pad, pady=(0, theme.PAD_SM))
        theme.button(bottom, "删除今天的记录", self.delete_today).pack(side="left")
        theme.button(bottom, "导出 CSV…", self.export_csv).pack(
            side="left", padx=theme.PAD_SM
        )
        theme.button(bottom, "删除全部记录", self.clear_all, kind="danger").pack(side="left")
        self.hint = theme.label(self.win, "", kind="small", fg=theme.MUTED, anchor="w")
        self.hint.pack(fill="x", padx=pad)
        panel.close_button(self.win, self).pack(pady=theme.PAD_MD)

    def _report_tab(self, title):
        frame = tk.Frame(self.book, bg=theme.BG)
        self.book.add(frame, text=title)
        area = theme.text_area(frame, read_only=True, height=12, width=1)
        area.pack(fill="both", expand=True, padx=theme.PAD_SM, pady=theme.PAD_SM)
        return area

    def _timeline_tab(self):
        frame = tk.Frame(self.book, bg=theme.BG)
        self.book.add(frame, text="时间线")
        columns = ("start", "end", "app", "project", "duration")
        tree = ttk.Treeview(frame, columns=columns, show="headings", height=10)
        for key, title, width in (
            ("start", "开始", 130),
            ("end", "结束", 130),
            ("app", "应用", 220),
            ("project", "项目", 150),
            ("duration", "时长", 90),
        ):
            tree.heading(key, text=title)
            tree.column(key, width=width, stretch=(key in ("app", "project")), anchor="w")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True, padx=theme.PAD_SM, pady=theme.PAD_SM)
        scroll.pack(side="right", fill="y", pady=theme.PAD_SM)
        return tree

    def _audit_tab(self):
        frame = tk.Frame(self.book, bg=theme.BG)
        self.book.add(frame, text="隐私审计")
        area = theme.text_area(frame, read_only=True, height=12, width=1)
        area.pack(fill="both", expand=True, padx=theme.PAD_SM, pady=theme.PAD_SM)
        return area

    def refresh(self):
        service = self._service()
        if service is None:
            self._hint("工作记录没有接入。")
            self.state_label.config(text="未接入")
            return {}
        try:
            state = service.state()
            day = service.report()
            week = service.report(weekly=True)
            rows = service.records()
            audit = service.audit_rows()
        except Exception as exc:
            self._hint("读取工作记录失败：%s" % exc)
            return {}
        self.on_var.set(bool(state.get("active")))
        self.project_var.set(state.get("project") or "")
        self.retention_var.set(str(state.get("retention_days") or 30))
        label = "正在记录" if state.get("active") else (
            "已暂停" if state.get("enabled") else "未开启"
        )
        self.state_label.config(text=label)
        self._fill(self.today_area, day.get("text") or "没有工作记录。")
        self._fill(self.week_area, week.get("text") or "没有工作记录。")
        for item in self.timeline_tree.get_children():
            self.timeline_tree.delete(item)
        for row in rows:
            self.timeline_tree.insert(
                "",
                "end",
                values=(
                    row.get("start_label"),
                    row.get("end_label"),
                    row.get("app"),
                    row.get("project") or "",
                    row.get("duration"),
                ),
            )
        audit_lines = [
            "%s  %s：%s" % (row.get("at"), row.get("action"), row.get("detail"))
            for row in audit
        ]
        self._fill(self.audit_area, "\n".join(audit_lines) or "还没有操作记录。")
        self._hint(
            "采集方式：%s；可用：%s。关闭或暂停后不会写入新的活动记录。"
            % (state.get("provider") or "未知", "是" if state.get("available") else "否")
        )
        self.exclude_label.config(
            text="、".join(state.get("exclusions") or []) or "没有排除应用"
        )
        return state

    def toggle(self):
        service = self._service()
        if service is None:
            self.on_var.set(False)
            return False
        wanted = bool(self.on_var.get())
        if not wanted:
            return self.stop()
        approved = messagebox.askyesno(
            "开启工作记录",
            "开启后会记录前台应用名、窗口标题、开始与结束时间，并保存在本机数据库。\n\n"
            "不截屏、不录音、不记键盘、不读网页正文，也不上传。\n"
            "可以随时暂停、关闭或删除记录。现在开启吗？",
            parent=self.win,
        )
        if not approved:
            self.on_var.set(False)
            self._hint("没有开启。")
            return False
        ok = self.pet.enable_worklog(consent=True)
        self.refresh()
        return ok

    def pause(self):
        ok = self.pet.pause_worklog() if self._service() is not None else False
        self.refresh()
        return ok

    def resume(self):
        ok = self.pet.resume_worklog() if self._service() is not None else False
        self.refresh()
        return ok

    def stop(self):
        ok = self.pet.stop_worklog() if self._service() is not None else False
        self.refresh()
        return ok

    def save_project(self):
        service = self._service()
        if service is None:
            return False
        ok = service.set_project(self.project_var.get())
        self.refresh()
        self._hint("项目已更新。" if ok else "项目没存下来。")
        return ok

    def save_retention(self):
        service = self._service()
        if service is None:
            return 0
        value = service.set_retention_days(self.retention_var.get())
        self.refresh()
        self._hint("保留 %d 天，过期记录已清理。" % value)
        return value

    def add_exclusion(self):
        service = self._service()
        if service is None:
            return False
        ok = service.add_exclusion(self.exclude_var.get())
        self.refresh()
        self._hint("已加入排除。" if ok else "应用名不能为空。")
        return ok

    def remove_exclusion(self):
        service = self._service()
        if service is None:
            return False
        ok = service.remove_exclusion(self.exclude_var.get())
        self.refresh()
        self._hint("已移出排除。" if ok else "应用名不能为空。")
        return ok

    def delete_today(self):
        service = self._service()
        if service is None or not messagebox.askyesno(
            "删除今天的记录",
            "删除今天的工作记录吗？删除后不能从本机恢复。",
            parent=self.win,
        ):
            return 0
        count = service.delete_day()
        self.refresh()
        self._hint("删除了 %d 条记录。" % count)
        return count

    def clear_all(self):
        service = self._service()
        if service is None or not messagebox.askyesno(
            "删除全部记录",
            "删除全部工作记录吗？应用、人格和聊天记录都不受影响。",
            parent=self.win,
        ):
            return 0
        count = service.clear()
        self.refresh()
        self._hint("删除了 %d 条记录。" % count)
        return count

    def export_csv(self):
        service = self._service()
        if service is None:
            return False
        target = filedialog.asksaveasfilename(
            parent=self.win,
            title="导出工作记录",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
        )
        if not target:
            return False
        ok = service.export_csv(target)
        self._hint("已导出。" if ok else "导出失败，详情见日志。")
        return ok

    @staticmethod
    def _fill(area, text):
        try:
            area.config(state="normal")
            area.delete("1.0", "end")
            area.insert("1.0", text or "")
            area.config(state="disabled")
        except Exception:
            pass

    def _hint(self, text):
        try:
            self.hint.config(text=str(text or ""))
        except Exception:
            pass
