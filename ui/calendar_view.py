"""日历日程页：月视图 + 当日详情 + 增删日程。

数据全部来自本地 SQLite 的 calendar_events（CalendarService），不联网、
不编造日程。日期格子里只显示真实存在的条目。
"""

import calendar
import tkinter as tk
from datetime import date, datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from ui import panel, theme

WEEK_LABELS = ("一", "二", "三", "四", "五", "六", "日")
MAX_CELL_EVENTS = 2
MAX_WEEK_EVENTS = 6
VIEW_LABELS = {"month": "月", "week": "周", "day": "日"}


class CalendarWindow:
    """月视图。左侧日期网格，右侧当日日程与新增表单。"""

    TITLE = "日历日程"
    GEOMETRY = (1000, 660)

    def __init__(self, pet, parent=None):
        self.pet = pet
        self.embedded = parent is not None
        self.win = panel.container(pet, self.TITLE, self.GEOMETRY, parent)
        today = date.today()
        self.year = today.year
        self.month = today.month
        self.selected = today
        self.view_mode = "month"
        self.events = []
        self.cells = {}
        self._mode_buttons = {}
        self._editing_id = 0
        self._build()
        self.refresh()

    # ---------- 数据 ----------
    def service(self):
        capabilities = getattr(self.pet.agent, "capabilities", None)
        return getattr(capabilities, "calendar", None)

    def month_range(self):
        first = date(self.year, self.month, 1)
        if self.month == 12:
            after = date(self.year + 1, 1, 1)
        else:
            after = date(self.year, self.month + 1, 1)
        return (
            first.strftime("%Y-%m-%d 00:00"),
            after.strftime("%Y-%m-%d 00:00"),
        )

    def week_range(self):
        first = self.selected - timedelta(days=self.selected.weekday())
        after = first + timedelta(days=7)
        return first.strftime("%Y-%m-%d 00:00"), after.strftime("%Y-%m-%d 00:00")

    def day_range(self):
        first = self.selected
        after = first + timedelta(days=1)
        return first.strftime("%Y-%m-%d 00:00"), after.strftime("%Y-%m-%d 00:00")

    def visible_range(self):
        if self.view_mode == "week":
            return self.week_range()
        if self.view_mode == "day":
            return self.day_range()
        return self.month_range()

    def range_text(self):
        if self.view_mode == "week":
            first = self.selected - timedelta(days=self.selected.weekday())
            last = first + timedelta(days=6)
            return "%d 年 %d 月 %d 日 - %d 月 %d 日" % (
                first.year,
                first.month,
                first.day,
                last.month,
                last.day,
            )
        if self.view_mode == "day":
            return "%d 年 %d 月 %d 日（周%s）" % (
                self.selected.year,
                self.selected.month,
                self.selected.day,
                "一二三四五六日"[self.selected.weekday()],
            )
        return "%d 年 %d 月" % (self.year, self.month)

    def load_events(self):
        service = self.service()
        if service is None:
            self.events = []
            self._hint("日历服务没有接上，先看不了日程。")
            return []
        start, end = self.visible_range()
        try:
            payload = service.events_between(start, end)
            self.events = list((payload.get("data") or {}).get("events") or [])
        except Exception as exc:
            self.events = []
            self._hint("读日程失败：%s" % exc)
        return self.events

    def events_of(self, day):
        prefix = day.strftime("%Y-%m-%d")
        return [row for row in self.events if str(row.get("when_at") or "").startswith(prefix)]

    # ---------- 搭建 ----------
    def _build(self):
        head = tk.Frame(self.win, bg=theme.BG)
        head.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_MD, theme.PAD_SM))
        theme.label(head, "日历日程", kind="title").pack(side="left")
        self.month_label = theme.label(head, "", kind="body", fg=theme.TEXT)
        self.month_label.pack(side="left", padx=theme.PAD_MD)
        theme.button(head, "当天空闲", self.show_free_slots).pack(side="right", padx=(0, theme.PAD_SM))
        theme.button(head, "今天", self.go_today).pack(side="right")
        theme.button(head, "›", lambda: self.shift(1)).pack(side="right", padx=(0, theme.PAD_XS))
        theme.button(head, "‹", lambda: self.shift(-1)).pack(side="right", padx=(0, theme.PAD_XS))

        toolbar = tk.Frame(self.win, bg=theme.BG)
        toolbar.pack(fill="x", padx=theme.PAD_MD, pady=(0, theme.PAD_SM))
        for mode in ("month", "week", "day"):
            button = theme.button(
                toolbar,
                VIEW_LABELS[mode],
                lambda target=mode: self.set_view(target),
                kind="chip",
            )
            button.pack(side="left", padx=(0, theme.PAD_XS))
            self._mode_buttons[mode] = button
        theme.button(toolbar, "导入 ICS", self.import_ics_dialog).pack(side="right")
        theme.button(toolbar, "导出 ICS", self.export_ics_dialog).pack(
            side="right", padx=(0, theme.PAD_XS)
        )

        body = tk.Frame(self.win, bg=theme.BG)
        body.pack(fill="both", expand=True, padx=theme.PAD_MD, pady=(0, theme.PAD_MD))

        grid_card = theme.card(body)
        grid_card.pack(side="left", fill="both", expand=True)
        self.weekday = tk.Frame(grid_card, bg=theme.CARD)
        self.weekday.pack(fill="x", padx=theme.PAD_SM, pady=(theme.PAD_SM, theme.PAD_XS))
        for index, name in enumerate(WEEK_LABELS):
            cell = theme.label(
                self.weekday, name, kind="small", fg=theme.MUTED, bg=theme.CARD, anchor="center"
            )
            cell.grid(row=0, column=index, sticky="nsew")
            self.weekday.columnconfigure(index, weight=1, uniform="weekday")

        self.view_host = tk.Frame(grid_card, bg=theme.CARD)
        self.view_host.pack(fill="both", expand=True, padx=theme.PAD_SM, pady=(0, theme.PAD_SM))
        self.grid = tk.Frame(self.view_host, bg=theme.CARD)
        self.week_view = tk.Frame(self.view_host, bg=theme.CARD)
        self.day_view = tk.Frame(self.view_host, bg=theme.CARD)
        for column in range(7):
            self.grid.columnconfigure(column, weight=1, uniform="day")
        for row_index in range(6):
            self.grid.rowconfigure(row_index, weight=1, uniform="dayrow")
        for row_index in range(6):
            for column in range(7):
                cell = tk.Frame(
                    self.grid,
                    bg=theme.PANEL_2,
                    highlightthickness=1,
                    highlightbackground=theme.BORDER,
                    cursor="hand2",
                )
                cell.grid(
                    row=row_index,
                    column=column,
                    sticky="nsew",
                    padx=2,
                    pady=2,
                )
                number = theme.label(
                    cell, "", kind="small", bg=theme.PANEL_2, fg=theme.TEXT, anchor="nw"
                )
                number.pack(fill="x", padx=theme.PAD_XS, pady=(theme.PAD_XS, 0))
                marks = theme.label(
                    cell, "", kind="small", bg=theme.PANEL_2, fg=theme.MUTED, anchor="nw",
                    justify="left",
                )
                marks.pack(fill="both", expand=True, padx=theme.PAD_XS, pady=(0, 2))
                self.cells[(row_index, column)] = (cell, number, marks)

        side = tk.Frame(body, bg=theme.BG, width=300)
        side.pack(side="left", fill="y", padx=(theme.PAD_MD, 0))
        side.pack_propagate(False)
        detail = theme.card(side)
        detail.pack(fill="both", expand=True)
        self.detail_title = theme.label(detail, "", kind="body", bg=theme.CARD)
        self.detail_title.pack(anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_MD, theme.PAD_XS))
        self.detail_list = tk.Frame(detail, bg=theme.CARD)
        self.detail_list.pack(fill="both", expand=True, padx=theme.PAD_SM)

        form = tk.Frame(detail, bg=theme.CARD)
        form.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_SM, theme.PAD_MD))
        self.form_title = theme.label(
            form, "新增日程", kind="small", fg=theme.MUTED, bg=theme.CARD
        )
        self.form_title.pack(anchor="w")
        self.title_var = tk.StringVar(value="")
        theme.entry(form, textvariable=self.title_var).pack(fill="x", pady=(2, 0))
        theme.label(form, "时间（2026-10-21 09:00）", kind="small", fg=theme.MUTED, bg=theme.CARD).pack(
            anchor="w", pady=(theme.PAD_XS, 0)
        )
        self.when_var = tk.StringVar(value="")
        theme.entry(form, textvariable=self.when_var).pack(fill="x", pady=(2, 0))
        theme.label(
            form, "结束（留空按 30 分钟）", kind="small", fg=theme.MUTED, bg=theme.CARD
        ).pack(anchor="w", pady=(theme.PAD_XS, 0))
        self.end_var = tk.StringVar(value="")
        theme.entry(form, textvariable=self.end_var).pack(fill="x", pady=(2, 0))
        options = tk.Frame(form, bg=theme.CARD)
        options.pack(fill="x", pady=(theme.PAD_XS, 0))
        theme.label(options, "优先级", kind="small", fg=theme.MUTED, bg=theme.CARD).pack(
            side="left"
        )
        self.priority_var = tk.StringVar(value="1")
        tk.Spinbox(
            options,
            from_=0,
            to=9,
            width=3,
            textvariable=self.priority_var,
            bg=theme.FIELD,
            fg=theme.TEXT,
            buttonbackground=theme.PANEL,
            bd=0,
            highlightthickness=0,
            font=theme.FONT_SMALL,
        ).pack(side="left", padx=(theme.PAD_XS, theme.PAD_MD))
        theme.label(options, "提前提醒", kind="small", fg=theme.MUTED, bg=theme.CARD).pack(
            side="left"
        )
        self.remind_var = tk.StringVar(value="10")
        tk.Spinbox(
            options,
            from_=0,
            to=1440,
            width=5,
            textvariable=self.remind_var,
            bg=theme.FIELD,
            fg=theme.TEXT,
            buttonbackground=theme.PANEL,
            bd=0,
            highlightthickness=0,
            font=theme.FONT_SMALL,
        ).pack(side="left", padx=theme.PAD_XS)
        theme.label(options, "分钟", kind="small", fg=theme.MUTED, bg=theme.CARD).pack(
            side="left"
        )
        repeat_row = tk.Frame(form, bg=theme.CARD)
        repeat_row.pack(fill="x", pady=(theme.PAD_XS, 0))
        theme.label(repeat_row, "重复", kind="small", fg=theme.MUTED, bg=theme.CARD).pack(
            side="left"
        )
        self.repeat_box = ttk.Combobox(
            repeat_row,
            state="readonly",
            width=10,
            values=("不重复", "每天", "每周", "每月"),
        )
        self.repeat_box.current(0)
        self.repeat_box.pack(side="left", padx=theme.PAD_XS)
        self.note_var = tk.StringVar(value="")
        theme.label(form, "备注（可选）", kind="small", fg=theme.MUTED, bg=theme.CARD).pack(
            anchor="w", pady=(theme.PAD_XS, 0)
        )
        theme.entry(form, textvariable=self.note_var).pack(fill="x", pady=(2, 0))
        self.save_button = theme.button(form, "添加到日历", self.add_event, kind="primary")
        self.save_button.pack(fill="x", pady=(theme.PAD_SM, 0))
        self.cancel_edit_button = theme.button(form, "取消编辑", self.cancel_edit)
        self.cancel_edit_button.pack(fill="x", pady=(theme.PAD_XS, 0))
        self.cancel_edit_button.pack_forget()
        self.hint = theme.label(detail, "", kind="small", fg=theme.MUTED, bg=theme.CARD, wraplength=260)
        self.hint.pack(fill="x", padx=theme.PAD_MD, pady=(0, theme.PAD_MD))
        panel.close_button(self.win, self).pack(pady=theme.PAD_SM)

    # ---------- 交互 ----------
    def set_view(self, mode):
        if mode not in VIEW_LABELS:
            return False
        self.view_mode = mode
        self.refresh()
        return True

    def shift(self, delta):
        if self.view_mode == "month":
            return self.shift_month(delta)
        step = 7 if self.view_mode == "week" else 1
        self.selected = self.selected + timedelta(days=step * int(delta))
        self.year, self.month = self.selected.year, self.selected.month
        self.refresh()
        return self.selected

    def shift_month(self, delta):
        month = self.month + int(delta)
        year = self.year
        while month < 1:
            month += 12
            year -= 1
        while month > 12:
            month -= 12
            year += 1
        self.year, self.month = year, month
        self.selected = date(year, month, 1)
        self.refresh()
        return (year, month)

    def go_today(self):
        today = date.today()
        self.year, self.month = today.year, today.month
        self.selected = today
        self.refresh()
        return today

    def select_day(self, day):
        self.selected = day
        self.year, self.month = day.year, day.month
        self.refresh()
        return day

    def add_event(self):
        service = self.service()
        if service is None:
            self._hint("日历服务没有接上，加不了日程。")
            return False
        repeat_values = {"不重复": "", "每天": "daily", "每周": "weekly", "每月": "monthly"}
        repeat = repeat_values.get(self.repeat_box.get(), "")
        try:
            priority = int(self.priority_var.get() or 0)
        except ValueError:
            priority = 1
        try:
            remind = int(self.remind_var.get() or 0)
        except ValueError:
            remind = 10
        fields = {
            "title": self.title_var.get(),
            "when": self.when_var.get(),
            "end": self.end_var.get(),
            "note": self.note_var.get(),
            "priority": priority,
            "repeat_rule": repeat,
            "remind_minutes": remind,
        }
        if self._editing_id:
            result = service.update_event(self._editing_id, **fields)
        else:
            result = service.add_event(**fields)
        if not result.get("ok"):
            self._hint(str(result.get("error") or "没加上"))
            return False
        event_id = int((result.get("data") or {}).get("id") or 0)
        conflicts = list((result.get("data") or {}).get("conflicts") or [])
        self._hint(
            ("已保存。有 %d 个时间冲突。" % len(conflicts))
            if conflicts
            else ("已保存修改。" if self._editing_id else "已添加。")
        )
        self._schedule_reminder(event_id, replace=True)
        self._editing_id = 0
        self.form_title.configure(text="新增日程")
        self.save_button.configure(text="添加到日历")
        self.cancel_edit_button.pack_forget()
        for variable, value in (
            (self.title_var, ""),
            (self.when_var, ""),
            (self.end_var, ""),
            (self.note_var, ""),
            (self.priority_var, "1"),
            (self.remind_var, "10"),
        ):
            variable.set(value)
        self.repeat_box.current(0)
        when = str((result.get("data") or {}).get("when") or "")
        if when[:7] == "%04d-%02d" % (self.year, self.month):
            try:
                self.selected = datetime.strptime(when, "%Y-%m-%d %H:%M").date()
            except ValueError:
                pass
        self.refresh()
        return True

    def _schedule_reminder(self, event_id, replace=False):
        if not event_id:
            return False
        reminder = getattr(getattr(self.pet, "agent", None), "calendar_reminder", None)
        if reminder is None:
            return False
        try:
            if replace:
                scheduler = getattr(self.pet, "scheduler", None)
                if scheduler is not None:
                    scheduler.cancel_key_prefix("calendar:%s:" % event_id)
                reminder.forget_event(event_id)
            return bool(reminder.schedule_event(event_id))
        except Exception:
            return False

    def edit_event(self, event_id):
        service = self.service()
        if service is None:
            return False
        event = service.event(event_id)
        if event is None:
            self._hint("这条日程已经不在了。")
            return False
        self._editing_id = int(event_id)
        self.form_title.configure(text="编辑日程")
        self.save_button.configure(text="保存修改")
        self.cancel_edit_button.pack(fill="x", pady=(theme.PAD_XS, 0))
        self.title_var.set(event.get("title") or "")
        self.when_var.set(event.get("when_at") or "")
        self.end_var.set(event.get("end_at") or "")
        self.note_var.set(event.get("note") or "")
        self.priority_var.set(str(event.get("priority") or 0))
        self.remind_var.set(str(event.get("remind_minutes") or 0))
        labels = {"": 0, "daily": 1, "weekly": 2, "monthly": 3}
        self.repeat_box.current(labels.get(event.get("repeat_rule") or "", 0))
        self._hint("正在编辑「%s」。保存后再刷新。" % event.get("title"))
        return True

    def cancel_edit(self):
        self._editing_id = 0
        self.form_title.configure(text="新增日程")
        self.save_button.configure(text="添加到日历")
        self.cancel_edit_button.pack_forget()
        self._hint("已取消编辑，没有改日程。")
        return True

    def complete_event(self, event_id):
        service = self.service()
        if service is None:
            return False
        result = service.complete_event(event_id)
        self._cancel_reminders(event_id)
        self._hint("日程已完成。" if result.get("ok") else str(result.get("error") or "没完成"))
        self.refresh()
        return bool(result.get("ok"))

    def show_free_slots(self):
        service = self.service()
        if service is None:
            return []
        result = service.free_slots(self.selected, min_minutes=30)
        if not result.get("ok"):
            self._hint(str(result.get("error") or "算不出空闲时间"))
            return []
        slots = list((result.get("data") or {}).get("slots") or [])
        if not slots:
            self._hint("%d 月 %d 日在 09:00-18:00 没有 30 分钟以上的空闲。" % (self.selected.month, self.selected.day))
        else:
            text = "，".join("%s-%s" % (row["start"][11:16], row["end"][11:16]) for row in slots[:6])
            self._hint("当天空闲：%s。可按预计耗时新建日程。" % text)
        return slots

    def export_ics_dialog(self):
        path = filedialog.asksaveasfilename(
            title="导出日历",
            defaultextension=".ics",
            filetypes=(("iCalendar", "*.ics"), ("所有文件", "*.*")),
            initialfile="blossom-calendar.ics",
        )
        if not path:
            self._hint("已取消导出。")
            return 0
        return self.export_ics(path)

    def export_ics(self, path):
        service = self.service()
        if service is None:
            self._hint("日历服务没有接上，导不了日程。")
            return 0
        start, end = self.visible_range()
        result = service.export_ics(start, end)
        if not result.get("ok"):
            self._hint(str(result.get("error") or "导出失败"))
            return 0
        text = str((result.get("data") or {}).get("text") or "")
        try:
            with Path(path).open("w", encoding="utf-8", newline="") as handle:
                handle.write(text)
        except Exception as exc:
            self._hint("写文件失败：%s" % exc)
            return 0
        count = int((result.get("data") or {}).get("count") or 0)
        self._hint("已导出 %d 条日程。" % count)
        return count

    def import_ics_dialog(self):
        path = filedialog.askopenfilename(
            title="导入日历",
            filetypes=(("iCalendar", "*.ics"), ("所有文件", "*.*")),
        )
        if not path:
            self._hint("已取消导入。")
            return 0
        return self.import_ics(path)

    def import_ics(self, path, confirm=None):
        service = self.service()
        if service is None:
            self._hint("日历服务没有接上，导不了日程。")
            return 0
        try:
            text = Path(path).read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = Path(path).read_text(encoding="gb18030")
            except Exception as exc:
                self._hint("读文件失败：%s" % exc)
                return 0
        except Exception as exc:
            self._hint("读文件失败：%s" % exc)
            return 0
        preview = service.import_ics(text, dry_run=True)
        if not preview.get("ok"):
            self._hint(str(preview.get("error") or "没有可导入的日程"))
            return 0
        data = preview.get("data") or {}
        count = int(data.get("would_create") or 0)
        skipped = int(data.get("skipped") or 0)
        warnings = list(data.get("warnings") or [])
        if count <= 0:
            self._hint(
                "没有新日程可导入。"
                + (" 已跳过 %d 条重复日程。" % skipped if skipped else "")
            )
            return 0
        message = "准备导入 %d 条日程。" % count
        if skipped:
            message += " 已跳过 %d 条重复日程。" % skipped
        if warnings:
            message += " 有 %d 条提示，导入后会显示。" % len(warnings)
        if confirm is None:
            confirm = lambda text: messagebox.askyesno("导入 ICS", text)
        if not confirm(message):
            self._hint("已取消导入。")
            return 0
        result = service.import_ics(text, dry_run=False)
        if not result.get("ok"):
            self._hint(str(result.get("error") or "导入失败"))
            return 0
        created = int((result.get("data") or {}).get("created") or 0)
        problems = list((result.get("data") or {}).get("warnings") or [])
        self._hint(
            "已导入 %d 条日程。" % created
            + (" %s" % problems[0] if problems else "")
        )
        self.refresh()
        return created

    def delete_event(self, event_id):
        service = self.service()
        if service is None:
            return False
        result = service.cancel_event(event_id)
        if not result.get("ok"):
            self._hint(str(result.get("error") or "删不掉"))
            return False
        self._cancel_reminders(event_id)
        self._hint("已删除。")
        self.refresh()
        return True

    def _cancel_reminders(self, event_id):
        scheduler = getattr(self.pet, "scheduler", None)
        if scheduler is not None:
            scheduler.cancel_key_prefix("calendar:%s:" % event_id)
        reminder = getattr(getattr(self.pet, "agent", None), "calendar_reminder", None)
        if reminder is not None:
            reminder.forget_event(event_id)
        return True

    # ---------- 渲染 ----------
    def refresh(self):
        self.month_label.configure(text=self.range_text())
        self.load_events()
        self._render_view()
        self.refresh_detail()
        return len(self.events)

    def _render_view(self):
        for frame in (self.weekday, self.grid, self.week_view, self.day_view):
            frame.pack_forget()
        if self.view_mode == "week":
            self._paint_week()
            self.week_view.pack(fill="both", expand=True)
        elif self.view_mode == "day":
            self._paint_day()
            self.day_view.pack(fill="both", expand=True)
        else:
            self.weekday.pack(
                fill="x",
                padx=theme.PAD_SM,
                pady=(theme.PAD_SM, theme.PAD_XS),
                before=self.view_host,
            )
            self.grid.pack(fill="both", expand=True)
            self._paint_grid()
        return True

    def _clear(self, frame):
        for child in list(frame.winfo_children()):
            try:
                child.destroy()
            except Exception:
                continue

    def _paint_week(self):
        self._clear(self.week_view)
        first = self.selected - timedelta(days=self.selected.weekday())
        today = date.today()
        for column in range(7):
            self.week_view.columnconfigure(column, weight=1, uniform="weekcol")
        for index in range(7):
            day = first + timedelta(days=index)
            selected = day == self.selected
            base = theme.ACCENT if selected else (
                theme.PANEL if day == today else theme.PANEL_2
            )
            fg = theme.ON_ACCENT if selected else theme.TEXT
            column = tk.Frame(
                self.week_view,
                bg=base,
                highlightthickness=1,
                highlightbackground=theme.ACCENT if selected else theme.BORDER,
            )
            column.grid(row=0, column=index, sticky="nsew", padx=2, pady=2)
            header = theme.label(
                column,
                "%d/%d\n周%s" % (day.month, day.day, WEEK_LABELS[index]),
                kind="small",
                bg=base,
                fg=fg,
                justify="center",
                cursor="hand2",
            )
            header.pack(fill="x", padx=theme.PAD_XS, pady=(theme.PAD_XS, 2))
            rows = self.events_of(day)
            if not rows:
                empty = theme.label(
                    column,
                    "无日程",
                    kind="small",
                    bg=base,
                    fg=theme.ON_ACCENT if selected else theme.MUTED,
                )
                empty.pack(anchor="w", padx=theme.PAD_XS, pady=theme.PAD_XS)
                targets = (column, header, empty)
            else:
                targets = [column, header]
                for item in rows[:MAX_WEEK_EVENTS]:
                    done = str(item.get("state") or "open") == "done"
                    line = "%s %s" % (
                        str(item.get("when_at") or "")[11:16],
                        str(item.get("title") or ""),
                    )
                    card = theme.label(
                        column,
                        ("✓ " if done else "") + line,
                        kind="small",
                        bg=base,
                        fg=theme.ON_ACCENT if selected else (
                            theme.MUTED if done else theme.TEXT
                        ),
                        justify="left",
                        anchor="w",
                        wraplength=92,
                    )
                    card.pack(fill="x", padx=theme.PAD_XS, pady=1)
                    targets.append(card)
                if len(rows) > MAX_WEEK_EVENTS:
                    more = theme.label(
                        column,
                        "+%d 条" % (len(rows) - MAX_WEEK_EVENTS),
                        kind="small",
                        bg=base,
                        fg=theme.ON_ACCENT if selected else theme.MUTED,
                    )
                    more.pack(anchor="w", padx=theme.PAD_XS, pady=(0, theme.PAD_XS))
                    targets.append(more)
            for widget in targets:
                widget.bind(
                    "<Button-1>",
                    lambda event, target=day: self.select_day(target),
                )
        return True

    def _paint_day(self):
        self._clear(self.day_view)
        canvas = tk.Canvas(
            self.day_view, bg=theme.CARD, highlightthickness=0, bd=0
        )
        scrollbar = tk.Scrollbar(
            self.day_view,
            orient="vertical",
            command=canvas.yview,
            bg=theme.PANEL_2,
            troughcolor=theme.CARD,
            activebackground=theme.ACCENT,
            bd=0,
            highlightthickness=0,
        )
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        inner = tk.Frame(canvas, bg=theme.CARD)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind(
            "<Configure>",
            lambda event: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(window, width=event.width),
        )
        canvas.bind(
            "<MouseWheel>",
            lambda event: canvas.yview_scroll(int(-event.delta / 120), "units"),
        )
        rows = self.events_of(self.selected)
        heading = theme.label(
            inner,
            "当日 %d 条日程" % len(rows),
            kind="body",
            bg=theme.CARD,
            fg=theme.TEXT,
        )
        heading.pack(anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_SM, 2))
        for hour in range(24):
            slot = tk.Frame(inner, bg=theme.CARD)
            slot.pack(fill="x", padx=theme.PAD_MD, pady=1)
            theme.label(
                slot,
                "%02d:00" % hour,
                kind="small",
                fg=theme.MUTED,
                bg=theme.CARD,
                width=5,
                anchor="nw",
            ).pack(side="left")
            items = [
                item for item in rows
                if getattr(item.get("start_dt"), "hour", -1) == hour
            ]
            if not items:
                theme.label(
                    slot, "—", kind="small", fg=theme.MUTED, bg=theme.CARD
                ).pack(side="left", padx=theme.PAD_XS)
                continue
            body = tk.Frame(slot, bg=theme.CARD)
            body.pack(side="left", fill="x", expand=True)
            for item in items:
                done = str(item.get("state") or "open") == "done"
                when_text = "%s-%s" % (
                    str(item.get("when_at") or "")[11:16],
                    str(item.get("end_at") or "")[11:16],
                )
                theme.label(
                    body,
                    ("✓ " if done else "") + when_text + "  " + str(item.get("title") or ""),
                    kind="small",
                    fg=theme.MUTED if done else (
                        theme.ACCENT if hour == datetime.now().hour else theme.TEXT
                    ),
                    bg=theme.CARD,
                    wraplength=650,
                    justify="left",
                    anchor="w",
                ).pack(fill="x")
        return True

    def _paint_grid(self):
        first = date(self.year, self.month, 1)
        # 周一为一周起点：Python weekday() 周一=0，正好对齐。
        offset = first.weekday()
        days_in_month = calendar.monthrange(self.year, self.month)[1]
        today = date.today()
        for row in range(6):
            for column in range(7):
                index = row * 7 + column - offset
                cell, number, marks = self.cells[(row, column)]
                if index < 0 or index >= days_in_month:
                    number.configure(text="")
                    marks.configure(text="")
                    cell.configure(bg=theme.BG, highlightbackground=theme.BG)
                    number.configure(bg=theme.BG)
                    marks.configure(bg=theme.BG)
                    cell.unbind("<Button-1>")
                    for child in (number, marks):
                        child.unbind("<Button-1>")
                    continue
                day = date(self.year, self.month, index + 1)
                selected = day == self.selected
                is_today = day == today
                base = theme.ACCENT if selected else (theme.PANEL if is_today else theme.PANEL_2)
                fg = theme.ON_ACCENT if selected else theme.TEXT
                cell.configure(bg=base, highlightbackground=theme.ACCENT if selected else theme.BORDER)
                number.configure(text="%d" % day.day, bg=base, fg=fg)
                rows = self.events_of(day)
                preview = "\n".join(
                    "%s %s" % (
                        str(item.get("when_at") or "")[11:16],
                        str(item.get("title") or "")[:10],
                    )
                    for item in rows[:MAX_CELL_EVENTS]
                )
                if len(rows) > MAX_CELL_EVENTS:
                    preview += "\n+%d" % (len(rows) - MAX_CELL_EVENTS)
                marks.configure(
                    text=preview,
                    bg=base,
                    fg=theme.ON_ACCENT if selected else theme.MUTED,
                )
                for widget in (cell, number, marks):
                    widget.bind(
                        "<Button-1>",
                        lambda event, target=day: self.select_day(target),
                    )
        return True

    def refresh_detail(self):
        day = self.selected
        self.detail_title.configure(text="%d 月 %d 日（%s）" % (day.month, day.day, "一二三四五六日"[day.weekday()]))
        for child in list(self.detail_list.winfo_children()):
            try:
                child.destroy()
            except Exception:
                continue
        rows = self.events_of(day)
        if not rows:
            theme.label(
                self.detail_list, "这一天还没有日程。", kind="small", fg=theme.MUTED, bg=theme.CARD
            ).pack(anchor="w", padx=theme.PAD_XS, pady=theme.PAD_XS)
            return 0
        for item in rows:
            done = str(item.get("state") or "open") == "done"
            row = tk.Frame(self.detail_list, bg=theme.PANEL_2, highlightthickness=1,
                           highlightbackground=theme.BORDER)
            row.pack(fill="x", pady=2)
            when_text = str(item.get("when_at") or "")[11:16]
            end_text = str(item.get("end_at") or "")[11:16]
            if end_text:
                when_text += "-" + end_text
            theme.label(
                row,
                when_text,
                kind="small",
                fg=theme.OK if done else theme.ACCENT,
                bg=theme.PANEL_2,
            ).pack(side="left", padx=theme.PAD_XS, pady=theme.PAD_XS)
            body = tk.Frame(row, bg=theme.PANEL_2)
            body.pack(side="left", fill="x", expand=True)
            theme.label(
                body,
                ("✓ " if done else "") + str(item.get("title") or ""),
                kind="small",
                bg=theme.PANEL_2,
                fg=theme.MUTED if done else theme.TEXT,
            ).pack(anchor="w")
            meta = "优先级 %s" % item.get("priority", 1)
            repeat = str(item.get("repeat_label") or "不重复")
            if repeat != "不重复":
                meta += " · " + repeat
            if int(item.get("remind_minutes") or 0):
                meta += " · 提前 %d 分钟提醒" % int(item.get("remind_minutes") or 0)
            theme.label(body, meta, kind="small", fg=theme.MUTED, bg=theme.PANEL_2).pack(
                anchor="w"
            )
            note = str(item.get("note") or "").strip()
            if note:
                theme.label(
                    body, note, kind="small", bg=theme.PANEL_2, fg=theme.MUTED, wraplength=150,
                    justify="left",
                ).pack(anchor="w")
            if not done:
                theme.button(
                    row,
                    "成",
                    lambda event_id=item.get("id"): self.complete_event(event_id),
                    kind="ghost",
                    font=theme.FONT_SMALL,
                ).pack(side="right", padx=(0, theme.PAD_XS))
            theme.button(
                row,
                "改",
                lambda event_id=item.get("id"): self.edit_event(event_id),
                kind="ghost",
                font=theme.FONT_SMALL,
            ).pack(side="right", padx=(0, theme.PAD_XS))
            theme.button(
                row, "删", lambda event_id=item.get("id"): self.delete_event(event_id), kind="danger"
            ).pack(side="right", padx=theme.PAD_XS)
        return len(rows)

    def _hint(self, text):
        try:
            self.hint.configure(text=str(text or ""))
        except Exception:
            pass
        return text

    # 兼容测试/外部调用的小接口
    def month_text(self):
        return "%d 年 %d 月" % (self.year, self.month)

    def selected_events(self):
        return self.events_of(self.selected)
