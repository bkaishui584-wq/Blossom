"""本地能力与权限中心。

一张表说清：哪些能力不用模型就能用、哪个要联网、哪个要模型；每个工具要什么
权限；哪些目录被持久授权了；最近执行过什么。界面只读这些状态，只有撤销授权、
授权目录和暂停主动行为会写回设置。
"""

import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from agent import safe_tools
from extensions import packs as packs_kit
from tools.permission import MODE_LABELS, SAFE_LEVELS, normalize_level
from tools.workspace import FILE_TOOL_NAMES
from ui import panel, theme
from voice import stt as stt_kit


class CapabilityWindow:
    """能力与权限中心。数据都从 Agent 上只读获取。"""
    TITLE = "本地能力与权限"

    # 扩展筛选的键与显示名，跟注册表里的分类一一对齐，顺序也要对上。
    EXTENSION_FILTERS = (
        ("", "全部"),
        ("installed", "已安装"),
        ("recommended", "推荐技能"),
        ("review", "待核验"),
        ("needs_setup", "需要配置"),
        ("disabled", "已禁用"),
        ("developer", "开发者专用"),
        ("end_user", "面向最终用户"),
        ("updates", "更新与兼容性"),
        ("optional", "可选扩展"),
        ("builtin", "内置"),
        ("offline", "本地运行"),
        ("network", "需要网络"),
        ("model", "需要模型"),
        ("permission", "需要授权"),
        ("blocked", "不可用"),
    )


    GEOMETRY = (900, 600)

    def __init__(self, pet, parent=None):

        self.pet = pet
        # 嵌进主工作台是一块 Frame，单独打开才是一个窗口。
        self.embedded = parent is not None
        self.win = panel.container(pet, self.TITLE, self.GEOMETRY, parent)
        self._build()
        self.refresh()

    # ---------- 只读数据源 ----------
    def _agent(self):
        return getattr(self.pet, "agent", None)

    def _capabilities(self):
        caps = getattr(self._agent(), "capabilities", None)
        if caps is None or not hasattr(caps, "catalog"):
            return []
        try:
            return caps.catalog()
        except Exception:
            return []

    def _offline_names(self):
        caps = getattr(self._agent(), "capabilities", None)
        if caps is None or not hasattr(caps, "offline_names"):
            return []
        try:
            return caps.offline_names()
        except Exception:
            return []

    def _tools(self):
        registry = getattr(self._agent(), "registry", None)
        if registry is None or not hasattr(registry, "catalog"):
            return []
        try:
            return registry.catalog()
        except Exception:
            return []

    def _grant_store(self):
        permissions = getattr(self._agent(), "permissions", None)
        return getattr(permissions, "grants", None)

    def _grants(self):
        store = self._grant_store()
        if store is None:
            return []
        try:
            return store.all()
        except Exception:
            return []

    def _audit(self, limit=120, kind=None):
        audit = getattr(self._agent(), "audit", None)
        if audit is None:
            return []
        try:
            return audit.recent(limit=limit, kind=kind)
        except Exception:
            return []

    def _extensions(self):
        """扩展注册表。Agent 上没挂就返回 None，界面如实说没有。"""
        return getattr(self._agent(), "extensions", None)

    def _extension_rows(self, flt=""):
        registry = self._extensions()
        if registry is None or not hasattr(registry, "rows"):
            return []
        try:
            return registry.rows(flt)
        except Exception:
            return []

    def _license_rows(self):
        registry = self._extensions()
        if registry is None or not hasattr(registry, "all"):
            return []
        try:
            from extensions import licenses

            return licenses.rows(registry.all())
        except Exception:
            return []

    def _selected_extension(self):
        registry = self._extensions()
        selected = self.ext_tree.selection()
        if registry is None or not selected:
            return None, None
        return registry, selected[0]

    def _skills(self):
        manager = getattr(self._agent(), "skills", None)
        if manager is None:
            return []
        for name in ("developer_rows", "rows"):
            reader = getattr(manager, name, None)
            if callable(reader):
                try:
                    return list(reader())
                except Exception:
                    return []
        return []

    def _note(self, kind, name, decision, detail=""):
        audit = getattr(self._agent(), "audit", None)
        if audit is None:
            return
        try:
            audit.record(kind=kind, name=name, decision=decision, detail=detail)
        except Exception:
            pass

    # ---------- 搭建 ----------
    def _build(self):
        header = theme.card(self.win)
        header.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_MD, theme.PAD_SM))
        head = tk.Frame(header, bg=theme.CARD)
        head.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_SM, theme.PAD_XS))
        theme.label(head, "本地能力与权限", kind="title").pack(side="left")
        self.offline_label = theme.label(head, "", kind="small", fg=theme.MUTED)
        self.offline_label.pack(side="left", padx=theme.PAD_MD)
        theme.button(head, "刷新", self.refresh, kind="primary").pack(side="right")

        bar = tk.Frame(header, bg=theme.CARD)
        bar.pack(fill="x", padx=theme.PAD_MD, pady=(0, theme.PAD_SM))
        self.pause_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            bar,
            text="暂停主动行为",
            variable=self.pause_var,
            command=self.toggle_proactive,
            bg=theme.BG,
            fg=theme.TEXT,
            selectcolor=theme.FIELD,
            activebackground=theme.BG,
            activeforeground=theme.ACCENT,
            font=theme.FONT_BODY,
            anchor="w",
        ).pack(side="left")
        theme.label(
            bar,
            "关掉后桌宠不再主动开口；你自己设的计时和待办提醒照常。",
            kind="small",
            fg=theme.MUTED,
        ).pack(side="left", padx=theme.PAD_SM)

        self.book = ttk.Notebook(self.win)
        self.book.pack(fill="both", expand=True, padx=theme.PAD_MD, pady=theme.PAD_SM)
        for title, builder in (
            ("本地能力", self._build_caps),
            ("工具与权限", self._build_tools),
            ("技能", self._build_skills_tab),
            ("扩展", self._build_extensions),
            ("许可证", self._build_licenses),
            ("授权", self._build_grants),
            ("能力包", self._build_packs),
            ("最近执行", self._build_audit),
        ):
            frame = theme.card(self.book)
            self.book.add(frame, text=title)
            builder(frame)
            theme.cardify(frame)

        self.hint = theme.label(header, "", kind="small", fg=theme.MUTED, anchor="w")
        self.hint.pack(side="bottom", fill="x", padx=theme.PAD_MD)
        panel.close_button(header, self).pack(side="bottom", pady=theme.PAD_XS)
        theme.cardify(header)

    def _tree(self, parent, columns):
        wrap = tk.Frame(parent, bg=theme.CARD)
        wrap.pack(fill="both", expand=True, padx=theme.PAD_SM, pady=theme.PAD_SM)
        tree = ttk.Treeview(
            wrap, columns=[key for key, _, _ in columns], show="headings", height=15
        )
        first = columns[0][0]
        for key, title, width in columns:
            tree.heading(key, text=title)
            tree.column(key, width=width, anchor="w", stretch=(key == first))
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        return tree

    def _build_caps(self, parent):
        self.cap_tree = self._tree(
            parent,
            (("label", "能力", 180), ("requires", "依赖", 110), ("ready", "状态", 80), ("reason", "说明", 420)),
        )
        # 没有模型时，这一行就是手动入口：挑一条本机能力，点一下就跑。
        line = tk.Frame(parent, bg=theme.BG)
        line.pack(fill="x", pady=(theme.PAD_XS, 0))
        theme.label(
            line, "没有模型时，这几条照样能跑：", kind="small", fg=theme.MUTED
        ).pack(side="left")
        names = [row.get("name") for row in self._runnable_tools()]
        self.ability_names = [name for name in names if name]
        self.ability_var = tk.StringVar(value=self.ability_names[0] if self.ability_names else "")
        self.ability_menu = tk.OptionMenu(
            line, self.ability_var, *(self.ability_names or ["（没有可手动跑的能力）"])
        )
        self.ability_menu.configure(
            bg=theme.FIELD,
            fg=theme.TEXT,
            activebackground=theme.FIELD,
            activeforeground=theme.TEXT,
            highlightthickness=0,
            bd=0,
            relief="flat",
            font=theme.FONT_SMALL,
            state="normal" if self.ability_names else "disabled",
        )
        self.ability_menu["menu"].configure(bg=theme.FIELD, fg=theme.TEXT)
        self.ability_menu.pack(side="left", padx=theme.PAD_XS)
        theme.button(line, "跑一下", self.run_ability, kind="primary").pack(side="left")
        self.ability_result = theme.label(
            parent, "", kind="small", fg=theme.MUTED, anchor="w", justify="left"
        )
        self.ability_result.pack(fill="x", pady=(2, 0))

    def _runnable_tools(self):
        """无模型时能手动跑的：只读、安全等级、不要参数。

        不要求工具自带"说人话"函数：没有的话就把真实结果原样贴出来。
        跑的时候仍然走 ToolExecutor，权限与审计一样生效。
        """
        registry = getattr(self._agent(), "registry", None)
        if registry is None:
            return []
        rows = []
        for row in self._tools():
            name = str(row.get("name") or "")
            if not row.get("readonly") or not name:
                continue
            if normalize_level(row.get("permission")) not in SAFE_LEVELS:
                continue
            tool = registry.get(name)
            if tool is None:
                continue
            if not self._askable(tool):
                continue
            rows.append(row)
        return rows

    def _askable(self, tool):
        """要不要额外问一句。只认"就一个字符串参数"的那种：

        搜文件问"搜什么"，处理文本问"哪段文本"——多问一句就能跑。
        参数更多、更复杂的，等有模型再说，不在这一层硬凑表单。
        """
        schema = getattr(tool, "schema", None) or {}
        required = list(schema.get("required") or [])
        if not required:
            return True
        if len(required) != 1:
            return False
        spec = (schema.get("properties") or {}).get(required[0]) or {}
        return str(spec.get("type") or "") == "string"

    def _ask_arguments(self, tool):
        """问那一个参数。用户取消就返回 None，这次就不跑。"""
        required = list((getattr(tool, "schema", None) or {}).get("required") or [])
        if not required:
            return {}
        key = required[0]
        spec = ((tool.schema or {}).get("properties") or {}).get(key) or {}
        prompt = str(spec.get("description") or key)
        try:
            value = simpledialog.askstring("要填一个参数", prompt, parent=self.win)
        except Exception as exc:
            self._show_ability("没法问参数：%s" % exc)
            return None
        text = str(value or "").strip()
        return {key: text} if text else None

    def run_ability(self, name=None, arguments=None):
        """手动跑一条本机能力。没模型也能用，但照样过权限和审计。"""
        executor = getattr(self._agent(), "executor", None)
        wanted = str(name or self.ability_var.get() or "").strip()
        if executor is None:
            return self._show_ability("现在没有接入工具执行层，跑不了。")
        if not wanted or wanted not in self.ability_names:
            return self._show_ability("先挑一条能跑的能力。")
        registry = getattr(self._agent(), "registry", None)
        tool = registry.get(wanted) if registry is not None else None
        if arguments is None:
            if tool is None:
                return self._show_ability("这条能力已经不在了。")
            arguments = self._ask_arguments(tool)
            if arguments is None:
                return self._show_ability("没填参数，这次没跑。")
        try:
            outcome = executor.execute(wanted, arguments)
        except Exception as exc:
            return self._show_ability("%s 没跑成：%s" % (wanted, exc))
        return self._show_ability(self._ability_text(outcome))

    def _ability_text(self, outcome):
        if not getattr(outcome, "ok", False):
            return "%s：%s" % (outcome.name, outcome.error or "没做成")
        tool = getattr(self._agent(), "registry", None)
        tool = tool.get(outcome.name) if tool is not None else None
        text = safe_tools.note(tool, outcome) if tool is not None else ""
        return "%s → %s" % (outcome.name, text or outcome.result)

    def _show_ability(self, text):
        try:
            self.ability_result.configure(text=str(text or ""))
        except Exception:
            pass
        return text

    def _build_tools(self, parent):
        self.tool_tree = self._tree(
            parent,
            (
                ("name", "工具", 180),
                ("level", "权限", 110),
                ("requires", "依赖", 90),
                ("readonly", "只读", 60),
                ("about", "说明", 380),
            ),
        )

    def _build_packs(self, parent):
        """能力包：文档解析、离线语音这类大件，按需装，不装不影响别的功能。"""
        theme.label(
            parent,
            "基础包不带文档解析和离线语音（合计一百多 MB）。要用时在这里装："
            "选择发行包里的 docs / voice zip 即可。识别模型权重不在包里，"
            "也不会自动下载；装完运行库后还要自己选模型目录。",
            kind="small",
            fg=theme.MUTED,
            anchor="w",
            justify="left",
            wraplength=760,
        ).pack(fill="x", pady=(0, theme.PAD_XS))
        self.pack_tree = self._tree(
            parent,
            (("name", "能力包", 140), ("title", "名称", 120), ("version", "版本", 80),
             ("provides", "包含", 380)),
        )
        bar = tk.Frame(parent, bg=theme.BG)
        bar.pack(fill="x", pady=(theme.PAD_XS, 0))
        theme.button(bar, "从 zip 安装…", self.install_pack, kind="primary").pack(side="left")
        theme.button(bar, "卸载选中", self.remove_pack, kind="danger").pack(
            side="left", padx=theme.PAD_SM
        )
        theme.button(bar, "检查语音模型", self.check_voice_model).pack(
            side="left", padx=theme.PAD_SM
        )
        theme.label(bar, "装在数据目录的 packs/ 下，卸载只删这个包。", kind="small", fg=theme.MUTED).pack(
            side="left", padx=theme.PAD_SM
        )
        self.voice_model_state_label = theme.label(
            parent,
            "",
            kind="small",
            fg=theme.MUTED,
            anchor="w",
            justify="left",
            wraplength=760,
        )
        self.voice_model_state_label.pack(fill="x", pady=(theme.PAD_XS, 0))

    def _packs(self):
        return packs_kit.installed(getattr(self.pet, "config", None))

    def _refresh_voice_model_state(self):
        label = getattr(self, "voice_model_state_label", None)
        config = getattr(self.pet, "config", None)
        path = str(getattr(config, "stt_model", "") or "")
        kind = str(getattr(config, "stt_kind", "auto") or "auto")
        state = stt_kit.streaming_model_status(path, kind=kind)
        names = {row.get("name") for row in self._packs()}
        if "voice" in names:
            pack_state = "已安装"
        elif state.get("runtime_ready"):
            pack_state = "运行库已装（不是从本页装的）"
        else:
            pack_state = "未安装"
        translation = ""
        if callable(getattr(self.pet, "subtitle_translation_note", None)):
            try:
                translation = " ｜ 字幕翻译：%s" % self.pet.subtitle_translation_note()
            except Exception:
                translation = ""
        text = "离线语音能力包：%s ｜ 识别模型：%s%s" % (
            pack_state,
            state.get("note") or "状态未知",
            translation,
        )
        if label is not None:
            label.configure(text=text)
        return state

    def check_voice_model(self):
        state = self._refresh_voice_model_state()
        self._hint("离线语音已就绪。" if state.get("ready") else state.get("note") or "识别状态未知")
        return state

    def _selected_pack(self):
        tree = getattr(self, "pack_tree", None)
        if tree is None:
            return ""
        try:
            picked = tree.selection()
        except Exception:
            return ""
        if not picked:
            return ""
        return str(tree.item(picked[0], "values")[0] or "")

    def install_pack(self):
        """从 zip 装一个能力包。装的是本地文件，不联网、不自己下载。"""
        path = filedialog.askopenfilename(
            title="选择能力包", filetypes=[("能力包", "*.zip"), ("所有文件", "*.*")]
        )
        if not path:
            return None
        result = packs_kit.install(getattr(self.pet, "config", None), path)
        if result.get("ok"):
            self.hint.configure(text="已装能力包：%s" % result.get("title") or result.get("name"))
        else:
            messagebox.showerror("这个包装不上", str(result.get("error") or "原因不明"))
        self.refresh()
        return result

    def remove_pack(self):
        name = self._selected_pack()
        if not name:
            self.hint.configure(text="先在上面的列表里选一个能力包。")
            return None
        if not messagebox.askyesno("卸载能力包", "确定卸载 %s 吗？\n只会删这个包，用户数据不动。" % name):
            return None
        result = packs_kit.remove(getattr(self.pet, "config", None), name)
        self.hint.configure(
            text="已卸载 %s" % name if result.get("ok") else "卸载失败：%s" % result.get("error")
        )
        self.refresh()
        return result

    def _build_grants(self, parent):
        self.grant_tree = self._tree(
            parent,
            (
                ("tool", "工具", 170),
                ("scope", "范围", 330),
                ("mode", "类型", 110),
                ("note", "备注", 200),
            ),
        )
        row = tk.Frame(parent, bg=theme.BG)
        row.pack(side="bottom", fill="x", pady=theme.PAD_SM)
        theme.button(row, "授权目录…", self.authorize_directory, kind="primary").pack(side="left")
        theme.button(row, "撤销选中", self.revoke_selected).pack(side="left", padx=theme.PAD_SM)
        theme.label(
            row,
            "授权只对文件工具、且只在你选中的目录内生效。",
            kind="small",
            fg=theme.MUTED,
        ).pack(side="left")

    def _build_skills_tab(self, parent):
        self.skill_tree = self._tree(
            parent,
            (
                ("name", "技能", 170),
                ("version", "版本", 70),
                ("permission", "权限", 110),
                ("tools", "用到的工具", 260),
                ("enabled", "状态", 80),
                ("blocked", "加载问题", 200),
            ),
        )

    def _build_extensions(self, parent):
        bar = tk.Frame(parent, bg=theme.BG)
        bar.pack(side="top", fill="x", pady=(0, theme.PAD_SM))
        theme.label(bar, "筛选", kind="small", fg=theme.MUTED).pack(side="left")
        self.ext_filter = ttk.Combobox(bar, state="readonly", width=14, values=["全部"])
        self.ext_filter.pack(side="left", padx=theme.PAD_SM)
        self.ext_filter.bind("<<ComboboxSelected>>", lambda event: self.refresh())
        self.ext_summary = theme.label(bar, "", kind="small", fg=theme.MUTED)
        self.ext_summary.pack(side="left", padx=theme.PAD_SM)
        self.ext_tree = self._tree(
            parent,
            (
                ("name", "扩展", 190),
                ("verdict", "核验结论", 90),
                ("audience", "受众", 100),
                ("kind", "类型", 80),
                ("source", "来源", 80),
                ("license", "许可证", 150),
                ("health", "环境", 100),
                ("state", "状态", 90),
                ("pinned", "锁定版本", 120),
            ),
        )
        row = tk.Frame(parent, bg=theme.BG)
        row.pack(side="bottom", fill="x", pady=theme.PAD_SM)
        theme.button(row, "启用／停用", self.toggle_extension, kind="primary").pack(side="left")
        theme.button(row, "登记已安装", self.mark_extension_installed).pack(side="left", padx=theme.PAD_SM)
        theme.button(row, "卸载", self.uninstall_extension).pack(side="left")
        theme.button(row, "重新检查", self.check_extension).pack(side="left", padx=theme.PAD_SM)
        theme.button(row, "查看说明", self.show_extension_note).pack(side="left")
        theme.button(row, "检查更新", self.check_updates).pack(side="left", padx=theme.PAD_SM)
        theme.button(row, "生成许可证清单", self.export_notice).pack(side="left")
        theme.label(
            row,
            "没装的扩展不会被启用；带脚本的扩展要单独确认。",
            kind="small",
            fg=theme.MUTED,
        ).pack(side="left", padx=theme.PAD_SM)

    def _build_licenses(self, parent):
        self.license_tree = self._tree(
            parent,
            (
                ("name", "组件", 200),
                ("pinned", "锁定版本", 100),
                ("license", "许可证", 170),
                ("status", "审阅结论", 140),
                ("bundle", "分发结论", 220),
            ),
        )

    def _build_audit(self, parent):
        self.audit_tree = self._tree(
            parent,
            (
                ("time", "时间", 140),
                ("kind", "类别", 110),
                ("name", "对象", 150),
                ("decision", "结论", 90),
                ("detail", "摘要", 340),
            ),
        )

    # ---------- 刷新 ----------
    def refresh(self):
        names = self._offline_names()
        self.offline_label.configure(
            text="断网、没模型也能用：" + ("、".join(names) if names else "暂无")
        )
        try:
            raw = str(self.pet.memory.get_preference("proactive_enabled") or "").strip().lower()
        except Exception:
            raw = ""
        self.pause_var.set(raw in {"0", "false", "no", "off"})
        self._fill(
            self.cap_tree,
            [
                (row.get("label"), row.get("requires_label"), "可用" if row.get("ready") else "不可用", row.get("reason"))
                for row in self._capabilities()
            ],
        )
        self._fill(
            self.tool_tree,
            [
                (
                    tool.get("name"),
                    tool.get("permission"),
                    tool.get("requires", "none"),
                    "是" if tool.get("readonly") else "",
                    tool.get("description", ""),
                )
                for tool in self._tools()
            ],
        )
        self._fill(
            self.grant_tree,
            [
                (
                    grant.get("tool"),
                    grant.get("scope") or "该工具整体",
                    MODE_LABELS.get(str(grant.get("mode")), grant.get("mode")),
                    grant.get("note", ""),
                )
                for grant in self._grants()
            ],
        )
        self._fill(
            self.audit_tree,
            [
                (
                    str(row.get("created_at")),
                    str(row.get("kind")),
                    str(row.get("name")),
                    str(row.get("decision")),
                    str(row.get("detail") or "")[:120],
                )
                for row in self._audit()
            ],
        )
        try:
            self._fill_extension_filter()
        except Exception:
            pass
        summary = {}
        registry = self._extensions()
        if registry is not None and hasattr(registry, "summary"):
            try:
                summary = registry.summary() or {}
            except Exception:
                summary = {}
        self.ext_summary.configure(
            text="共 %s 项，已启用 %s 项，可用 %s 项，核验待定 %s 项，许可证待审 %s 项，不可分发 %s 项"
            % (
                summary.get("total", 0),
                summary.get("enabled", 0),
                summary.get("use_now", 0),
                summary.get("review", 0),
                summary.get("need_review", 0),
                summary.get("blocked", 0),
            )
            if summary
            else "扩展注册表没挂上，暂时读不到。"
        )
        if getattr(self, "ext_tree", None) is not None:
            self._fill(
                self.ext_tree,
                [
                    (
                        row.get("name"),
                        row.get("verdict_label") or "未评估",
                        row.get("audience_label") or "",
                        row.get("kind_label"),
                        row.get("source_label"),
                        row.get("license") or "未声明",
                        row.get("health_label"),
                        "启用" if row.get("enabled") else ("可启用" if row.get("can_enable") else "不可用"),
                        row.get("pinned") or "",
                    )
                    for row in self._extension_rows(self._extension_filter_key())
                ],
            )
        if getattr(self, "license_tree", None) is not None:
            self._fill(
                self.license_tree,
                [
                    (
                        row.get("name"),
                        row.get("pinned") or "不适用",
                        row.get("license"),
                        row.get("license_label"),
                        row.get("bundle_blocked") or ("已随包内置" if row.get("bundled") else "可评估"),
                    )
                    for row in self._license_rows()
                ],
            )
        self._fill(
            self.skill_tree,
            [
                (
                    row.get("name"),
                    row.get("version"),
                    row.get("permission"),
                    row.get("tools"),
                    "启用" if row.get("enabled") else "停用",
                    row.get("blocked") or "",
                )
                for row in self._skills()
            ],
        )
        self._fill(
            getattr(self, "pack_tree", None),
            [
                (
                    row.get("name"),
                    row.get("title"),
                    row.get("version"),
                    "、".join(row.get("provides") or []) or "（清单里没写）",
                )
                for row in self._packs()
            ],
        )
        self._refresh_voice_model_state()

    def _fill(self, tree, rows):
        if tree is None:
            return
        if not hasattr(self, "_trees_ready"):
            self._trees_ready = set()
        tree.delete(*tree.get_children())
        for row in rows:
            tree.insert("", "end", values=tuple("" if value is None else value for value in row))

    # ---------- 动作 ----------
    def toggle_proactive(self):
        value = "0" if self.pause_var.get() else "1"
        try:
            self.pet.memory.set_preference("proactive_enabled", value)
        except Exception as exc:
            self._hint("改不了：%s" % exc)
            return
        self._note("preference", "proactive_enabled", value, "用户在能力中心修改")
        self._hint("已暂停主动行为。" if value == "0" else "已恢复主动行为。")

    def revoke_selected(self):
        store = self._grant_store()
        if store is None:
            self._hint("授权表读不到。")
            return
        selected = self.grant_tree.selection()
        if not selected:
            self._hint("先选一条授权。")
            return
        values = self.grant_tree.item(selected[0], "values")
        tool = values[0]
        scope = values[1]
        scope = "" if scope == "该工具整体" else scope
        try:
            removed = store.revoke(tool, scope or None)
        except Exception as exc:
            self._hint("撤销不了：%s" % exc)
            return
        self._note("grant_revoked", tool, "revoke", scope or "该工具整体")
        self._hint("撤销了 %d 条。" % removed)
        self.refresh()

    def authorize_directory(self):
        store = self._grant_store()
        if store is None:
            self._hint("授权表读不到。")
            return
        directory = filedialog.askdirectory(parent=self.win, title="选择要授权的目录")
        if not directory:
            return
        scope = "path:" + str(directory)
        for tool in FILE_TOOL_NAMES:
            try:
                store.set(tool, "always", scope=scope, note="用户在能力中心授权")
            except Exception as exc:
                self._hint("授权失败：%s" % exc)
                return
        self._note("grant_added", ",".join(FILE_TOOL_NAMES), "allow", scope)
        self._hint("已授权目录：%s" % directory)
        self.refresh()

    # ---------- 扩展动作 ----------
    def _extension_filter_key(self):
        box = getattr(self, "ext_filter", None)
        if box is None:
            return ""
        chosen = str(box.get() or "").strip()
        for key, label in self.EXTENSION_FILTERS:
            if label == chosen:
                return key
        return ""

    def _fill_extension_filter(self):
        box = getattr(self, "ext_filter", None)
        if box is None:
            return
        labels = [label for _, label in self.EXTENSION_FILTERS]
        if list(box.cget("values")) != labels:
            box.configure(values=labels)
        if not str(box.get() or "").strip():
            box.set(labels[0])

    def toggle_extension(self):
        registry, ext_id = self._selected_extension()
        if registry is None or ext_id is None:
            self._hint("先选一个扩展。")
            return
        if registry.is_enabled(ext_id):
            ok, reason = registry.disable(ext_id)
        else:
            ok, reason = registry.enable(ext_id)
        self._note("extension", ext_id, "enable" if ok else "refuse", reason)
        self._hint(("%s：%s" % (ext_id, reason)) if not ok else ("%s %s" % (ext_id, reason)))
        self.refresh()

    def mark_extension_installed(self):
        registry, ext_id = self._selected_extension()
        if registry is None or ext_id is None:
            self._hint("先选一个扩展。")
            return
        ok, reason = registry.install(ext_id)
        self._note("extension", ext_id, "install" if ok else "refuse", reason)
        self._hint("%s：%s" % (ext_id, reason))
        self.refresh()

    def uninstall_extension(self):
        registry, ext_id = self._selected_extension()
        if registry is None or ext_id is None:
            self._hint("先选一个扩展。")
            return
        manifest = registry.get(ext_id)
        name = getattr(manifest, "name", ext_id)
        if not messagebox.askyesno("卸载扩展", "要卸载「%s」吗？只清状态，不动你自己放的文件。" % name, parent=self.win):
            return
        ok, reason = registry.uninstall(ext_id)
        self._note("extension", ext_id, "uninstall" if ok else "refuse", reason)
        self._hint("%s：%s" % (ext_id, reason))
        self.refresh()

    def check_updates(self):
        """问一遍市场里有没有新版本。只查不装，网络在后台线程里跑。"""
        registry = self._extensions()
        if registry is None or not hasattr(registry, "all"):
            self._hint("扩展注册表没挂上。")
            return False
        if getattr(self, "_updates_busy", False):
            self._hint("上一轮还在查，稍等一下。")
            return False
        self._updates_busy = True
        self._hint("正在查新版本…")
        manifests = list(registry.all())

        def work():
            try:
                from extensions import qoder as qoder_module
                from extensions import updates as updates_module

                result = updates_module.check(qoder_module, manifests)
            except Exception as exc:
                result = {"error": str(exc)}
            self._later(lambda: self._updates_done(result))

        threading.Thread(target=work, daemon=True).start()
        return True

    def _updates_done(self, result):
        self._updates_busy = False
        if result.get("error"):
            self._hint("没查成：%s" % result["error"])
            return None
        from extensions import updates as updates_module

        lines = [updates_module.summary_text(result)]
        for item in result.get("pending") or []:
            lines.append("  - %s：%s → %s" % (item.get("name"), item.get("local"), item.get("remote")))
        for item in result.get("errors") or []:
            lines.append("  - %s：没查成（%s）" % (item.get("id"), item.get("reason")))
        lines.append("")
        lines.append("只报版本差异，不会自动下载或替换任何东西。")
        messagebox.showinfo("更新检查", "\n".join(lines), parent=self.win)
        self._note("extension", "updates", "check", updates_module.summary_text(result))
        self._hint(updates_module.summary_text(result))
        return result

    def _later(self, fn):
        try:
            self.pet.root.after(0, fn)
        except Exception:
            pass

    def show_extension_note(self):
        """把这一项的来源、核验结论和备注摊开给用户看。只读。"""
        registry, ext_id = self._selected_extension()
        if registry is None or ext_id is None:
            self._hint("先选一个扩展。")
            return
        manifest = registry.get(ext_id)
        if manifest is None:
            self._hint("找不到这一项。")
            return
        lines = [
            "%s（%s）" % (manifest.name, manifest.id),
            "类型：%s　来源：%s　受众：%s" % (
                manifest.kind, manifest.source, getattr(manifest, "audience", "") or "user",
            ),
            "核验结论：%s" % (getattr(manifest, "verdict", "") or "未评估"),
            "锁定版本：%s" % (manifest.pinned or "未锁定"),
            "来源地址：%s" % (manifest.homepage or "无"),
            "许可证：%s" % (manifest.license or "未声明"),
            "",
            manifest.summary or "",
        ]
        note = getattr(manifest, "verdict_note", "")
        if note:
            lines.extend(["", "结论说明：%s" % note])
        if manifest.notes:
            lines.extend(["", "备注："])
            lines.extend("  - %s" % item for item in manifest.notes)
        if manifest.permissions:
            lines.extend(["", "需要权限：%s" % "、".join(manifest.permissions)])
        if manifest.dependencies:
            lines.extend(["依赖："])
            lines.extend("  - %s" % item for item in manifest.dependencies)
        if manifest.scripts:
            lines.extend(["", "包含脚本（只登记，不执行）："])
            lines.extend("  - %s" % item for item in manifest.scripts)
        messagebox.showinfo("扩展说明", "\n".join(lines), parent=self.win)
        self._note("extension", ext_id, "inspect", getattr(manifest, "verdict", "") or "未评估")
        return manifest

    def check_extension(self):
        registry, ext_id = self._selected_extension()
        if registry is None or ext_id is None:
            self._hint("先选一个扩展。")
            return
        result = registry.health(ext_id)
        self._note("extension", ext_id, "check", str(result.get("detail") or ""))
        self._hint("%s：%s" % (ext_id, result.get("detail") or "没有更多信息"))
        self.refresh()

    def export_notice(self):
        registry = self._extensions()
        if registry is None:
            self._hint("扩展注册表没挂上。")
            return
        try:
            from extensions import licenses
            from security.config import data_root
            from pathlib import Path

            target = Path(data_root()) / "licenses"
            target.mkdir(parents=True, exist_ok=True)
            path = target / "NOTICE.md"
            path.write_text(licenses.notice_text(registry.all()), encoding="utf-8")
        except Exception as exc:
            self._hint("清单写不出来：%s" % exc)
            return
        self._note("license", "NOTICE.md", "export", str(path))
        self._hint("许可证清单写到：%s" % path)

    def _hint(self, text):
        try:
            self.hint.configure(text=text)
        except Exception:
            pass
        return text
