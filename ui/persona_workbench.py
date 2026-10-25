"""人格工作台的几个分页：人格文本、版本、习惯、测试台。

只做界面与读写，执行层在宿主（DesktopPet）与 persona 模块里。
人格文本按原文存，程序不替用户改写；没有模型时测试台如实说跑不了。
"""

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from persona import cards as persona_cards
from persona import fusion as persona_fusion
from persona import habits as persona_habits
from persona import lab as persona_lab
from persona import learner as persona_learner
from persona import policy as persona_policy
from persona import profile as persona_profile
from persona.interface import DefaultPersonaProvider, build_persona_note
from ui import theme

# 测试台可以点的几个反馈。点一下就记一条反馈记忆。
LAB_FEEDBACK = ("更符合", "不符合", "太过了", "再自然一点")


class WorkbenchTabs:
    """给 PersonaWindow 用的分页。宿主提供 pet / memory / win / _hint。"""

    # ---------- 人格文本页 ----------
    def _text_tab(self, frame):
        pad = theme.PAD_MD
        theme.label(frame, "你自己写的人格说明", kind="body").pack(
            anchor="w", padx=pad, pady=(pad, 0)
        )
        theme.label(
            frame,
            "写什么就存什么，程序不替你改写。每轮按上限截断，安全规则不受影响。",
            kind="small",
            fg=theme.MUTED,
            wraplength=780,
            justify="left",
        ).pack(anchor="w", padx=pad, pady=(theme.PAD_XS, theme.PAD_SM))

        self.text_area = theme.text_area(frame, read_only=False, height=10, width=1)
        self.text_area.pack(fill="both", expand=True, padx=pad)

        row = tk.Frame(frame, bg=theme.BG)
        row.pack(fill="x", padx=pad, pady=theme.PAD_SM)
        theme.button(row, "保存并生效", self.save_text, kind="primary").pack(side="left")
        theme.button(row, "只存草稿", self.save_draft).pack(side="left", padx=theme.PAD_SM)
        theme.button(row, "重新载入", self.load_text).pack(side="left")
        self.template_var = tk.StringVar(value="")
        combo = ttk.Combobox(
            row,
            textvariable=self.template_var,
            values=[name for name, _body in persona_profile.templates()],
            state="readonly",
            width=10,
        )
        combo.current(0)
        combo.pack(side="left", padx=(theme.PAD_MD, theme.PAD_SM))
        theme.button(row, "用模板覆盖编辑区", self.use_template).pack(side="left")

        identity = tk.Frame(frame, bg=theme.BG)
        identity.pack(fill="x", padx=pad, pady=(theme.PAD_SM, 0))
        theme.label(identity, "名字", kind="small", fg=theme.MUTED).pack(side="left")
        self.name_var = tk.StringVar(value="")
        theme.entry(identity, textvariable=self.name_var, width=14).pack(
            side="left", padx=theme.PAD_SM
        )
        theme.button(identity, "应用名字", self.save_name).pack(side="left")
        theme.button(identity, "导入角色卡…", self.import_card).pack(
            side="left", padx=(theme.PAD_MD, theme.PAD_SM)
        )
        theme.label(
            identity,
            "支持 .json / .png，先预览后生效",
            kind="small",
            fg=theme.MUTED,
        ).pack(side="left")

        library = tk.Frame(frame, bg=theme.BG)
        library.pack(fill="x", padx=pad, pady=(theme.PAD_SM, 0))
        theme.label(library, "角色卡库", kind="small", fg=theme.MUTED).pack(side="left")
        self.library_var = tk.StringVar(value="")
        self.library_paths = {}
        self.library_combo = ttk.Combobox(
            library,
            textvariable=self.library_var,
            values=[],
            state="readonly",
            width=20,
        )
        self.library_combo.pack(side="left", padx=theme.PAD_SM)
        theme.button(library, "应用这张", self.apply_library_card).pack(side="left")
        theme.button(library, "刷新", self.refresh_library).pack(
            side="left", padx=(theme.PAD_SM, 0)
        )
        theme.button(library, "打开文件夹", self.open_library_folder).pack(
            side="left", padx=(theme.PAD_SM, 0)
        )
        self.library_hint = theme.label(library, "", kind="small", fg=theme.MUTED)
        self.library_hint.pack(side="left", padx=theme.PAD_MD)

        budget_row = tk.Frame(frame, bg=theme.BG)
        budget_row.pack(fill="x", padx=pad)
        theme.label(budget_row, "每轮上限（字）", kind="small", fg=theme.MUTED).pack(side="left")
        self.budget_var = tk.StringVar(value=str(persona_profile.DEFAULT_NOTE_BUDGET))
        tk.Spinbox(
            budget_row,
            from_=persona_profile.MIN_NOTE_BUDGET,
            to=persona_profile.MAX_NOTE_BUDGET,
            increment=100,
            textvariable=self.budget_var,
            width=6,
            bg=theme.FIELD,
            fg=theme.TEXT,
            bd=0,
            highlightthickness=0,
            font=theme.FONT_SMALL,
            buttonbackground=theme.PANEL,
        ).pack(side="left", padx=theme.PAD_SM)
        theme.button(budget_row, "应用上限", self.save_budget).pack(side="left")
        self.text_stats = theme.label(budget_row, "", kind="small", fg=theme.MUTED)
        self.text_stats.pack(side="left", padx=theme.PAD_MD)

        theme.label(frame, "这一轮真正发出去的内容（含安全规则）", kind="body").pack(
            anchor="w", padx=pad, pady=(theme.PAD_SM, 0)
        )
        self.preview_area = theme.text_area(frame, read_only=True, height=4, width=1)
        self.preview_area.pack(fill="x", padx=pad, pady=(theme.PAD_XS, theme.PAD_XS))
        theme.label(
            frame,
            persona_fusion.describe(),
            kind="small",
            fg=theme.MUTED,
            wraplength=800,
            justify="left",
        ).pack(anchor="w", padx=pad)
        theme.label(
            frame,
            "预览按编辑区现在的内容算。没点保存就还没生效。",
            kind="small",
            fg=theme.MUTED,
        ).pack(anchor="w", padx=pad, pady=(0, pad))
    # ---------- 版本页 ----------
    def _version_tab(self, frame):
        pad = theme.PAD_MD
        theme.label(frame, "保存过的版本", kind="body").pack(anchor="w", padx=pad, pady=(pad, 0))
        theme.label(
            frame,
            "每次保存都会留一份原文。回滚是把旧文本重新存一次，任何版本都不会被删。",
            kind="small",
            fg=theme.MUTED,
            wraplength=780,
            justify="left",
        ).pack(anchor="w", padx=pad, pady=(theme.PAD_XS, theme.PAD_SM))

        wrap = tk.Frame(frame, bg=theme.BG)
        wrap.pack(fill="both", expand=True, padx=pad)
        columns = ("v", "t", "chars", "note")
        self.version_tree = ttk.Treeview(wrap, columns=columns, show="headings", height=5)
        for key, title, width, stretch in (
            ("v", "版本", 60, False),
            ("t", "保存时间", 150, False),
            ("chars", "字数", 60, False),
            ("note", "说明", 320, True),
        ):
            self.version_tree.heading(key, text=title)
            self.version_tree.column(key, width=width, anchor="w", stretch=stretch)
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.version_tree.yview)
        self.version_tree.configure(yscrollcommand=scroll.set)
        self.version_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        row = tk.Frame(frame, bg=theme.BG)
        row.pack(fill="x", padx=pad, pady=theme.PAD_SM)
        theme.button(row, "回滚到这个版本", self.revert_version, kind="danger").pack(side="left")
        theme.button(row, "和当前对比", self.compare_version, kind="primary").pack(
            side="left", padx=theme.PAD_SM
        )
        theme.button(row, "刷新", self.refresh_versions).pack(side="left")

        theme.label(frame, "当前生效", kind="small", fg=theme.MUTED).pack(anchor="w", padx=pad)
        self.compare_current = theme.text_area(frame, read_only=True, height=4, width=1)
        self.compare_current.pack(fill="both", expand=True, padx=pad)
        theme.label(frame, "选中的版本", kind="small", fg=theme.MUTED).pack(anchor="w", padx=pad)
        self.compare_other = theme.text_area(frame, read_only=True, height=4, width=1)
        self.compare_other.pack(fill="both", expand=True, padx=pad, pady=(0, pad))

    # ---------- 习惯页 ----------
    def _habit_tab(self, frame):
        pad = theme.PAD_MD
        theme.label(frame, "你明确设置的偏好", kind="body").pack(anchor="w", padx=pad, pady=(pad, 0))
        theme.label(
            frame,
            "这里填的永远优先于系统推断。留空就是不设置。改不了安全规则，也不会给自己开权限。",
            kind="small",
            fg=theme.MUTED,
            wraplength=780,
            justify="left",
        ).pack(anchor="w", padx=pad, pady=(theme.PAD_XS, theme.PAD_SM))

        grid = tk.Frame(frame, bg=theme.BG)
        grid.pack(fill="x", padx=pad)
        self.habit_vars = {}
        for index, name in enumerate(persona_habits.FIELDS):
            theme.label(
                grid,
                persona_habits.LABELS[name],
                kind="small",
                fg=theme.MUTED,
                width=12,
                anchor="w",
            ).grid(row=index, column=0, sticky="w", pady=theme.PAD_XS)
            var = tk.StringVar(value="")
            self.habit_vars[name] = var
            choices = persona_habits.CHOICES[name]
            if choices:
                widget = ttk.Combobox(
                    grid, textvariable=var, values=list(choices), state="readonly", width=14
                )
            else:
                widget = theme.entry(grid, textvariable=var, width=32)
            widget.grid(row=index, column=1, sticky="w", padx=theme.PAD_SM, pady=theme.PAD_XS)
            theme.label(grid, persona_habits.HINTS[name], kind="small", fg=theme.MUTED).grid(
                row=index, column=2, sticky="w", padx=theme.PAD_SM
            )

        row = tk.Frame(frame, bg=theme.BG)
        row.pack(fill="x", padx=pad, pady=theme.PAD_SM)
        theme.button(row, "保存习惯", self.save_habits, kind="primary").pack(side="left")
        theme.button(row, "清空", self.reset_habits).pack(side="left", padx=theme.PAD_SM)
        theme.button(row, "重新载入", self.load_habits).pack(side="left")
        self.learning_var = tk.IntVar(value=1)
        tk.Checkbutton(
            row,
            text="从对话里学习习惯",
            variable=self.learning_var,
            command=self.toggle_learning,
            bg=theme.BG,
            fg=theme.TEXT,
            selectcolor=theme.FIELD,
            activebackground=theme.BG,
            activeforeground=theme.TEXT,
            bd=0,
            highlightthickness=0,
            font=theme.FONT_SMALL,
        ).pack(side="left", padx=theme.PAD_MD)

        self.habit_note = theme.label(
            frame, "", kind="small", fg=theme.MUTED, wraplength=780, justify="left"
        )
        self.habit_note.pack(anchor="w", padx=pad, pady=(theme.PAD_SM, 0))
        theme.label(
            frame,
            "自动学习已经接入：它只从你明确说过的话里挑候选，称呼和雷区等你点确认；"
            "关掉后已采纳的观察偏好会立刻收回。",
            kind="small",
            fg=theme.MUTED,
            wraplength=780,
            justify="left",
        ).pack(anchor="w", padx=pad, pady=(theme.PAD_XS, 0))

    # ---------- 情境页 ----------
    def _context_tab(self, frame):
        pad = theme.PAD_MD
        theme.label(frame, "现在按哪种情境和你相处", kind="body").pack(
            anchor="w", padx=pad, pady=(pad, 0)
        )
        theme.label(
            frame,
            "这是本地规则，不调用模型。工作、休息和勿扰只拦住普通主动搭话；"
            "你自己设的日程和高优先级提醒仍按真实规则处理。",
            kind="small",
            fg=theme.MUTED,
            wraplength=780,
            justify="left",
        ).pack(anchor="w", padx=pad, pady=(theme.PAD_XS, theme.PAD_SM))

        self.context_var = tk.StringVar(value=persona_policy.DEFAULT_MODE)
        choices = tk.Frame(frame, bg=theme.BG)
        choices.pack(fill="x", padx=pad)
        for mode in persona_policy.MODES:
            box = theme.card(choices)
            box.pack(side="left", fill="both", expand=True, padx=(0, theme.PAD_SM))
            tk.Radiobutton(
                box,
                text=persona_policy.LABELS[mode],
                value=mode,
                variable=self.context_var,
                command=self.save_context_mode,
                bg=theme.CARD,
                fg=theme.TEXT,
                selectcolor=theme.FIELD,
                activebackground=theme.CARD,
                activeforeground=theme.TEXT,
                bd=0,
                highlightthickness=0,
                font=theme.FONT_BODY,
                anchor="w",
            ).pack(anchor="w", padx=theme.PAD_SM, pady=(theme.PAD_SM, 0))
            theme.label(
                box,
                persona_policy.DESCRIPTIONS[mode],
                kind="small",
                fg=theme.MUTED,
                wraplength=150,
                justify="left",
            ).pack(anchor="w", padx=theme.PAD_SM, pady=(0, theme.PAD_SM))

        row = tk.Frame(frame, bg=theme.BG)
        row.pack(fill="x", padx=pad, pady=theme.PAD_SM)
        theme.button(row, "重新载入", self.load_context).pack(side="left")
        theme.label(
            row,
            "也可以直接说：进入工作模式 / 休息模式 / 勿扰模式 / 回到陪伴。",
            kind="small",
            fg=theme.MUTED,
        ).pack(side="left", padx=theme.PAD_MD)

        self.context_summary = theme.text_area(frame, read_only=True, height=5, width=1)
        self.context_summary.pack(fill="both", expand=True, padx=pad, pady=(0, pad))
    # ---------- 测试台页 ----------
    def _lab_tab(self, frame):
        pad = theme.PAD_MD
        theme.label(frame, "拿几句话试试这套人格", kind="body").pack(
            anchor="w", padx=pad, pady=(pad, 0)
        )
        theme.label(
            frame,
            "只在这里看一眼回答：不写聊天记录、不写记忆、不改人格。没配模型就如实说跑不了。",
            kind="small",
            fg=theme.MUTED,
            wraplength=780,
            justify="left",
        ).pack(anchor="w", padx=pad, pady=(theme.PAD_XS, theme.PAD_SM))

        self.lab_scenarios = {}
        names = []
        for item in persona_lab.scenarios():
            key = self._lab_key(item)
            self.lab_scenarios[key] = item
            names.append(key)

        row = tk.Frame(frame, bg=theme.BG)
        row.pack(fill="x", padx=pad)
        self.lab_var = tk.StringVar(value="")
        self.lab_combo = ttk.Combobox(
            row, textvariable=self.lab_var, values=names, state="readonly", width=44
        )
        if names:
            self.lab_combo.current(0)
        self.lab_combo.pack(side="left")
        theme.button(row, "跑一次", self.run_lab, kind="primary").pack(
            side="left", padx=theme.PAD_SM
        )

        row2 = tk.Frame(frame, bg=theme.BG)
        row2.pack(fill="x", padx=pad, pady=(theme.PAD_XS, theme.PAD_SM))
        theme.label(row2, "对比版本", kind="small", fg=theme.MUTED).pack(side="left")
        self.lab_version_var = tk.StringVar(value="不对比")
        self.lab_version_combo = ttk.Combobox(
            row2,
            textvariable=self.lab_version_var,
            values=["不对比"],
            state="readonly",
            width=16,
        )
        self.lab_version_combo.pack(side="left", padx=theme.PAD_SM)
        theme.button(row2, "两套各回一次", self.run_lab_compare).pack(side="left")

        panes = tk.Frame(frame, bg=theme.BG)
        panes.pack(fill="both", expand=True, padx=pad)
        for side, title, gap in (
            ("left", "当前生效", theme.PAD_SM),
            ("right", "对比结果", 0),
        ):
            col = tk.Frame(panes, bg=theme.BG)
            col.pack(side="left", fill="both", expand=True, padx=(0, gap))
            theme.label(col, title, kind="small", fg=theme.MUTED).pack(anchor="w")
            area = theme.text_area(col, read_only=True, height=9, width=1)
            area.pack(fill="both", expand=True)
            setattr(self, "lab_%s" % side, area)

        frow = tk.Frame(frame, bg=theme.BG)
        frow.pack(fill="x", padx=pad, pady=theme.PAD_SM)
        theme.label(frow, "对这次回答的反馈", kind="small", fg=theme.MUTED).pack(side="left")
        for label in LAB_FEEDBACK:
            theme.button(frow, label, lambda name=label: self.lab_feedback(name)).pack(
                side="left", padx=(theme.PAD_SM, 0)
            )
        self.lab_status = theme.label(
            frame, "", kind="small", fg=theme.MUTED, wraplength=780, justify="left"
        )
        self.lab_status.pack(anchor="w", padx=pad, pady=(0, pad))

    def _lab_key(self, item):
        return "%s：%s" % (item["name"], item["input"])

    def _lab_scenario(self):
        return self.lab_scenarios.get(self.lab_var.get())
    # ---------- 人格文本：读写 ----------
    def reload_text_tab(self):
        """把人格文本、版本、习惯与预览一起读一遍。"""
        self.load_text()
        self.refresh_name()
        self.refresh_library()
        self.refresh_versions()
        self.load_habits()
        self.load_context()
        self.refresh_candidates()
        self.refresh_preview()
        return True

    def _editor_text(self):
        try:
            return self.text_area.get("1.0", "end-1c")
        except Exception:
            return ""

    def _set_editor(self, body):
        try:
            self.text_area.delete("1.0", "end")
            self.text_area.insert("1.0", body or "")
        except Exception:
            pass
        return body or ""

    def _fill(self, area, text):
        """往只读文本区里写字：临时打开，写完马上关回去。"""
        try:
            area.config(state="normal")
            area.delete("1.0", "end")
            area.insert("1.0", text or "")
            area.config(state="disabled")
        except Exception:
            pass
        return text

    def load_text(self):
        """把草稿（没写过草稿就是生效文本）读进编辑区。"""
        body = ""
        try:
            body = self.pet.persona_draft()
        except Exception as exc:
            self._hint("读人格文本失败：%s" % exc)
        self._set_editor(body)
        self.refresh_text_status()
        return body

    def refresh_text_status(self):
        info = {}
        try:
            info = self.pet.persona_profile_state() or {}
        except Exception:
            info = {}
        parts = [
            "现在 %s 字" % info.get("chars", 0),
            "版本 %s" % info.get("version", 0),
            "保留 %s 个版本" % info.get("versions", 0),
            "每轮上限 %s 字" % info.get("budget", 0),
            "草稿没保存" if info.get("dirty") else "已生效",
        ]
        sent = info.get("sent")
        if isinstance(sent, int) and sent > 0:
            parts.append("这轮发 %s 字" % sent)
        try:
            self.text_stats.config(text="　".join(parts))
        except Exception:
            pass
        try:
            self.budget_var.set(str(info.get("budget", persona_profile.DEFAULT_NOTE_BUDGET)))
        except Exception:
            pass
        return info

    def save_text(self):
        """保存并生效：原文照存，顺手留一个版本。"""
        result = self.pet.save_persona_text(self._editor_text(), note="面板保存")
        if not (result or {}).get("ok"):
            self._hint(str((result or {}).get("error") or "保存失败"))
            return False
        self.refresh_text_status()
        self.refresh_versions()
        self.refresh_preview()
        if result.get("changed"):
            self._hint("已生效，下一句话就按新的来。")
        else:
            self._hint("和现在一样，没重复存版本。")
        return True

    def save_draft(self):
        """只存草稿，不生效。"""
        result = self.pet.save_persona_draft(self._editor_text())
        if not (result or {}).get("ok"):
            self._hint(str((result or {}).get("error") or "草稿没存下来"))
            return False
        self.refresh_text_status()
        self._hint("草稿已存，还没生效。")
        return True

    def use_template(self):
        """把模板放进编辑区：只改草稿，不直接生效。"""
        body = dict(persona_profile.templates()).get(self.template_var.get())
        if not body:
            self._hint("先选一个模板。")
            return False
        if self._editor_text().strip() and not messagebox.askyesno(
            "用模板",
            "会用模板替换编辑区现在的内容，替换前的内容不保留。继续吗？",
            parent=self.win,
        ):
            self._hint("已取消。")
            return False
        self._set_editor(body)
        self.refresh_text_status()
        self.refresh_preview()
        self._hint("模板放进编辑区了，点「保存并生效」才生效。")
        return True

    def refresh_name(self):
        """名字以存下来的为准；没设过就是配置里的默认名字。"""
        try:
            value = self.pet.persona_name()
        except Exception:
            value = ""
        self.name_var.set(value or "")
        return value

    def save_name(self):
        """只改名字。人格文本不动，也不生成人格版本。"""
        try:
            result = self.pet.set_persona_name(self.name_var.get())
        except Exception as exc:
            self._hint("改名失败：%s" % exc)
            return False
        if not (result or {}).get("ok"):
            self._hint(str((result or {}).get("error") or "名字没能写进去"))
            return False
        self.refresh_name()
        self.refresh_preview()
        self._hint("名字改成「%s」，下一句话生效。" % result.get("name"))
        return True

    def import_card(self):
        """选一张卡先看预览；读不出来就明说，不猜。"""
        path = filedialog.askopenfilename(
            parent=self.win,
            title="选一张酒馆角色卡",
            filetypes=[("角色卡（json / png）", "*.json *.png"), ("所有文件", "*.*")],
        )
        if not path:
            return False
        preview = persona_cards.read_file(path)
        if not preview.get("ok"):
            self._hint("这张卡读不了：%s" % preview.get("error"))
            return False
        CardPreviewWindow(self, preview)
        return True

    def apply_card(self, preview):
        """用户点过确认之后才写：名字和人格文本一起换，留一条版本。"""
        try:
            result = self.pet.import_persona_card(preview)
        except Exception as exc:
            self._hint("导入失败：%s" % exc)
            return False
        if not (result or {}).get("ok"):
            self._hint(str((result or {}).get("error") or "导入失败"))
            return False
        # 导入过的卡自动留一份进卡库，以后想切回来不用再找原文件。
        stored = {}
        try:
            stored = self.pet.save_persona_card(
                preview, source=str(preview.get("source") or "")
            )
        except Exception as exc:
            stored = {"ok": False, "error": str(exc)}
        self.load_text()
        self.refresh_name()
        self.refresh_library()
        self.refresh_versions()
        self.refresh_text_status()
        self.refresh_preview()
        if (stored or {}).get("ok"):
            self._hint("已导入「%s」，并存进角色卡库。" % result.get("name"))
        else:
            self._hint(
                "已导入「%s」，但没存进卡库：%s"
                % (result.get("name"), (stored or {}).get("error") or "原因不明")
            )
        return True

    def refresh_library(self):
        """列出卡库里的卡。同名用字数区分，路径单独记着，显示名只用来选。"""
        try:
            rows = self.pet.persona_card_library()
        except Exception:
            rows = []
        self.library_paths = {}
        values = []
        for row in rows:
            label = "%s（%d 字）" % (row.get("name"), row.get("chars", 0))
            while label in self.library_paths:
                label += " "
            self.library_paths[label] = row.get("path")
            values.append(label)
        self.library_combo.configure(values=values)
        if values:
            if self.library_var.get() not in values:
                self.library_combo.current(0)
            self.library_hint.configure(text="共 %d 张，导入过的卡会自动留一份" % len(values))
        else:
            self.library_var.set("")
            self.library_hint.configure(text="还没有存过卡，导入时会自动留一份")
        return values

    def apply_library_card(self):
        """从卡库里挑一张，先看预览再应用。"""
        path = self.library_paths.get(self.library_var.get())
        if not path:
            self._hint("先在卡库里选一张。")
            return False
        card = self.pet.load_persona_card(path)
        if not (card or {}).get("ok"):
            self._hint(str((card or {}).get("error") or "这张卡读不了"))
            return False
        CardPreviewWindow(self, card)
        return True

    def open_library_folder(self):
        """打开卡库目录：想手动删卡、改卡就到这里。"""
        result = self.pet.open_persona_card_folder()
        if not (result or {}).get("ok"):
            self._hint(str((result or {}).get("error") or "打不开文件夹"))
            return False
        return True

    def save_budget(self):
        """改每轮发给模型的人格文本上限。"""
        value = self.pet.set_persona_budget(self.budget_var.get())
        self.refresh_text_status()
        self.refresh_preview()
        self._hint("每轮上限改成 %s 字。" % value)
        return value

    def refresh_preview(self):
        try:
            note = self.preview_text()
        except Exception as exc:
            note = "预览失败：%s" % exc
        self._fill(self.preview_area, note)
        return note

    def preview_text(self):
        """按编辑区现在的内容，算一遍这轮会发出去的人格提示。"""
        persona = getattr(self.pet.agent, "persona", None)
        name = ""
        style = ""
        traits = {}
        feedback = None
        if persona is not None:
            try:
                profile = persona.get_profile() or {}
                name = profile.get("name") or ""
                style = profile.get("style") or ""
                traits = profile.get("traits") or {}
            except Exception:
                pass
            feedback = getattr(persona, "feedback_buckets", None)
        draft = DefaultPersonaProvider(name=name, style=style, traits=traits, feedback=feedback)
        draft.set_note(self._editor_text())
        try:
            draft.set_habits(persona_habits.load(self.memory))
            draft.note_budget = persona_profile.budget(self.memory)
        except Exception:
            pass
        return build_persona_note(draft)
    # ---------- 版本 ----------
    def refresh_versions(self):
        for item in self.version_tree.get_children():
            self.version_tree.delete(item)
        rows = []
        try:
            rows = self.pet.persona_versions() or []
        except Exception as exc:
            self._hint("读版本失败：%s" % exc)
        for row in rows:
            self.version_tree.insert(
                "",
                "end",
                iid=str(row.get("v")),
                values=(row.get("v"), row.get("t"), row.get("chars"), row.get("note") or ""),
            )
        try:
            self.lab_version_combo.config(
                values=["不对比"] + ["v%s" % row.get("v") for row in rows]
            )
        except Exception:
            pass
        return rows

    def _selected_version(self):
        selection = self.version_tree.selection()
        if not selection:
            self._hint("先在列表里选一个版本。")
            return None
        return selection[0]

    def compare_version(self):
        """把当前生效的文本和选中的版本并排放着看。"""
        number = self._selected_version()
        if number is None:
            return False
        other = self.pet.persona_version_text(number)
        self._fill(self.compare_current, self.pet.persona_text())
        self._fill(self.compare_other, other)
        self._hint("上面是现在生效的，下面是选的版本。")
        return True

    def revert_version(self):
        """把选中版本的原文写回生效位置。需要确认。"""
        number = self._selected_version()
        if number is None:
            return False
        if not messagebox.askyesno(
            "回滚版本",
            "把这个版本的原文写回生效位置吗？现在的文本会作为一个新版本留着，不会丢。",
            parent=self.win,
        ):
            self._hint("已取消。")
            return False
        result = self.pet.revert_persona_version(number)
        if not (result or {}).get("ok"):
            self._hint(str((result or {}).get("error") or "回滚失败"))
            return False
        self.load_text()
        self.refresh_versions()
        self.refresh_preview()
        self._hint("已回滚到版本 %s。" % number)
        return True

    # ---------- 习惯 ----------
    def load_habits(self):
        values = {}
        try:
            values = self.pet.persona_habits() or {}
        except Exception:
            values = {}
        for name, var in self.habit_vars.items():
            try:
                var.set(str(values.get(name) or ""))
            except Exception:
                pass
        try:
            self.learning_var.set(1 if self.pet.persona_habit_learning() else 0)
        except Exception:
            pass
        try:
            self.habit_note.config(text=persona_habits.summary(values))
        except Exception:
            pass
        return values

    def save_habits(self):
        """保存用户明确设置的偏好。这些优先于任何推断。"""
        values = {name: var.get() for name, var in self.habit_vars.items()}
        result = self.pet.save_persona_habits(values)
        if not (result or {}).get("ok"):
            self._hint(str((result or {}).get("error") or "保存失败"))
            return False
        self.load_habits()
        self.refresh_preview()
        self._hint("习惯已保存，下一句话就按它来。")
        return True

    def reset_habits(self):
        if not messagebox.askyesno(
            "清空习惯",
            "清空这里的设置吗？聊天记录和记忆都不动。",
            parent=self.win,
        ):
            self._hint("已取消。")
            return False
        result = self.pet.reset_persona_habits()
        if not (result or {}).get("ok"):
            self._hint(str((result or {}).get("error") or "清空失败"))
            return False
        self.load_habits()
        self.refresh_candidates()
        self.refresh_preview()
        self._hint("已清空。")
        return True

    def toggle_learning(self):
        """学习开关。关掉后不再记录，观察到的内容也立刻从提示词里收回。"""
        flag = bool(self.learning_var.get())
        ok = self.pet.set_persona_habit_learning(flag)
        self._hint("学习开关已开。" if flag else "学习开关已关。")
        if not ok:
            self._hint("开关没存下来。")
        return ok

    # ---------- 情境 ----------
    def load_context(self):
        try:
            mode = self.pet.persona_context_mode()
        except Exception:
            mode = persona_policy.DEFAULT_MODE
        mode = persona_policy.normalize_mode(mode)
        self.context_var.set(mode)
        self.refresh_context_summary()
        return mode

    def save_context_mode(self):
        mode = self.context_var.get()
        try:
            result = self.pet.set_persona_context_mode(mode)
        except Exception as exc:
            self._hint("情境没切换：%s" % exc)
            return False
        if not (result or {}).get("ok"):
            self._hint(str((result or {}).get("error") or "情境没切换"))
            return False
        self.refresh_context_summary()
        self._hint("情境切到%s，下一轮主动行为按它判断。" % result.get("label"))
        return True

    def refresh_context_summary(self):
        try:
            info = self.pet.persona_context_summary() or {}
        except Exception as exc:
            body = "读不到情境状态：%s" % exc
            self._fill(self.context_summary, body)
            return {}
        lines = [
            "当前：%s。%s" % (info.get("label") or "陪伴", info.get("description") or ""),
            "主动冷却倍率：%s。数字越大越安静，越小越主动。" % info.get("cooldown_factor", 1.0),
        ]
        habits = info.get("habits") or {}
        active = [
            "%s：%s" % (persona_habits.LABELS.get(name, name), value)
            for name, value in habits.items()
            if value
        ]
        lines.append("生效偏好：" + ("；".join(active) if active else "还没有设置"))
        body = "\n".join(lines)
        self._fill(self.context_summary, body)
        return info

    # ---------- 测试台 ----------
    def run_lab(self):
        """跑一次预览。只调模型，不写聊天记录，不写记忆。"""
        item = self._lab_scenario()
        if item is None:
            self._hint("先选一个场景。")
            return False
        self.lab_status.config(text="跑着…")
        self._fill(self.lab_left, "")
        self._fill(self.lab_right, "")
        try:
            self.win.update_idletasks()
        except Exception:
            pass
        result = self.pet.persona_test_run(item["id"])
        return self._show_lab(result, self.lab_left, item)

    def run_lab_compare(self):
        """同一句话，当前人格和选中的历史版本各回一次。"""
        item = self._lab_scenario()
        if item is None:
            self._hint("先选一个场景。")
            return False
        picked = self.lab_version_var.get()
        if picked in ("", "不对比"):
            self._hint("先在下面选一个对比版本。")
            return False
        other = self.pet.persona_version_text(picked.lstrip("v"))
        if not other:
            self._hint("读不到这个版本的原文。")
            return False
        self.lab_status.config(text="跑着…")
        self._fill(self.lab_left, "")
        self._fill(self.lab_right, "")
        try:
            self.win.update_idletasks()
        except Exception:
            pass
        result = self.pet.persona_test_compare(item["id"], "", other)
        if not isinstance(result, dict):
            self.lab_status.config(text="没跑出结果。")
            return False
        left = result.get("left") or {}
        right = result.get("right") or {}
        self._show_lab(left, self.lab_left, item, keep_status=True)
        self._show_lab(right, self.lab_right, item, keep_status=True)
        self.lab_status.config(
            text="左：%s；右：%s"
            % (
                "跑了" if left.get("ok") else str(left.get("error") or "失败"),
                "跑了" if right.get("ok") else str(right.get("error") or "失败"),
            )
        )
        return True

    def _show_lab(self, result, area, item, keep_status=False):
        result = result if isinstance(result, dict) else {}
        if not result.get("ok"):
            error = str(result.get("error") or "跑不了")
            self._fill(area, "")
            self.lab_status.config(text=error)
            self._hint(error)
            return False
        self._fill(area, str(result.get("content") or ""))
        if not keep_status:
            self.lab_status.config(
                text="这个场景看的是：%s。模型：%s，用了 %s+%s tokens。"
                % (
                    item.get("focus") or "整体口气",
                    result.get("model") or "未注明",
                    result.get("prompt_tokens", 0),
                    result.get("completion_tokens", 0),
                )
            )
        return True

    def lab_feedback(self, label):
        """把这次看到的问题记一条反馈。写入失败就直说。"""
        if self.memory is None:
            self._hint("记忆不可用，反馈没地方存。")
            return False
        item = self._lab_scenario()
        content = "%s：回答%s" % (item["name"] if item else "人格测试", label)
        try:
            self.memory.remember(content, kind="feedback", importance=2)
        except Exception as exc:
            self._hint("记不下这条反馈：%s" % exc)
            return False
        self.refresh()
        self.refresh_preview()
        self._hint("记下了。表达类的反馈会进人格提示，其余只留档。")
        return True

    # ---------- 学习页：从对话里学到的偏好 ----------
    def _learn_tab(self, frame):
        pad = theme.PAD_MD
        theme.label(frame, "从对话里学到的偏好", kind="body").pack(
            anchor="w", padx=pad, pady=(pad, 0)
        )
        rule = persona_learner.rules()
        self.learn_rule = theme.label(
            frame,
            "同一件事说够 %s 次、说法一致、置信度到 %s，低影响的字段才自动采纳；"
            "称呼和不喜欢的行为一律等你点。点「接受」才真的进提示词。"
            % (rule["min_seen"], rule["confidence"]),
            kind="small",
            fg=theme.MUTED,
            wraplength=800,
            justify="left",
        )
        self.learn_rule.pack(anchor="w", padx=pad, pady=(theme.PAD_XS, theme.PAD_SM))

        table = tk.Frame(frame, bg=theme.BG)
        table.pack(fill="both", expand=True, padx=pad)
        columns = ("field", "value", "status", "scope", "seen", "confidence")
        self.learn_tree = ttk.Treeview(table, columns=columns, show="headings", height=6)
        for key, title, width, stretch in (
            ("field", "字段", 80, False),
            ("value", "建议", 180, True),
            ("status", "状态", 100, False),
            ("scope", "影响范围", 210, False),
            ("seen", "提到", 50, False),
            ("confidence", "置信度", 60, False),
        ):
            self.learn_tree.heading(key, text=title)
            self.learn_tree.column(key, width=width, anchor="w", stretch=stretch)
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.learn_tree.yview)
        self.learn_tree.configure(yscrollcommand=scroll.set)
        self.learn_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.learn_tree.bind("<<TreeviewSelect>>", lambda event: self.show_evidence())

        row = tk.Frame(frame, bg=theme.BG)
        row.pack(fill="x", padx=pad, pady=theme.PAD_SM)
        theme.button(row, "接受", self.accept_candidate, kind="primary").pack(side="left")
        theme.button(row, "拒绝", self.reject_candidate).pack(side="left", padx=theme.PAD_SM)
        theme.button(row, "改内容", self.edit_candidate).pack(side="left")
        theme.button(row, "稍后", self.snooze_candidate).pack(side="left", padx=theme.PAD_SM)
        theme.button(row, "撤销", self.undo_candidate).pack(side="left")
        theme.button(row, "删除", self.remove_candidate, kind="danger").pack(
            side="left", padx=theme.PAD_SM
        )
        theme.button(row, "清空记录", self.clear_candidates, kind="danger").pack(side="left")
        theme.button(row, "刷新", self.refresh_candidates).pack(side="left", padx=theme.PAD_SM)

        theme.label(frame, "这条是怎么来的", kind="small", fg=theme.MUTED).pack(
            anchor="w", padx=pad
        )
        self.learn_evidence = theme.text_area(frame, read_only=True, height=4, width=1)
        self.learn_evidence.pack(fill="x", padx=pad)
        self.learn_status = theme.label(
            frame, "", kind="small", fg=theme.MUTED, wraplength=800, justify="left"
        )
        self.learn_status.pack(anchor="w", padx=pad, pady=(theme.PAD_XS, pad))

    def refresh_candidates(self):
        """重新读学习记录。读不到就显示空表，不编内容。"""
        for item in self.learn_tree.get_children():
            self.learn_tree.delete(item)
        rows = []
        try:
            rows = self.pet.persona_habit_candidates() or []
        except Exception as exc:
            self._hint("读学习记录失败：%s" % exc)
        labels = persona_learner.rules()["labels"]
        for row in rows:
            self.learn_tree.insert(
                "",
                "end",
                iid=str(row.get("id")),
                values=(
                    labels.get(row.get("field"), row.get("field")),
                    row.get("value"),
                    persona_learner.status_label(row),
                    self.pet.persona_habit_scope(row.get("id")),
                    row.get("seen"),
                    row.get("confidence"),
                ),
            )
        if rows:
            self.learn_tree.selection_set(str(rows[0].get("id")))
            self.show_evidence()
        else:
            self._fill(self.learn_evidence, "")
        self.learn_status.config(text=self._learning_line())
        return rows

    def _learning_line(self):
        try:
            info = self.pet.persona_habit_summary() or {}
        except Exception:
            info = {}
        return "学习开关：%s。候选 %s 条，已采纳 %s 条，已拒绝 %s 条，现在有 %s 条在生效。" % (
            "开" if info.get("learning") else "关",
            info.get("candidate", 0),
            info.get("accepted", 0),
            info.get("rejected", 0),
            info.get("applied", 0),
        )

    def _selected_candidate(self):
        selection = self.learn_tree.selection()
        if not selection:
            self._hint("先在表里选一条记录。")
            return None
        return selection[0]

    def show_evidence(self):
        """把这条建议是从哪句话来的摆出来。"""
        selection = self.learn_tree.selection()
        if not selection:
            self._fill(self.learn_evidence, "")
            return ""
        identifier = selection[0]
        try:
            items = self.pet.persona_habit_evidence(identifier) or []
        except Exception as exc:
            body = "读不到：%s" % exc
            self._fill(self.learn_evidence, body)
            return body
        lines = [
            "%s  你当时说：%s" % (item.get("t") or "", item.get("text") or "")
            for item in items
        ]
        body = "\n".join(lines) if lines else "还没有依据。"
        self._fill(self.learn_evidence, body)
        return body

    def accept_candidate(self):
        return self._candidate_action("accept")

    def reject_candidate(self):
        return self._candidate_action("reject")

    def remove_candidate(self):
        return self._candidate_action("remove")

    def snooze_candidate(self):
        """稍后再说：这条先收起来，过一阵再问一次。"""
        return self._candidate_action("snooze")

    def undo_candidate(self):
        """撤销：已采纳的退回候选，不再影响说话方式。"""
        return self._candidate_action("undo")

    def _candidate_action(self, action):
        identifier = self._selected_candidate()
        if identifier is None:
            return False
        if action == "remove" and not messagebox.askyesno(
            "删除记录",
            "删掉这条学习记录吗？删了就不再影响说话方式，也不会自己回来。",
            parent=self.win,
        ):
            self._hint("已取消。")
            return False
        try:
            done = getattr(self.pet, "%s_persona_habit" % action)(identifier)
        except Exception as exc:
            self._hint("这一步没成：%s" % exc)
            return False
        if not done:
            self._hint("这条可能已经不在了。")
            return False
        self.refresh_candidates()
        self.load_habits()
        self.refresh_preview()
        self._hint(
            {
                "accept": "已采纳，下一句话就按它来。",
                "reject": "已拒绝，这条不再影响说话方式。",
                "snooze": "先记下了，过一阵再问你一次。",
                "undo": "已撤销，这条退回候选，等你重新拿主意。",
                "remove": "已删除。",
            }[action]
        )
        return True

    def edit_candidate(self):
        identifier = self._selected_candidate()
        if identifier is None:
            return False
        current = ""
        try:
            rows = self.pet.persona_habit_candidates() or []
            for row in rows:
                if str(row.get("id")) == identifier:
                    current = str(row.get("value") or "")
                    break
        except Exception:
            current = ""
        value = simpledialog.askstring("改内容", "改成什么？", initialvalue=current, parent=self.win)
        if not value or not value.strip():
            self._hint("已取消。")
            return False
        if not self.pet.edit_persona_habit(identifier, value):
            self._hint("没改动。")
            return False
        self.refresh_candidates()
        self.refresh_preview()
        self._hint("改好了。现在还是候选，点「接受」才生效。")
        return True

    def clear_candidates(self):
        if not messagebox.askyesno(
            "清空记录",
            "清掉全部学习记录吗？你自己填的习惯和聊天记录都不动。",
            parent=self.win,
        ):
            self._hint("已取消。")
            return False
        try:
            self.pet.clear_persona_habit_records()
        except Exception as exc:
            self._hint("没清掉：%s" % exc)
            return False
        self.refresh_candidates()
        self.refresh_preview()
        self._hint("已清空。")
        return True


class CardPreviewWindow:
    """导入前的确认窗：写进去的是什么、哪些字段没导入，一眼看完再决定。"""

    GEOMETRY = (760, 600)

    def __init__(self, workbench, preview):
        self.workbench = workbench
        self.preview = preview
        self.win = tk.Toplevel(workbench.win)
        self.win.title("导入角色卡 · %s" % preview.get("name", ""))
        self.win.configure(bg=theme.BG)
        width, height = self.GEOMETRY
        try:
            x = workbench.win.winfo_rootx() + 40
            y = workbench.win.winfo_rooty() + 40
        except Exception:
            x, y = 120, 120
        self.win.geometry("%dx%d+%d+%d" % (width, height, x, y))
        self._build()

    def summary(self):
        card = self.preview
        lines = [
            "名字：%s" % (card.get("name") or ""),
            "来源：%s" % (card.get("source") or "（未知）"),
            "作者：%s    规格：%s    字数：%s / %s"
            % (card.get("creator") or "未写", card.get("spec") or "", card.get("chars", 0),
               persona_cards.MAX_TEXT_CHARS),
        ]
        if card.get("tags"):
            lines.append("标签：%s" % "、".join(card["tags"]))
        if card.get("ignored"):
            lines.append("")
            lines.append("这些字段没有导入：")
            lines.extend("  · %s" % item for item in card["ignored"])
        if card.get("warnings"):
            lines.append("")
            lines.append("提示：")
            lines.extend("  · %s" % item for item in card["warnings"])
        if card.get("greeting"):
            lines.append("")
            lines.append("卡里的开场白（只给你看，不导入）：")
            lines.append("  " + card["greeting"].replace("\n", "\n  "))
        return "\n".join(lines)

    def _build(self):
        pad = theme.PAD_MD
        theme.label(self.win, "导入角色卡 · %s" % self.preview.get("name", ""), kind="title").pack(
            anchor="w", padx=pad, pady=(pad, theme.PAD_XS)
        )
        summary = theme.text_area(self.win, read_only=True, height=8, width=1)
        summary.pack(fill="x", padx=pad)
        self._fill(summary, self.summary())

        theme.label(self.win, "确认后会写进人格的文本", kind="body").pack(
            anchor="w", padx=pad, pady=(theme.PAD_SM, 0)
        )
        body = theme.text_area(self.win, read_only=True, height=12, width=1)
        body.pack(fill="both", expand=True, padx=pad)
        self._fill(body, self.preview.get("text") or "")

        foot = tk.Frame(self.win, bg=theme.BG)
        foot.pack(fill="x", padx=pad, pady=pad)
        theme.button(foot, "导入并生效", self.confirm, kind="primary").pack(side="right")
        theme.button(foot, "取消", self.win.destroy).pack(side="right", padx=theme.PAD_SM)
        theme.label(
            foot,
            "名字会一起改；原来的文本留在版本历史里，随时能回滚。",
            kind="small",
            fg=theme.MUTED,
        ).pack(side="left")

    @staticmethod
    def _fill(area, text):
        try:
            area.config(state="normal")
            area.delete("1.0", "end")
            area.insert("1.0", text or "")
            area.config(state="disabled")
        except Exception:
            pass

    def confirm(self):
        if self.workbench.apply_card(self.preview):
            self.win.destroy()
            return True
        return False
