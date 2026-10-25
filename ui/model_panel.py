"""设置 → 模型：模型与 API 管理。

版式按确认过的方案：服务商单选 → 配置名 → API Key（掩码）→ 接口地址 →
模型 ID → 用途 → 整行主按钮。下面是已保存的配置列表，点一行就能改。

这里的每个按钮都接真实逻辑：保存写配置仓库，测试会发一次最小请求，
删除会问是否连密钥一起删。没有"看起来能点、其实没做事"的控件。
"""

import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

from model import adapter, errors
from model import config as model_config
from ui import theme


class ModelPanel:
    """一块面板，挂在设置页的「模型」标签里。"""

    PROVIDER_ORDER = ("openai", "deepseek", "qwen", "custom", "ollama")

    def __init__(self, pet, parent):
        self.pet = pet
        self.repo = getattr(pet.config, "model_configs", None)
        self.secrets = getattr(pet.config, "secrets", None)
        self.root = parent
        self.editing_id = ""
        self.statuses = {}
        self.last_probe = {}
        self.provider_var = tk.StringVar(value="deepseek")
        self.name_var = tk.StringVar(value="")
        self.key_var = tk.StringVar(value="")
        self.base_var = tk.StringVar(value="")
        self.model_var = tk.StringVar(value="")
        self.role_vars = {role: tk.BooleanVar(value=role == "companion") for role in model_config.ROLES}
        self._build()
        self.refresh()
        rows = self.repo.enabled() if self.repo else []
        first = rows[0] if rows else None
        if first:
            self.load_config(first)
        else:
            self.on_provider()

    # ---------- 搭建 ----------
    def _build(self):
        outer = theme.scroll_area(self.root)
        card = theme.card(outer)
        card.pack(fill="x", padx=theme.PAD_MD, pady=theme.PAD_MD)
        pad = theme.PAD_MD

        head = tk.Frame(card, bg=theme.CARD)
        head.pack(fill="x", padx=pad, pady=(pad, 0))
        theme.label(head, "模型与 API 管理", kind="title", bg=theme.CARD).pack(side="left")
        self.badge = theme.label(head, "未配置", kind="small", bg=theme.CARD, fg=theme.WARN)
        self.badge.pack(side="right")

        theme.label(
            card,
            "Key 用 Windows 当前用户的 DPAPI 加密保存，不写进配置文件、日志和聊天记录；"
            "换电脑或换 Windows 用户都解不开。",
            kind="small",
            fg=theme.MUTED,
            bg=theme.CARD,
            wraplength=560,
            justify="left",
        ).pack(anchor="w", padx=pad, pady=(theme.PAD_XS, theme.PAD_SM))

        theme.label(card, "服务商", kind="small", fg=theme.MUTED, bg=theme.CARD).pack(
            anchor="w", padx=pad
        )
        providers = tk.Frame(card, bg=theme.CARD)
        providers.pack(fill="x", padx=pad, pady=(theme.PAD_XS, theme.PAD_SM))
        for key in self.PROVIDER_ORDER:
            preset = model_config.PROVIDERS[key]
            tk.Radiobutton(
                providers,
                text=preset["label"],
                value=key,
                variable=self.provider_var,
                command=self.on_provider,
                bg=theme.CARD,
                fg=theme.TEXT,
                selectcolor=theme.PANEL_2,
                activebackground=theme.CARD,
                activeforeground=theme.TEXT,
                highlightthickness=0,
                bd=0,
                anchor="w",
                font=theme.FONT_BODY,
            ).pack(anchor="w")

        self._field(card, "配置名称（可选）", self.name_var)
        self.key_entry = self._field(
            card,
            "API Key",
            self.key_var,
            show="*",
            hint="已保存时留空表示不修改；输入新值会替换旧 Key。",
        )
        self._field(card, "API 接口地址", self.base_var)
        theme.label(card, "模型 ID", kind="small", fg=theme.MUTED, bg=theme.CARD).pack(
            anchor="w", padx=pad, pady=(theme.PAD_XS, 0)
        )
        model_row = tk.Frame(card, bg=theme.CARD)
        model_row.pack(fill="x", padx=pad, pady=(2, 0))
        self.model_combo = ttk.Combobox(model_row, textvariable=self.model_var, values=[])
        self.model_combo.pack(side="left", fill="x", expand=True)
        theme.button(model_row, "获取列表", self.fetch_models).pack(
            side="left", padx=(theme.PAD_SM, 0)
        )
        theme.label(
            card,
            "服务商支持时可以直接拉取模型列表；拉不到就手动填。",
            kind="small",
            fg=theme.MUTED,
            bg=theme.CARD,
        ).pack(anchor="w", padx=pad, pady=(2, 0))

        roles = tk.Frame(card, bg=theme.CARD)
        roles.pack(fill="x", padx=pad, pady=(theme.PAD_XS, 0))
        theme.label(roles, "用途", kind="small", fg=theme.MUTED, bg=theme.CARD).pack(side="left")
        for role in model_config.ROLES:
            tk.Checkbutton(
                roles,
                text=model_config.ROLE_LABELS[role],
                variable=self.role_vars[role],
                bg=theme.CARD,
                fg=theme.TEXT,
                selectcolor=theme.PANEL_2,
                activebackground=theme.CARD,
                activeforeground=theme.TEXT,
                highlightthickness=0,
                bd=0,
                font=theme.FONT_BODY,
            ).pack(side="left", padx=(theme.PAD_SM, 0))

        theme.button(card, "保存并应用", self.save, kind="primary").pack(
            fill="x", padx=pad, pady=(theme.PAD_MD, theme.PAD_SM)
        )

        row = tk.Frame(card, bg=theme.CARD)
        row.pack(fill="x", padx=pad)
        theme.button(row, "测试连接", self.test_connection).pack(side="left")
        theme.button(row, "设为聊天默认", lambda: self.set_default("companion")).pack(
            side="left", padx=(theme.PAD_SM, 0)
        )
        theme.button(row, "设为任务默认", lambda: self.set_default("task")).pack(
            side="left", padx=(theme.PAD_SM, 0)
        )
        theme.button(row, "新建", self.new_config).pack(side="right")
        self.delete_button = theme.button(row, "删除", self.delete, kind="danger")
        self.delete_button.pack(side="right", padx=(0, theme.PAD_SM))
        self.docs_button = theme.button(row, "官方文档", self.open_docs)
        self.docs_button.pack(side="right", padx=(0, theme.PAD_SM))

        self.status_label = theme.label(
            card, "", kind="small", fg=theme.MUTED, bg=theme.CARD, wraplength=560, justify="left"
        )
        self.status_label.pack(anchor="w", padx=pad, pady=(theme.PAD_SM, theme.PAD_XS))

        theme.label(
            card,
            "测试连接会向该地址真实发送一次最小请求，可能产生极少量费用；不点就不会发。",
            kind="small",
            fg=theme.MUTED,
            bg=theme.CARD,
            wraplength=560,
            justify="left",
        ).pack(anchor="w", padx=pad, pady=(0, theme.PAD_MD))

        theme.label(outer, "已保存的配置", kind="body", bg=theme.BG).pack(
            anchor="w", padx=theme.PAD_MD, pady=(0, theme.PAD_XS)
        )
        table = tk.Frame(outer, bg=theme.BG)
        table.pack(fill="both", expand=True, padx=theme.PAD_MD, pady=(0, theme.PAD_MD))
        columns = ("name", "provider", "model", "roles", "status")
        self.tree = ttk.Treeview(table, columns=columns, show="headings", height=6)
        for key, title, width, stretch in (
            ("name", "名称", 150, True),
            ("provider", "服务商", 90, False),
            ("model", "模型", 150, True),
            ("roles", "用途", 110, False),
            ("status", "状态", 90, False),
        ):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="w", stretch=stretch)
        bar = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview, style="Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self.load_selected())

        self.current_label = theme.label(outer, "", kind="small", fg=theme.MUTED, bg=theme.BG)
        self.current_label.pack(anchor="w", padx=theme.PAD_MD, pady=(0, theme.PAD_MD))

        theme.label(outer, "最近的模型调用", kind="body", bg=theme.BG).pack(
            anchor="w", padx=theme.PAD_MD, pady=(0, theme.PAD_XS)
        )
        self.call_summary = theme.label(outer, "", kind="small", fg=theme.MUTED, bg=theme.BG)
        self.call_summary.pack(anchor="w", padx=theme.PAD_MD)
        calls = tk.Frame(outer, bg=theme.BG)
        calls.pack(fill="both", expand=True, padx=theme.PAD_MD, pady=(theme.PAD_XS, theme.PAD_MD))
        self.call_tree = ttk.Treeview(
            calls,
            columns=("at", "role", "model", "status", "ms", "tokens", "error"),
            show="headings",
            height=5,
        )
        for key, title, width in (
            ("at", "时间", 120),
            ("role", "用途", 70),
            ("model", "模型", 150),
            ("status", "结果", 70),
            ("ms", "耗时", 70),
            ("tokens", "Token", 80),
            ("error", "错误", 180),
        ):
            self.call_tree.heading(key, text=title)
            self.call_tree.column(key, width=width, anchor="w", stretch=(key in ("model", "error")))
        scroll = ttk.Scrollbar(calls, orient="vertical", command=self.call_tree.yview)
        self.call_tree.configure(yscrollcommand=scroll.set)
        self.call_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        call_row = tk.Frame(outer, bg=theme.BG)
        call_row.pack(fill="x", padx=theme.PAD_MD, pady=(0, theme.PAD_MD))
        theme.button(call_row, "刷新调用记录", self.refresh_calls).pack(side="left")
        theme.button(call_row, "清空调用记录", self.clear_calls, kind="danger").pack(
            side="left", padx=theme.PAD_SM
        )

    def _field(self, parent, label, variable, show=None, hint=""):
        theme.label(parent, label, kind="small", fg=theme.MUTED, bg=theme.CARD).pack(
            anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_XS, 0)
        )
        entry = theme.entry(parent, textvariable=variable)
        if show:
            entry.configure(show=show)
        entry.pack(fill="x", padx=theme.PAD_MD, pady=(2, 0))
        if hint:
            theme.label(
                parent, hint, kind="small", fg=theme.MUTED, bg=theme.CARD, wraplength=560, justify="left"
            ).pack(anchor="w", padx=theme.PAD_MD, pady=(2, 0))
        return entry

    # ---------- 表单 ----------
    def preset(self):
        return model_config.PROVIDERS.get(self.provider_var.get()) or model_config.PROVIDERS["custom"]

    def on_provider(self):
        preset = self.preset()
        self.base_var.set(preset.get("base_url") or "")
        self.model_var.set(preset.get("model") or "")
        self.name_var.set("")
        try:
            state = "normal" if preset.get("docs") else "disabled"
            self.docs_button.configure(state=state)
        except Exception:
            pass
        note = preset.get("note") or ""
        if note:
            self.set_status(note, theme.MUTED)
        return preset

    def controls(self):
        """取当前表单值。只读输入，不写任何东西。"""
        roles = [role for role, var in self.role_vars.items() if var.get()]
        return {
            "id": self.editing_id,
            "name": self.name_var.get().strip(),
            "provider": self.provider_var.get(),
            "base_url": self.base_var.get().strip(),
            "model": self.model_var.get().strip(),
            "roles": roles,
        }

    def load_config(self, row):
        self.editing_id = str(row.get("id") or "")
        self.provider_var.set(str(row.get("provider") or "custom"))
        self.name_var.set(str(row.get("name") or ""))
        self.base_var.set(str(row.get("base_url") or ""))
        self.model_var.set(str(row.get("model") or ""))
        self.key_var.set("")
        roles = row.get("roles") or []
        for role, var in self.role_vars.items():
            var.set(role in roles)
        try:
            self.docs_button.configure(state="normal" if self.preset().get("docs") else "disabled")
        except Exception:
            pass
        has_key = bool(self.secrets and self.secrets.has(row.get("secret_id")))
        self.set_status(
            "Key 已保存；留空表示不修改。" if has_key else "还没有保存 Key，填一个再保存。",
            theme.MUTED,
        )
        return row

    def load_selected(self):
        picked = self.tree.selection()
        if not picked:
            return False
        row = self.repo.get(picked[0]) if self.repo else None
        if row:
            self.load_config(row)
        return bool(row)

    def new_config(self):
        self.editing_id = ""
        self.key_var.set("")
        self.name_var.set("")
        for role, var in self.role_vars.items():
            var.set(role == "companion")
        self.on_provider()
        self.tree.selection_remove(self.tree.selection())
        self.set_status("新建配置：填好 Key 和模型后点「保存并应用」。", theme.MUTED)
        return True

    # ---------- 动作 ----------
    def save(self):
        if self.repo is None or self.secrets is None:
            self.set_status("配置仓库没有初始化，保存不了。", theme.ERROR)
            return False
        result = self.repo.upsert(self.controls())
        if not result.get("ok"):
            self.set_status(str(result.get("error") or "保存失败"), theme.ERROR)
            return False
        row = result["config"]
        key = self.key_var.get().strip()
        stored = {"ok": True}
        if key:
            stored = self.secrets.save(row["secret_id"], key)
            if not stored.get("ok"):
                self.set_status(
                    "配置已保存，但密钥没存进去：%s" % stored.get("error"), theme.ERROR
                )
                self.refresh()
                return False
        elif row.get("provider") != "ollama" and not self.secrets.has(row["secret_id"]):
            self.set_status("配置已保存，但还没有 API Key；填一个再保存。", theme.WARN)
            self.refresh()
            return False
        self.editing_id = row["id"]
        self.key_var.set("")
        reloaded = self.pet.reload_models()
        self.refresh()
        if reloaded.get("ok"):
            self.set_status("已保存并生效：%s" % row["name"], theme.OK)
        else:
            self.set_status(
                "配置已保存，但运行中的模型没换成新的：%s" % reloaded.get("error", ""),
                theme.WARN,
            )
        return True

    def test_connection(self):
        data = self.controls()
        cleaned, error = model_config.validate(data)
        if error:
            self.set_status(error, theme.ERROR)
            return False
        key = self.key_var.get().strip()
        if not key and self.editing_id and self.repo:
            row = self.repo.get(self.editing_id)
            if row:
                key = self.secrets.load(row.get("secret_id")) if self.secrets else ""
        if not key and cleaned.get("provider") != "ollama":
            self.set_status("先填 API Key（或先保存一条带 Key 的配置）再测试。", theme.WARN)
            return False
        local = cleaned.get("provider") == "ollama"
        if not messagebox.askyesno(
            "测试连接",
            (
                "会向本机 %s 发送一次最小请求，不联网。继续吗？"
                if local
                else "会向 %s 真实发送一次最小请求，可能产生极少量费用。继续吗？"
            )
            % cleaned["base_url"],
            parent=getattr(self.pet, "win", self.root),
        ):
            return False
        provider = adapter.PROVIDERS.get(cleaned["provider"], adapter.CustomEndpointProvider)(
            base_url=cleaned["base_url"],
            api_key=key,
            model=cleaned["model"],
            timeout=getattr(self.pet.config, "llm_timeout", 60.0),
            proxy=getattr(self.pet.config, "llm_proxy", ""),
            retries=0,
        )
        result = provider.probe()
        self.last_probe[self.editing_id or "__draft__"] = result
        color = theme.OK if result.get("ok") else theme.ERROR
        self.set_status(str(result.get("message") or ""), color)
        self.refresh()
        return bool(result.get("ok"))

    def fetch_models(self):
        """拉一次模型列表。拉不到就直说，让用户手填。"""
        data = self.controls()
        cleaned, error = model_config.validate(data)
        if error:
            self.set_status(error, theme.ERROR)
            return False
        key = self.key_var.get().strip()
        if not key and self.editing_id and self.repo:
            row = self.repo.get(self.editing_id)
            if row and self.secrets:
                key = self.secrets.load(row.get("secret_id"))
        if not key and cleaned.get("provider") != "ollama":
            self.set_status("先填 API Key（或保存一条带 Key 的配置）再拉列表。", theme.WARN)
            return False
        provider = adapter.PROVIDERS.get(cleaned["provider"], adapter.CustomEndpointProvider)(
            base_url=cleaned["base_url"],
            api_key=key,
            model=cleaned["model"],
            timeout=getattr(self.pet.config, "llm_timeout", 60.0),
            proxy=getattr(self.pet.config, "llm_proxy", ""),
            retries=0,
        )
        models = provider.list_models()
        if not models:
            self.set_status("服务商没有返回模型列表，直接手动填模型 ID 即可。", theme.MUTED)
            return False
        self.model_combo.configure(values=models)
        self.set_status("取到 %d 个模型，点下拉选择。" % len(models), theme.OK)
        return True

    def set_default(self, role):
        if not self.editing_id or self.repo is None:
            self.set_status("先保存这条配置，再设成默认。", theme.WARN)
            return False
        result = self.repo.set_default(role, self.editing_id)
        if not result.get("ok"):
            self.set_status(str(result.get("error") or "设置失败"), theme.ERROR)
            return False
        self.pet.reload_models()
        self.refresh()
        self.set_status("已设为%s默认模型。" % model_config.ROLE_LABELS[role], theme.OK)
        return True

    def delete(self):
        if not self.editing_id or self.repo is None:
            self.set_status("先在列表里选一条配置。", theme.WARN)
            return False
        row = self.repo.get(self.editing_id)
        if row is None:
            return False
        if not messagebox.askyesno(
            "删除配置",
            "删除「%s」？当前聊天和任务会回到默认配置。" % row.get("name", ""),
            parent=getattr(self.pet, "win", self.root),
        ):
            return False
        drop_key = messagebox.askyesno(
            "同时删除密钥？",
            "要不要把已保存的 API Key 也删掉？\n选「否」只删配置，密钥留着以后还能用。",
            parent=getattr(self.pet, "win", self.root),
        )
        result = self.repo.delete(self.editing_id)
        if not result.get("ok"):
            self.set_status(str(result.get("error") or "删除失败"), theme.ERROR)
            return False
        if drop_key and self.secrets is not None:
            self.secrets.delete(result.get("secret_id"))
        self.new_config()
        self.pet.reload_models()
        self.refresh()
        self.set_status("配置已删除。" + ("密钥也已删除。" if drop_key else "密钥保留。"), theme.OK)
        return True

    def open_docs(self):
        url = str(self.preset().get("docs") or "")
        if not url:
            return False
        try:
            webbrowser.open(url)
        except Exception as exc:
            self.set_status("打不开浏览器：%s" % exc, theme.ERROR)
            return False
        return True

    # ---------- 列表与状态 ----------
    def set_status(self, text, color=None):
        try:
            self.status_label.configure(text=str(text or ""), fg=color or theme.MUTED)
        except Exception:
            pass
        return text

    def _state_for(self, row):
        if not row.get("enabled", True):
            return "已停用", theme.MUTED
        if row.get("provider") != "ollama" and (
            not self.secrets or not self.secrets.has(row.get("secret_id"))
        ):
            return "未配置 Key", theme.WARN
        probe = self.last_probe.get(str(row.get("id")))
        if probe:
            if probe.get("ok"):
                return "可用", theme.OK
            return errors.label(probe.get("kind")), theme.ERROR
        return "待测试", theme.MUTED

    def refresh(self):
        if self.repo is None:
            return []
        rows = self.repo.list()
        self.tree.delete(*self.tree.get_children())
        for row in rows:
            state, _color = self._state_for(row)
            roles = "、".join(model_config.ROLE_LABELS.get(role, role) for role in row.get("roles") or [])
            self.tree.insert(
                "",
                "end",
                iid=str(row.get("id")),
                values=(
                    row.get("name", ""),
                    model_config.provider_label(row.get("provider")),
                    row.get("model", ""),
                    roles,
                    state,
                ),
            )
        defaults = self.repo.defaults()
        companion = self.repo.get(defaults.get("companion")) if defaults.get("companion") else None
        task = self.repo.get(defaults.get("task")) if defaults.get("task") else None
        self.current_label.configure(
            text="当前聊天模型：%s　　当前任务模型：%s"
            % (
                companion.get("name") if companion else "未配置",
                task.get("name") if task else "未配置",
            )
        )
        if rows:
            self.badge.configure(text="%d 条配置" % len(rows), fg=theme.OK)
        else:
            self.badge.configure(text="未配置", fg=theme.WARN)
        self.refresh_calls()
        return rows

    def refresh_calls(self):
        try:
            rows = self.pet.model_call_rows(limit=30) if hasattr(self.pet, "model_call_rows") else []
            summary = self.pet.model_call_summary() if hasattr(self.pet, "model_call_summary") else {}
        except Exception:
            rows, summary = [], {}
        self.call_tree.delete(*self.call_tree.get_children())
        for row in rows:
            self.call_tree.insert(
                "",
                "end",
                values=(
                    row.get("at"),
                    model_config.ROLE_LABELS.get(row.get("role"), row.get("role")),
                    row.get("model"),
                    "成功" if row.get("status") == "ok" else "失败",
                    "%d ms" % int(row.get("latency_ms") or 0),
                    row.get("total_tokens") or 0,
                    row.get("error") or row.get("error_kind") or "",
                ),
            )
        self.call_summary.configure(
            text="共 %s 次，成功 %s，失败 %s，平均 %s ms，累计 %s tokens。不含提示词和回复正文。"
            % (
                summary.get("calls", 0),
                summary.get("ok", 0),
                summary.get("failed", 0),
                summary.get("avg_latency_ms", 0),
                summary.get("total_tokens", 0),
            )
        )
        return rows

    def clear_calls(self):
        if not messagebox.askyesno(
            "清空调用记录",
            "清空调试记录吗？聊天、记忆和模型配置都不受影响。",
            parent=getattr(self.pet, "win", self.root),
        ):
            return 0
        removed = self.pet.clear_model_calls() if hasattr(self.pet, "clear_model_calls") else 0
        self.refresh_calls()
        self.set_status("已清空 %d 条调用记录。" % removed, theme.OK)
        return removed
