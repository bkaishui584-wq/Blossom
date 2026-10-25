"""文件与知识页：加目录、扫一遍、按关键词找片段。

不做向量检索，也不自动导入全盘：只索引用户明确加进来的目录。
搜出来的是原文件里的片段，顺着路径就能回原文。
"""

import os
import threading
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

from tools.workspace import FILE_TOOL_NAMES
from ui import panel, theme


class KnowledgeWindow:
    """本地知识库。数据只从 DeskPet 上的知识库服务读。"""

    TITLE = "文件与知识"
    GEOMETRY = (960, 620)

    def __init__(self, pet, parent=None):
        self.pet = pet
        # 嵌进主工作台是一块 Frame，单独打开才是一个窗口。
        self.embedded = parent is not None
        self.win = panel.container(pet, self.TITLE, self.GEOMETRY, parent)
        self._busy = False
        self._results = []
        self._org_plan_id = ""
        self._build()
        self.refresh()

    # ---------- 数据源 ----------
    def _service(self):
        return getattr(self.pet, "knowledge", None)

    def _state(self):
        service = self._service()
        if service is None:
            return {"ready": False, "documents": 0, "chunks": 0, "indexed": 0, "roots": []}
        try:
            return service.describe()
        except Exception as exc:
            return {
                "ready": False, "documents": 0, "chunks": 0, "indexed": 0,
                "roots": [], "reason": str(exc),
            }

    def _sources(self):
        service = self._service()
        if service is None:
            return []
        try:
            return service.index.sources()
        except Exception:
            return []

    # ---------- 搭建 ----------
    def _build(self):
        head = tk.Frame(self.win, bg=theme.BG)
        head.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_MD, theme.PAD_SM))
        theme.label(head, "文件与知识", kind="title").pack(side="left")
        self.state_label = theme.label(head, "", kind="small", fg=theme.MUTED)
        self.state_label.pack(side="left", padx=theme.PAD_MD)
        theme.button(head, "刷新", self.refresh, kind="primary").pack(side="right")

        self.book = ttk.Notebook(self.win)
        self.book.pack(fill="both", expand=True, padx=theme.PAD_MD, pady=theme.PAD_SM)
        for title, builder in (
            ("目录", self._build_dirs),
            ("整理", self._build_organize),
            ("搜索", self._build_search),
            ("状态", self._build_state),
        ):
            frame = tk.Frame(self.book, bg=theme.BG)
            self.book.add(frame, text=title)
            builder(frame)

        self.hint = theme.label(self.win, "", kind="small", fg=theme.MUTED, anchor="w")
        self.hint.pack(fill="x", padx=theme.PAD_MD)
        panel.close_button(self.win, self).pack(pady=theme.PAD_SM)

    def _tree(self, parent, columns, height=10):
        box = tk.Frame(parent, bg=theme.BG)
        box.pack(fill="both", expand=True, padx=theme.PAD_SM, pady=theme.PAD_SM)
        tree = ttk.Treeview(box, columns=[key for key, _, _ in columns], show="headings", height=height)
        for key, title, width in columns:
            tree.heading(key, text=title)
            tree.column(key, width=width, anchor="w", stretch=(key == columns[-1][0]))
        scroll = ttk.Scrollbar(box, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        return tree

    def _build_dirs(self, parent):
        self.dir_tree = self._tree(
            parent, (("root", "目录", 560), ("docs", "已索引", 90), ("scan", "最近扫描", 180))
        )
        bar = tk.Frame(parent, bg=theme.BG)
        bar.pack(fill="x", padx=theme.PAD_SM, pady=(0, theme.PAD_SM))
        theme.button(bar, "添加目录", self.add_dir, kind="primary").pack(side="left")
        theme.button(bar, "扫描", self.scan_now).pack(side="left", padx=theme.PAD_XS)
        theme.button(bar, "移除目录", self.remove_dir, kind="danger").pack(side="left", padx=theme.PAD_XS)
        theme.label(
            bar, "只读你加进来的目录；不自动扫全盘、不联网。", kind="small", fg=theme.MUTED
        ).pack(side="left", padx=theme.PAD_SM)

    def _build_search(self, parent):
        bar = tk.Frame(parent, bg=theme.BG)
        bar.pack(fill="x", padx=theme.PAD_SM, pady=(theme.PAD_SM, 0))
        theme.label(bar, "关键词", kind="small", fg=theme.MUTED).pack(side="left")
        self.search_entry = theme.entry(bar)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=theme.PAD_SM, ipady=4)
        self.search_entry.bind("<Return>", lambda event: self.do_search())
        theme.button(bar, "搜索", self.do_search, kind="primary").pack(side="left")
        self.result_tree = self._tree(
            parent,
            (("file", "文件", 220), ("locator", "位置", 110), ("snippet", "摘录", 520)),
            height=13,
        )
        self.result_tree.bind("<Double-1>", lambda event: self.open_selected())
        actions = tk.Frame(parent, bg=theme.BG)
        actions.pack(fill="x", padx=theme.PAD_SM, pady=(0, theme.PAD_SM))
        theme.button(actions, "打开所在文件夹", self.open_selected).pack(side="left")

    def _build_state(self, parent):
        self.state_tree = self._tree(
            parent, (("label", "项目", 180), ("value", "现在", 700)), height=12
        )

    def _build_organize(self, parent):
        bar = tk.Frame(parent, bg=theme.BG)
        bar.pack(fill="x", padx=theme.PAD_SM, pady=(theme.PAD_SM, 0))
        theme.label(bar, "授权目录", kind="small", fg=theme.MUTED).pack(side="left")
        self.org_root_var = tk.StringVar(value="")
        self.org_root_box = ttk.Combobox(
            bar, textvariable=self.org_root_var, state="readonly", width=42
        )
        self.org_root_box.pack(side="left", padx=theme.PAD_SM)
        theme.button(bar, "预览整理", self.organize_preview, kind="primary").pack(
            side="left"
        )
        self.org_apply_button = theme.button(
            bar, "执行计划", self.organize_apply, kind="danger", state="disabled"
        )
        self.org_apply_button.pack(side="left", padx=theme.PAD_SM)
        self.org_undo_button = theme.button(
            bar, "撤销选中", self.organize_undo, kind="danger", state="disabled"
        )
        self.org_undo_button.pack(side="left")

        self.org_summary = theme.label(
            parent, "", kind="small", fg=theme.MUTED, bg=theme.BG,
            wraplength=850, justify="left",
        )
        self.org_summary.pack(fill="x", padx=theme.PAD_SM, pady=(theme.PAD_XS, 0))
        self.org_tree = self._tree(
            parent,
            (
                ("action", "动作", 70),
                ("source", "原位置", 330),
                ("target", "目标位置", 330),
                ("status", "状态", 80),
            ),
            height=9,
        )
        theme.label(parent, "整理历史", kind="small", fg=theme.MUTED, bg=theme.BG).pack(
            anchor="w", padx=theme.PAD_SM
        )
        self.org_history = self._tree(
            parent,
            (
                ("time", "时间", 140),
                ("state", "状态", 90),
                ("moved", "移动", 60),
                ("note", "说明", 520),
            ),
            height=6,
        )
        self.org_history.bind("<<TreeviewSelect>>", lambda event: self._org_history_picked())

    def _organizer(self):
        return getattr(self.pet, "file_organizer", None) or getattr(
            getattr(self.pet, "agent", None), "file_organizer", None
        )

    def _tool_executor(self):
        return getattr(getattr(self.pet, "agent", None), "executor", None)

    def organize_preview(self):
        root = str(self.org_root_var.get() or "").strip()
        if not root:
            return self._note("先选一个已经授权并加进知识库的目录。")
        service = self._organizer()
        if service is None:
            return self._note("文件整理服务没有接上。")
        try:
            result = service.preview(root, mode="type")
        except Exception as exc:
            return self._note("预览失败：%s" % exc)
        if not result.get("ok"):
            return self._note(result.get("error") or "预览失败")
        data = result.get("data") or {}
        self._org_plan_id = str(data.get("operation_id") or "")
        self._org_plan_root = str(data.get("root") or "")
        rows = []
        for item in data.get("items") or []:
            rows.append(
                (
                    "移动",
                    item.get("source"),
                    item.get("target"),
                    item.get("status"),
                )
            )
        self._fill(self.org_tree, rows)
        self.org_apply_button.configure(state="normal" if self._org_plan_id else "disabled")
        self.org_summary.configure(
            text="文件 %d，可移动 %d，冲突 %d；重复组 %d，空文件 %d，旧文件 %d。"
            % (
                data.get("files", 0),
                data.get("ready", 0),
                data.get("conflicts", 0),
                len(data.get("duplicates") or []),
                len(data.get("empty") or []),
                len(data.get("old") or []),
            )
        )
        return data

    def organize_apply(self):
        plan_id = str(getattr(self, "_org_plan_id", "") or "")
        if not plan_id:
            return self._note("先预览一次整理计划。")
        executor = self._tool_executor()
        outcome = executor.execute("organize_apply", {"operation_id": plan_id}) if executor else None
        if outcome is None:
            return self._note("工具执行层没有接上。")
        self._note("执行完成。" if outcome.ok else (outcome.error or "执行失败"))
        self._org_plan_id = ""
        self.org_apply_button.configure(state="disabled")
        self._load_organize_history()
        return outcome.ok

    def organize_undo(self):
        picked = self.org_history.selection()
        if not picked:
            return self._note("先在整理历史里选一条记录。")
        operation_id = str(self.org_history.item(picked[0], "values")[0])
        executor = self._tool_executor()
        outcome = executor.execute("organize_undo", {"operation_id": operation_id}) if executor else None
        if outcome is None:
            return self._note("工具执行层没有接上。")
        self._note("已撤销。" if outcome.ok else (outcome.error or "撤销失败"))
        self._load_organize_history()
        return outcome.ok

    def _load_organize_history(self):
        service = self._organizer()
        if service is None:
            self._fill(self.org_history, [])
            return []
        rows = []
        for row in service.history(limit=30):
            stamp = str(row.get("created_at") or "")
            try:
                stamp = datetime.fromtimestamp(float(row.get("created_at"))).strftime("%m-%d %H:%M")
            except Exception:
                pass
            rows.append(
                (
                    row.get("id"),
                    row.get("state"),
                    row.get("moved", 0),
                    "%s %s" % (stamp, row.get("note") or ""),
                )
            )
        self._fill(self.org_history, rows)
        return rows

    def _org_history_picked(self):
        picked = self.org_history.selection()
        if not picked:
            return None
        values = self.org_history.item(picked[0], "values")
        state = str(values[1] if len(values) > 1 else "")
        self.org_undo_button.configure(
            state="normal" if state in ("applied", "failed") else "disabled"
        )
        return values[0] if values else None

    # ---------- 目录动作 ----------
    def _selected_root(self):
        picked = self.dir_tree.selection()
        if not picked:
            return ""
        return str(self.dir_tree.item(picked[0], "values")[0] or "").strip()

    def _grant(self, directory):
        """在权限中心放行这个目录，和「本地能力」里的授权走同一套。"""
        permissions = getattr(getattr(self.pet, "agent", None), "permissions", None)
        store = getattr(permissions, "grants", None)
        if store is None:
            return False
        scope = "path:" + str(directory)
        for tool in FILE_TOOL_NAMES:
            try:
                store.set(tool, "always", scope=scope, note="用户在知识库里加目录")
            except Exception:
                return False
        return True

    def add_dir(self):
        service = self._service()
        if service is None:
            return self._note("这一层没有接入知识库。")
        picked = filedialog.askdirectory(parent=self.win, title="选一个要加进知识库的目录")
        if not picked:
            return ""
        picked = os.path.realpath(picked)
        if not service.is_authorized(picked) and not self._grant(picked):
            self._note("这个目录没授权。先在「本地能力与权限」里放行，再加进来。")
            return ""
        try:
            root = service.add_root(picked)
        except Exception as exc:
            self._note("加不了：%s" % exc)
            return ""
        self._note("已加目录：%s。点「扫描」把它读进索引。" % root)
        self.refresh()
        return root

    def remove_dir(self):
        service = self._service()
        root = self._selected_root()
        if service is None or not root:
            return self._note("先在列表里选一个目录。")
        try:
            docs = len(service.index.documents_under(root))
        except Exception:
            docs = 0
        if not messagebox.askyesno(
            "移出知识库", "把「%s」移出知识库？\n它在索引里的 %d 份文档记录会一起删掉，原文件不动。" % (root, docs)
        ):
            return ""
        result = service.remove_root(root)
        self._note("已移出：%s，删掉 %d 条索引记录。" % (root, result.get("documents", 0)))
        self.refresh()
        return result

    def scan_now(self):
        service = self._service()
        root = self._selected_root()
        if service is None:
            return self._note("这一层没有接入知识库。")
        if not root:
            return self._note("先在列表里选一个目录。")
        if self._busy:
            return self._note("上一轮还在扫，稍等一下。")
        self._busy = True
        self._note("正在扫 %s …" % root)

        def work():
            try:
                result = service.scan_root(root, on_progress=self._progress)
            except Exception as exc:
                result = {"error": str(exc)}
            self._later(lambda: self._scan_done(result))

        threading.Thread(target=work, daemon=True).start()
        return True

    def _progress(self, found, item):
        # 后台线程只回主线程刷字，绝不直接碰控件。
        if found % 10 == 0:
            self._later(lambda: self._note("扫到第 %d 个…" % found))

    def _scan_done(self, result):
        self._busy = False
        if result.get("error"):
            self._note("扫描没做成：%s" % result["error"])
        else:
            self._note(
                "扫完：文件 %d，读进索引 %d，跳过 %d，失败 %d。"
                % (result["found"], result["indexed"], result["skipped"], result["failed"])
            )
        self.refresh()

    def _later(self, fn):
        try:
            self.pet.root.after(0, fn)
        except Exception:
            pass

    # ---------- 搜索 ----------
    def do_search(self):
        service = self._service()
        if service is None:
            return self._note("这一层没有接入知识库。")
        query = str(self.search_entry.get() or "").strip()
        if not query:
            return self._note("先写关键词。")
        try:
            rows = service.search(query, limit=20)
        except Exception as exc:
            return self._note("搜索出错：%s" % exc)
        self._results = rows
        self._fill(
            self.result_tree,
            [
                (row.get("relative") or row.get("name"), row.get("locator"), row.get("snippet"))
                for row in rows
            ],
        )
        if not rows:
            state = self._state()
            if not state.get("documents"):
                self._note("知识库还空着：先加目录、扫描，再来搜。")
            else:
                self._note("在 %d 份文档里没找到「%s」。" % (state.get("documents"), query))
        else:
            self._note("找到 %d 段。双击一条可以打开它所在的文件夹。" % len(rows))
        return rows

    def open_selected(self):
        picked = self.result_tree.selection()
        if not picked:
            return self._note("先选一条结果。")
        index = self.result_tree.index(picked[0])
        if index < 0 or index >= len(self._results):
            return self._note("这条结果已经过期，重新搜一次。")
        folder = os.path.dirname(str(self._results[index].get("path") or ""))
        if not folder or not os.path.isdir(folder):
            return self._note("找不到这个文件所在的文件夹。")
        try:
            os.startfile(folder)
        except Exception as exc:
            return self._note("打不开：%s" % exc)
        return folder

    # ---------- 刷新 ----------
    def refresh(self):
        state = self._state()
        if self._service() is None:
            label = "没接入"
        elif state.get("ready"):
            label = "%d 份文档 · %d 个片段 · %s 分词" % (
                state.get("documents", 0),
                state.get("chunks", 0),
                state.get("tokenizer") or "默认",
            )
        else:
            label = "索引不可用"
        try:
            self.state_label.configure(text=label)
        except Exception:
            pass
        self._fill(
            self.dir_tree,
            [
                (
                    str(row.get("root") or ""),
                    int(row.get("docs") or 0),
                    str(row.get("last_scan") or "还没扫过"),
                )
                for row in self._sources()
            ],
        )
        self._fill(
            self.state_tree,
            [
                ("文档数", state.get("documents", 0)),
                ("片段数", state.get("chunks", 0)),
                ("索引条数", state.get("indexed", 0)),
                ("分词方式", state.get("tokenizer") or "（没建成）"),
                ("索引状态", "可用" if state.get("ready") else (state.get("reason") or "不可用")),
                ("已加目录", "、".join(state.get("roots") or []) or "还没有"),
                ("检索方式", "关键词命中（不做向量检索，同义改写搜不到）"),
                ("数据在哪", "只在你自己的机器上；不联网、不上传"),
            ],
        )
        if self._service() is None:
            self._note("这一层没有接入知识库。")
        roots = [str(row.get("root") or "") for row in self._sources()]
        try:
            self.org_root_box.configure(values=roots)
            if roots and self.org_root_var.get() not in roots:
                self.org_root_var.set(roots[0])
            elif not roots:
                self.org_root_var.set("")
        except Exception:
            pass
        self._load_organize_history()

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
