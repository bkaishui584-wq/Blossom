"""技能管理界面：看清单、看权限、看运行记录、导入与启停。

只读 Skill 元数据和运行统计，不在这里执行任何 Skill。
导入只搬说明与文本参考，脚本一律跳过等用户单独授权。
"""

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from ui import panel, theme


class SkillsWindow:
    """技能管理窗口。所有动作都通过宿主的 agent.skills 完成。"""
    TITLE = "技能"


    GEOMETRY = (820, 560)

    def __init__(self, pet, parent=None):

        self.pet = pet
        self.manager = getattr(getattr(pet, "agent", None), "skills", None)
        # 嵌进主工作台是一块 Frame，单独打开才是一个窗口。
        self.embedded = parent is not None
        self.win = panel.container(pet, self.TITLE, self.GEOMETRY, parent)
        self._rows = []
        self._build()
        self.refresh()

    # ---------- 搭建 ----------
    def _build(self):
        header = theme.card(self.win)
        header.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_MD, theme.PAD_SM))
        head = tk.Frame(header, bg=theme.CARD)
        head.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_SM, theme.PAD_XS))
        theme.label(head, "技能", kind="title").pack(side="left")
        self.stats_label = theme.label(head, "", kind="small", fg=theme.MUTED)
        self.stats_label.pack(side="left", padx=theme.PAD_MD)
        theme.button(head, "导入技能…", self.import_skill, kind="primary").pack(side="right")
        theme.button(head, "导入技能包…", self.import_package).pack(side="right", padx=theme.PAD_SM)
        theme.button(head, "从市场链接导入…", self.import_from_market).pack(side="right")
        theme.button(head, "重新扫描", self.reload_skills).pack(
            side="right", padx=theme.PAD_SM
        )

        bar = tk.Frame(header, bg=theme.CARD)
        bar.pack(fill="x", padx=theme.PAD_MD, pady=(0, theme.PAD_SM))
        theme.label(bar, "说明：技能描述做什么，工具才真正执行；每个工具仍要单独过权限。",
                    kind="small", fg=theme.MUTED).pack(anchor="w")

        body = tk.Frame(self.win, bg=theme.BG)
        body.pack(fill="both", expand=True, padx=theme.PAD_MD, pady=theme.PAD_SM)

        left = theme.card(body)
        left.pack(side="left", fill="both", expand=True, padx=(0, theme.PAD_SM))
        columns = ("name", "version", "permission", "tools", "steps", "enabled")
        self.tree = ttk.Treeview(left, columns=columns, show="headings", height=12)
        for key, title, width in (
            ("name", "名称", 150),
            ("version", "版本", 70),
            ("permission", "权限", 80),
            ("tools", "工具数", 60),
            ("steps", "步骤", 50),
            ("enabled", "状态", 70),
        ):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="w", stretch=(key == "name"))
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda event: self.show_detail())

        right = theme.card(body)
        right.pack(side="left", fill="both", padx=(0, 0))
        theme.label(right, "详情", kind="small", fg=theme.MUTED).pack(anchor="w")
        self.detail = theme.text_area(right, height=12, width=36)
        self.detail.pack(fill="both", expand=True, pady=(theme.PAD_XS, theme.PAD_SM))
        theme.label(right, "最近运行", kind="small", fg=theme.MUTED).pack(anchor="w")
        self.history = theme.text_area(right, height=8, width=36)
        self.history.pack(fill="both", expand=True)

        actions_card = theme.card(self.win)
        actions_card.pack(fill="x", padx=theme.PAD_MD, pady=(0, theme.PAD_SM))
        actions = tk.Frame(actions_card, bg=theme.CARD)
        actions.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_SM, theme.PAD_XS))
        theme.button(actions, "启用 / 停用", self.toggle_selected, kind="primary").pack(side="left")
        theme.button(actions, "设为常用", self.favorite_selected).pack(
            side="left", padx=theme.PAD_SM
        )
        theme.button(actions, "取消常用", lambda: self.favorite_selected(False)).pack(side="left")
        self.auto_var = tk.BooleanVar(value=self._auto_match())
        tk.Checkbutton(
            actions, text="自动匹配", variable=self.auto_var, command=self._save_auto,
            bg=theme.BG, fg=theme.TEXT, selectcolor=theme.FIELD,
            activebackground=theme.BG, activeforeground=theme.ACCENT,
            font=theme.FONT_SMALL,
        ).pack(side="left", padx=theme.PAD_MD)

        self.hint = theme.label(actions_card, "", kind="small", fg=theme.MUTED, anchor="w")
        self.hint.pack(fill="x", padx=theme.PAD_MD)
        panel.close_button(actions_card, self).pack(pady=theme.PAD_XS)
        theme.cardify(header)
        theme.cardify(left)
        theme.cardify(right)
        theme.cardify(actions_card)

    # ---------- 数据 ----------
    def refresh(self):
        self._rows = self.pet.skill_rows()
        self.tree.delete(*self.tree.get_children())
        for index, row in enumerate(self._rows):
            self.tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    row.get("name"),
                    row.get("version"),
                    row.get("permission"),
                    row.get("tools"),
                    row.get("steps"),
                    "启用" if row.get("enabled") else "停用",
                ),
            )
        self.stats_label.config(text=self._stats_text())
        self.refresh_history()
        return len(self._rows)

    def _stats_text(self):
        stats = {}
        if self.manager is not None:
            try:
                stats = self.manager.stats() or {}
            except Exception:
                stats = {}
        runs = sum(int(bucket.get("runs", 0)) for bucket in stats.values())
        ok = sum(int(bucket.get("ok", 0)) for bucket in stats.values())
        rate = ("%.0f%%" % (100.0 * ok / runs)) if runs else "还没跑过"
        return "共 %d 个，运行 %d 次，成功率 %s" % (len(self._rows), runs, rate)

    def refresh_history(self):
        self.history.config(state="normal")
        self.history.delete("1.0", "end")
        rows = []
        if self.manager is not None:
            try:
                rows = self.manager.runs(limit=20) or []
            except Exception as exc:
                self.history.insert("end", "读不到运行记录：%s" % exc)
                self.history.config(state="disabled")
                return
        if not rows:
            self.history.insert("end", "还没有运行记录。")
        for row in rows:
            mark = "成功" if row.get("ok") else "失败"
            self.history.insert(
                "end",
                "%s %s %.2fs %s\n" % (
                    mark, row.get("skill"), float(row.get("seconds") or 0),
                    (row.get("error") or "")[:40],
                ),
            )
        self.history.config(state="disabled")

    def selected_name(self):
        selection = self.tree.selection()
        if not selection:
            return ""
        try:
            return str(self._rows[int(selection[0])].get("name"))
        except (IndexError, ValueError):
            return ""

    def show_detail(self):
        name = self.selected_name()
        if not name or self.manager is None:
            return None
        meta = self.manager.get(name)
        report = self.manager.report_of(name)
        lines = []
        if meta is not None:
            lines.append("名称：%s" % meta.name)
            lines.append("版本：%s" % meta.version)
            lines.append("说明：%s" % meta.description)
            lines.append("权限：%s" % meta.permission)
            lines.append("步骤：")
            lines.extend("  %d. %s%s" % (
                index, step.title, ("（%s）" % step.tool) if step.tool else ""
            ) for index, step in enumerate(meta.steps, start=1))
            lines.append("允许工具：%s" % ("、".join(meta.tools) or "无"))
            lines.append("输出：%s" % (meta.output or "未声明"))
            lines.append("失败处理：%s" % (meta.failure or "未声明"))
            if meta.verify:
                lines.append("验收标准：")
                lines.extend("  - %s" % item for item in meta.verify)
        if report is not None and not report.ok:
            lines.append("")
            lines.append("不可加载的原因：%s" % (report.blocked or "；".join(report.errors)))
        self.detail.config(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", "\n".join(lines) or "没有可显示的内容。")
        self.detail.config(state="disabled")
        return name

    # ---------- 动作 ----------
    def toggle_selected(self):
        name = self.selected_name()
        if not name or self.manager is None:
            self._hint("先选一个技能。")
            return False
        row = next((item for item in self._rows if item.get("name") == name), {})
        target = not bool(row.get("enabled"))
        if self.manager.enable(name, target):
            self._hint("已%s %s。" % ("启用" if target else "停用", name))
            self.refresh()
            self.show_detail()
            return True
        self._hint("操作失败。")
        return False

    def favorite_selected(self, flag=True):
        name = self.selected_name()
        if not name or self.manager is None:
            self._hint("先选一个技能。")
            return False
        if self.manager.favorite(name, flag):
            self._hint("已%s %s。" % ("标记常用" if flag else "取消常用", name))
            return True
        self._hint("需要记忆可用才能记住常用技能。")
        return False

    def _auto_match(self):
        manager = self.manager
        learning = getattr(manager, "learning", None) if manager is not None else None
        if learning is None:
            return True
        try:
            return bool(learning.auto_match_enabled())
        except Exception:
            return True

    def _save_auto(self):
        manager = self.manager
        learning = getattr(manager, "learning", None) if manager is not None else None
        if learning is None:
            return False
        value = bool(self.auto_var.get())
        ok = learning.set_auto_match(value)
        self._hint("已%s自动匹配。" % ("开启" if value else "关闭") if ok else "保存失败。")
        return ok

    def reload_skills(self):
        if self.manager is None:
            self._hint("没有接入技能系统。")
            return False
        try:
            self.manager.reload()
        except Exception as exc:
            self._hint("重新扫描失败：%s" % exc)
            return False
        self._hint("已重新扫描技能目录。")
        self.refresh()
        return True

    def import_skill(self):
        if self.manager is None:
            self._hint("没有接入技能系统。")
            return False
        path = filedialog.askdirectory(parent=self.win, title="选择技能文件夹（里面要有 SKILL.md）")
        if not path:
            return False
        result = self.manager.import_skill(path)
        if not result.get("ok"):
            messagebox.showerror("导入失败", str(result.get("error")), parent=self.win)
            return False
        skipped = result.get("skipped") or []
        lines = ["已导入：%s" % result.get("name"), "位置：%s" % result.get("path")]
        if skipped:
            lines.append("跳过的文件（需要单独授权）：")
            lines.extend("  - %s" % item for item in skipped)
        lines.append(result.get("note") or "")
        messagebox.showinfo("导入完成", "\n".join(lines), parent=self.win)
        self.refresh()
        return True

    # ---------- 技能包导入（先看清再落地） ----------
    def _skills_root(self):
        loader = getattr(self.manager, "loader", None) if self.manager is not None else None
        roots = list(getattr(loader, "roots", []) or [])
        return roots[0] if roots else ""

    def _importer(self):
        from extensions.importer import SkillImporter

        return SkillImporter()

    def _report_text(self, report):
        lines = [
            "名称：%s" % (report.name or "读不出来"),
            "版本：%s" % (report.version or "未声明"),
            "核验结论：%s" % report.verdict_label(),
            "来源：%s" % (report.source or "未记录"),
        ]
        if report.blocked_reason:
            lines.append("不能导入的原因：%s" % report.blocked_reason)
        lines.append("许可证：%s（%s）" % (report.license or "未声明", report.license_status))
        lines.append("联网：%s" % ("需要" if report.requires_network else "不需要"))
        if report.files:
            lines.append("包内文件 %d 个" % len(report.files))
        if report.scripts:
            lines.append("脚本（只登记，不执行，不复制）：")
            lines.extend("  - %s" % item for item in report.scripts)
        if report.unknown_files:
            lines.append("类型不支持的文件：%s" % "、".join(report.unknown_files))
        if report.suspicious:
            lines.append("可疑写法：")
            lines.extend("  - %s：%s" % (item[0], item[1]) for item in report.suspicious)
        if report.unlisted_refs:
            lines.append("引用了但包里没有的文件：%s" % "、".join(report.unlisted_refs))
        if report.external_urls:
            lines.append("外部地址：%s" % "、".join(report.external_urls[:5]))
        for note in report.notes:
            lines.append("· %s" % note)
        return "\n".join(lines)

    def _confirm_and_apply(self, report):
        text = self._report_text(report)
        if report.verdict == "blocked":
            messagebox.showerror("拒绝导入", text, parent=self.win)
            self._hint("这个技能包被安全检查挡住了，没有落地。")
            return False
        root = self._skills_root()
        if not root:
            messagebox.showerror("导入失败", "技能目录读不到，先不导入。", parent=self.win)
            return False
        text += "\n\n要现在导入吗？只会搬说明和文本参考，脚本不搬。"
        if not messagebox.askyesno("导入前确认", text, parent=self.win):
            self._importer().discard(report)
            self._hint("已取消，没有改动任何文件。")
            return False
        result = self._importer().apply(report, root)
        if not result.get("ok"):
            messagebox.showerror("导入失败", str(result.get("error")), parent=self.win)
            return False
        lines = ["已导入：%s" % result.get("name"), "位置：%s" % result.get("path")]
        skipped = result.get("skipped") or []
        if skipped:
            lines.append("跳过的文件：")
            lines.extend("  - %s" % item for item in skipped)
        lines.append(result.get("note") or "")
        messagebox.showinfo("导入完成", "\n".join(lines), parent=self.win)
        self.reload_skills()
        return True

    def import_package(self):
        """从压缩包导入：先摊到临时目录看清结构，用户点头才落地。"""
        if self.manager is None:
            self._hint("没有接入技能系统。")
            return False
        path = filedialog.askopenfilename(
            parent=self.win,
            title="选择技能压缩包",
            filetypes=[("技能包", "*.zip"), ("全部文件", "*.*")],
        )
        if not path:
            return False
        try:
            report = self._importer().inspect_zip(path)
        except Exception as exc:
            messagebox.showerror("检查失败", str(exc), parent=self.win)
            return False
        return self._confirm_and_apply(report)

    def _known_license(self, link):
        """清单里核过的许可证，直接带进检查单，免得又显示成「未声明」。"""
        try:
            from extensions import qoder as qoder_module
            from extensions.catalog import builtin_catalog

            skill_id = qoder_module.skill_id_from_url(link)
            if not skill_id:
                return "", ""
            for manifest in builtin_catalog():
                if str(manifest.pinned or "").startswith(skill_id):
                    if manifest.license_status == "approved":
                        return manifest.license, "approved"
                    return "", ""
        except Exception:
            return "", ""
        return "", ""

    def import_from_market(self):
        """从市场链接导入公开内容。没有网络就如实说取不到，不假装成功。"""
        if self.manager is None:
            self._hint("没有接入技能系统。")
            return False
        link = simpledialog.askstring(
            "从市场链接导入",
            "粘贴技能页面的链接：",
            parent=self.win,
        )
        if not link:
            return False
        hint, hint_status = self._known_license(link.strip())
        try:
            report = self._importer().inspect_qoder(
                link.strip(), license_hint=hint, license_status_hint=hint_status
            )
        except Exception as exc:
            messagebox.showerror("检查失败", str(exc), parent=self.win)
            return False
        if report.verdict == "review" and not report.stage_dir:
            messagebox.showwarning(
                "暂时取不到", self._report_text(report), parent=self.win
            )
            self._hint("内容取不到，先记为待核验。")
            return False
        return self._confirm_and_apply(report)

    def _hint(self, text):
        try:
            self.hint.config(text=text or "")
        except Exception:
            return None
        return text
