"""记忆管理界面：看数量、按层翻、搜索、改、删、锁、看来源、导出、备份。

只读写记忆，不碰模型。删除和恢复都要用户确认，绝不自动发生。
"""

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from memory import backup as memory_backup
from ui import panel, theme

KIND_LABELS = {
    "long_term": "长期事实",
    "project": "项目",
    "relationship": "关系",
    "feedback": "人格反馈",
}

SOURCE_LABELS = {
    "USER_EXPLICIT": "用户亲口说",
    "SYSTEM_OBSERVED": "程序观察到",
    "MODEL_INFERRED": "模型推测",
}

FILTERS = (
    ("全部", None),
    ("长期事实", "long_term"),
    ("项目", "project"),
    ("关系", "relationship"),
    ("人格反馈", "feedback"),
)


def source_text(row):
    """一条记忆的来源说明，给用户看清楚这是谁说的。"""
    source = SOURCE_LABELS.get(row.get("source"), str(row.get("source") or "未知"))
    if not row.get("confirmed", 1):
        source += "（未确认）"
    return source


def describe_row(row):
    """来源详情：谁说的、什么时候记的、用过没、锁没锁。"""
    return "\n".join(
        [
            f"层次：{KIND_LABELS.get(row.get('kind'), row.get('kind'))}",
            f"来源：{source_text(row)}",
            f"重要度：{row.get('importance')}",
            f"可信度：{row.get('confidence')}",
            f"锁定：{'是' if row.get('locked') else '否'}",
            f"记录时间：{row.get('created_at') or '未知'}",
            f"最近用到：{row.get('last_used_at') or '还没用过'}",
        ]
    )


class MemoryWindow:
    """记忆管理窗口。所有动作都通过宿主的 memory 对象完成。"""
    TITLE = "记忆"


    GEOMETRY = (780, 540)

    def __init__(self, pet, parent=None):

        self.pet = pet
        self.memory = getattr(pet, "memory", None)
        # 嵌进主工作台是一块 Frame，单独打开才是一个窗口。
        self.embedded = parent is not None
        self.win = panel.container(pet, self.TITLE, self.GEOMETRY, parent)
        self._build()
        self.refresh()

    # ---------- 搭建 ----------
    def _build(self):
        header = theme.card(self.win)
        header.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_MD, theme.PAD_SM))
        head = tk.Frame(header, bg=theme.CARD)
        head.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_SM, theme.PAD_XS))
        theme.label(head, "记忆", kind="title").pack(side="left")
        self.stats_label = theme.label(head, "", kind="small", fg=theme.MUTED)
        self.stats_label.pack(side="left", padx=theme.PAD_MD)

        bar = tk.Frame(header, bg=theme.CARD)
        bar.pack(fill="x", padx=theme.PAD_MD, pady=(0, theme.PAD_SM))
        theme.label(bar, "分类", kind="small", fg=theme.MUTED).pack(side="left")
        self.kind_var = tk.StringVar(value=FILTERS[0][0])
        self.kind_box = ttk.Combobox(
            bar, values=[name for name, _ in FILTERS], textvariable=self.kind_var,
            state="readonly", width=10, font=theme.FONT_BODY,
        )
        self.kind_box.pack(side="left", padx=theme.PAD_SM)
        self.kind_box.bind("<<ComboboxSelected>>", lambda event: self.refresh())
        theme.label(bar, "搜索", kind="small", fg=theme.MUTED).pack(side="left")
        self.search_entry = theme.entry(bar)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=theme.PAD_SM, ipady=4)
        self.search_entry.bind("<Return>", lambda event: self.refresh())
        theme.button(bar, "查找", self.refresh, kind="primary").pack(side="left")
        self.archived_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            bar, text="含已归档", variable=self.archived_var, command=self.refresh,
            bg=theme.BG, fg=theme.TEXT, selectcolor=theme.FIELD,
            activebackground=theme.BG, activeforeground=theme.ACCENT,
            font=theme.FONT_SMALL,
        ).pack(side="left", padx=theme.PAD_SM)

        table = theme.card(self.win)
        table.pack(fill="both", expand=True, padx=theme.PAD_MD, pady=theme.PAD_SM)
        columns = ("id", "kind", "source", "importance", "locked", "content")
        self.tree = ttk.Treeview(table, columns=columns, show="headings", height=12)
        for key, title, width in (
            ("id", "编号", 50),
            ("kind", "层次", 80),
            ("source", "来源", 110),
            ("importance", "重要度", 60),
            ("locked", "锁定", 50),
            ("content", "内容", 380),
        ):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="w", stretch=(key == "content"))
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda event: self.show_source())

        actions_card = theme.card(self.win)
        actions_card.pack(fill="x", padx=theme.PAD_MD)
        actions = tk.Frame(actions_card, bg=theme.CARD)
        actions.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_SM, theme.PAD_XS))
        theme.button(actions, "查看来源", self.show_source).pack(side="left")
        theme.button(actions, "编辑", self.edit_selected).pack(side="left", padx=theme.PAD_XS)
        theme.button(actions, "锁定/解锁", self.toggle_lock).pack(side="left", padx=theme.PAD_XS)
        theme.button(actions, "确认为事实", self.confirm_selected).pack(
            side="left", padx=theme.PAD_XS
        )
        theme.button(actions, "删除", self.delete_selected, kind="danger").pack(
            side="left", padx=theme.PAD_XS
        )
        theme.button(actions, "导出", self.export_now).pack(side="right")
        theme.button(actions, "备份整库", self.backup_now).pack(
            side="right", padx=theme.PAD_XS
        )
        theme.button(actions, "恢复整库", self.restore_from_file).pack(
            side="right", padx=theme.PAD_XS
        )
        theme.button(actions, "清空全部", self.clear_all_memory, kind="danger").pack(
            side="right", padx=theme.PAD_XS
        )

        self.hint = theme.label(actions_card, "", kind="small", fg=theme.MUTED, anchor="w")
        self.hint.pack(fill="x", padx=theme.PAD_MD, pady=(0, theme.PAD_XS))
        panel.close_button(actions_card, self).pack(pady=theme.PAD_XS)
        theme.cardify(header)
        theme.cardify(table)
        theme.cardify(actions_card)

    # ---------- 数据 ----------
    def _kind(self):
        for name, value in FILTERS:
            if name == self.kind_var.get():
                return value
        return None

    def refresh(self):
        """重新读一遍。记忆不可用时显示提示，不弹错误。"""
        if self.tree is None:
            return []
        for item in self.tree.get_children():
            self.tree.delete(item)
        if self.memory is None:
            self._hint("记忆不可用，打不开记忆库。")
            self.stats_label.config(text="")
            return []
        rows = []
        try:
            rows = self.memory.browse(
                kind=self._kind(),
                query=self.search_entry.get().strip() or None,
                include_archived=self.archived_var.get(),
                limit=500,
            )
        except Exception as exc:
            self._hint(f"读取记忆失败：{exc}")
        for row in rows:
            self.tree.insert(
                "",
                "end",
                iid=str(row["id"]),
                values=(
                    row["id"],
                    KIND_LABELS.get(row.get("kind"), row.get("kind")),
                    source_text(row),
                    row.get("importance"),
                    "是" if row.get("locked") else "",
                    str(row.get("content") or "").replace("\n", " ")[:200],
                ),
            )
        self.stats_label.config(text=self._stats_text())
        self._hint(f"共 {len(rows)} 条。双击一行看来源。")
        return rows

    def _stats_text(self):
        try:
            stats = self.memory.stats()
        except Exception:
            return ""
        kinds = stats.get("kinds") or {}
        parts = [f"共 {stats.get('total', 0)} 条"]
        for key, label in KIND_LABELS.items():
            if kinds.get(key):
                parts.append(f"{label} {kinds[key]}")
        if stats.get("archived"):
            parts.append(f"已归档 {stats['archived']}")
        parts.append(f"容量 {stats.get('capacity')}")
        parts.append(self._index_text())
        return "  ·  ".join(parts)

    def _index_text(self):
        """检索索引状态：建了就说分词和条数，没建就不提索引本身。"""
        index = getattr(self.memory, "fts", None)
        if index is None:
            return "检索：关键词"
        try:
            state = index.describe()
        except Exception:
            return "检索：关键词"
        if state.get("ready"):
            return "检索索引 %s %d 条" % (state.get("tokenizer") or "默认", state.get("indexed", 0))
        return "检索：关键词（索引不可用）"

    def _hint(self, text):
        try:
            self.hint.config(text=text or "")
        except Exception:
            pass
        return text

    def selected_id(self):
        selection = self.tree.selection()
        return int(selection[0]) if selection else None

    def _selected_row(self):
        memory_id = self.selected_id()
        if memory_id is None:
            self._hint("先选一条记忆。")
            return None
        try:
            row = self.memory.repository.get_memory(memory_id)
        except Exception as exc:
            self._hint(f"读不到这条：{exc}")
            return None
        if row is None:
            self._hint("这条记忆已经不在库里了。")
        return row

    # ---------- 动作 ----------
    def show_source(self):
        row = self._selected_row()
        if row is None:
            return False
        messagebox.showinfo("记忆来源", describe_row(row), parent=self.win)
        return True

    def toggle_lock(self):
        row = self._selected_row()
        if row is None:
            return False
        locked = not bool(row.get("locked"))
        if self.memory.set_locked(row["id"], locked):
            self._hint("已锁定，容量满时也不会被归档。" if locked else "已解锁。")
            self.refresh()
            return True
        self._hint("锁定失败。")
        return False

    def confirm_selected(self):
        row = self._selected_row()
        if row is None:
            return False
        if self.memory.confirm_memory(row["id"], True):
            self._hint("已记下：这条算你说过的事实。")
            self.refresh()
            return True
        self._hint("确认失败。")
        return False

    def delete_selected(self):
        row = self._selected_row()
        if row is None:
            return False
        if not messagebox.askyesno(
            "删除记忆", f"确定删掉这条吗？\n\n{row.get('content')}", parent=self.win
        ):
            self._hint("已取消，没有删。")
            return False
        if self.memory.forget(row["id"], confirm=True):
            self._hint("已删除。")
            self.refresh()
            return True
        self._hint("删除失败。")
        return False

    def edit_selected(self):
        row = self._selected_row()
        if row is None:
            return False
        editor = tk.Toplevel(self.win)
        editor.title("编辑记忆")
        editor.configure(bg=theme.BG)
        editor.transient(self.win)
        theme.label(editor, "内容", kind="small", fg=theme.MUTED).pack(
            anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_MD, 0)
        )
        text = theme.text_area(editor, read_only=False, height=6, width=48)
        text.insert("1.0", str(row.get("content") or ""))
        text.pack(padx=theme.PAD_MD, pady=theme.PAD_SM)
        row_frame = tk.Frame(editor, bg=theme.BG)
        row_frame.pack(fill="x", padx=theme.PAD_MD)
        theme.label(row_frame, "重要度", kind="small", fg=theme.MUTED).pack(side="left")
        importance = theme.entry(row_frame, width=6)
        importance.insert(0, str(row.get("importance") or 1))
        importance.pack(side="left", padx=theme.PAD_SM, ipady=3)

        def save():
            content = text.get("1.0", "end").strip()
            try:
                level = int(importance.get().strip() or 1)
            except ValueError:
                level = int(row.get("importance") or 1)
            if not content:
                self._hint("内容不能空着。")
                return
            if self.memory.edit(row["id"], content=content, importance=level):
                self._hint("已保存。")
                editor.destroy()
                self.refresh()
            else:
                self._hint("保存失败。")

        buttons = tk.Frame(editor, bg=theme.BG)
        buttons.pack(fill="x", padx=theme.PAD_MD, pady=theme.PAD_MD)
        theme.button(buttons, "保存", save, kind="primary").pack(side="right")
        theme.button(buttons, "取消", editor.destroy).pack(side="right", padx=theme.PAD_SM)
        return True

    def export_now(self):
        export_dir = self._export_dir()
        result = memory_backup.export_to_file(
            self.memory, export_dir / "memory-export.json"
        )
        if result.get("ok"):
            self._hint(f"已导出到 {result['path']}")
        else:
            self._hint(f"导出失败：{result.get('error')}")
        return bool(result.get("ok"))

    def backup_now(self):
        db_path = getattr(self.pet.config, "db_path", "")
        result = memory_backup.backup_database(db_path)
        if result.get("ok"):
            self._hint(f"已备份整库到 {result['path']}")
        else:
            self._hint(f"备份失败：{result.get('error')}")
        return bool(result.get("ok"))

    def restore_from_file(self):
        """挑一个备份文件恢复整库。先验真、先另存原库、再问一次。

        恢复本身就带一份覆盖前的原库备份，所以不静默丢数据。
        """
        db_path = getattr(self.pet.config, "db_path", "")
        if not db_path:
            self._hint("找不到数据库位置，没法恢复。")
            return False
        picked = filedialog.askopenfilename(
            parent=self.win,
            title="选一个备份文件",
            filetypes=[("SQLite 备份", "*.sqlite3"), ("所有文件", "*.*")],
        )
        if not picked:
            self._hint("没选文件，什么也没动。")
            return False
        check = memory_backup.validate_backup(picked)
        if not check.get("ok"):
            self._hint("这个备份不能用：%s" % check.get("error"))
            return False
        if not messagebox.askyesno(
            "恢复整库",
            "会用这个备份覆盖当前记忆库。\n覆盖前会把当前库另存一份。\n\n确定吗？",
            parent=self.win,
        ):
            self._hint("已取消，没有动数据。")
            return False
        result = memory_backup.restore_database(db_path, picked, confirm=True)
        if not result.get("ok"):
            self._hint("恢复失败：%s" % result.get("error"))
            return False
        safety = result.get("safety") or "（当前还没有库，没另存）"
        self.refresh()
        self._hint("已恢复。原库另存在 %s。重启桌宠后生效。" % safety)
        return True

    def clear_all_memory(self):
        """清空全部长期记忆。先问一遍，答应用户才动。"""
        if self.memory is None:
            self._hint("记忆不可用，清不了。")
            return False
        if not messagebox.askyesno(
            "清空记忆",
            "会删掉全部长期事实、项目、关系和人格反馈。\n这一下收不回来，确定吗？",
            parent=self.win,
        ):
            self._hint("已取消，没有删。")
            return False
        result = memory_backup.clear_all(self.memory, confirm=True)
        if not result.get("ok"):
            self._hint("清空失败：%s" % result.get("error"))
            return False
        self.refresh()
        self._hint("已清空 %d 条。" % int(result.get("removed") or 0))
        return True

    def _export_dir(self):
        base = getattr(self.pet.config, "data_dir", None)
        target = (base / "exports") if base is not None else None
        if target is None:
            target = self.win.master  # 兜底：至少不报错
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return target
