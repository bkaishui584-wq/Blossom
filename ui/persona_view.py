"""人格面板：调性格强度、看反馈、撤销反馈、导出样本，以及只读的状态分区。

人格只影响「怎么说」，不能绕过权限、工具和安全规则。
训练样本只是收集，不做微调，也不自动改人格。
"""

import json
import tkinter as tk
from tkinter import messagebox, ttk

from persona import history as persona_history
from persona import traits as traits_module
from ui import panel, theme
from ui.persona_workbench import WorkbenchTabs

# 面板上重点展示的几种基础性格；其余已知性格也会列出来。
PRIMARY_TRAITS = ("温柔", "理性", "活泼", "安静", "幽默", "好奇", "独立")

FEEDBACK_COLUMNS = (
    ("id", "编号", 46),
    ("content", "内容", 340),
    ("created_at", "记下的时间", 130),
)


def trait_names():
    """面板要显示的性格：重点的几个排前面，其余已知性格照列，不重复。"""
    names = [name for name in PRIMARY_TRAITS if name in traits_module.TRAITS]
    names += [name for name in traits_module.TRAITS if name not in names]
    return names


def mode_lines(behavior):
    """行为规则与模式：只读，全部来自程序判断，不是模型编的。"""
    if behavior is None:
        return ["行为层没有接入。"]
    try:
        data = behavior.describe() or {}
    except Exception as exc:
        return [f"读取行为状态失败：{exc}"]
    lines = ["静默模式：" + ("开" if data.get("quiet") else "关")]
    hours = list(data.get("quiet_hours") or [])
    if len(hours) == 2:
        lines.append(f"免打扰时段：{hours[0]}-{hours[1]} 点")
    lines.append("主动开口：" + ("允许" if data.get("proactive_allowed") else "已关闭"))
    if data.get("sleeping"):
        lines.append("现在：睡着了（很久没互动）")
    elif data.get("user_away"):
        lines.append("现在：用户不在电脑前")
    else:
        lines.append("现在：醒着")
    if data.get("window_busy"):
        lines.append("专注模式：用户在全屏，不打扰")
    return lines


class PersonaWindow(WorkbenchTabs):
    """人格面板。所有动作都通过宿主的 agent / memory 完成。"""
    TITLE = "人格"


    GEOMETRY = (880, 600)

    def __init__(self, pet, parent=None):
        self.pet = pet
        self.memory = getattr(pet, "memory", None)
        self.vars = {}
        self.sliders = {}
        # 嵌进主工作台是一块 Frame，单独打开才是一个窗口。
        self.embedded = parent is not None
        self.win = panel.container(pet, self.TITLE, self.GEOMETRY, parent)
        self._build()
        self.refresh()
        self.reload_text_tab()

    # ---------- 搭建 ----------
    def _build(self):
        header = theme.card(self.win)
        header.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_MD, theme.PAD_SM))
        head = tk.Frame(header, bg=theme.CARD)
        head.pack(fill="x", padx=theme.PAD_MD, pady=theme.PAD_SM)
        theme.label(head, "人格", kind="title").pack(side="left")
        self.stats_label = theme.label(head, "", kind="small", fg=theme.MUTED)
        self.stats_label.pack(side="left", padx=theme.PAD_MD)

        self._trait_frame = None
        self._setup_notebook()
        for title, builder in (
            ("人格文本", self._text_tab),
            ("版本", self._version_tab),
            ("习惯", self._habit_tab),
            ("学习", self._learn_tab),
            ("情境", self._context_tab),
            ("测试台", self._lab_tab),
            ("性格与状态", self._trait_tab),
        ):
            page = theme.card(self.book)
            self.book.add(page, text=title)
            try:
                builder(page)
                theme.cardify(page)
            except Exception as exc:  # 一页出错不能拖垮整个面板
                theme.label(
                    page,
                    "这一页暂时不可用：%s" % exc,
                    kind="small",
                    fg=theme.ERROR,
                ).pack(anchor="w", padx=theme.PAD_MD, pady=theme.PAD_MD)

        body = tk.Frame(self._trait_frame, bg=theme.BG)
        body.pack(fill="both", expand=True, padx=theme.PAD_MD, pady=theme.PAD_SM)
        left = theme.card(body)
        left.pack(side="left", fill="y", padx=(0, theme.PAD_SM))
        right = theme.card(body)
        right.pack(side="left", fill="both", expand=True)

        # 左：性格强度
        theme.label(left, "性格强度", kind="body").pack(anchor="w")
        for name in trait_names():
            line = tk.Frame(left, bg=theme.BG)
            line.pack(fill="x")
            theme.label(
                line, name, kind="small", fg=theme.MUTED, width=5, anchor="w"
            ).pack(side="left")
            var = tk.IntVar(value=traits_module.DEFAULT_INTENSITY)
            value_label = theme.label(
                line, str(traits_module.DEFAULT_INTENSITY), kind="small", width=3
            )
            value_label.pack(side="right")
            scale = tk.Scale(
                line,
                from_=0,
                to=100,
                orient="horizontal",
                resolution=5,
                showvalue=0,
                length=170,
                variable=var,
                bg=theme.BG,
                fg=theme.TEXT,
                troughcolor=theme.FIELD,
                activebackground=theme.ACCENT,
                highlightthickness=0,
                bd=0,
                font=theme.FONT_SMALL,
            )
            scale.pack(side="right")
            var.trace_add("write", lambda *a, label=value_label, v=var: label.config(text=str(v.get())))
            self.vars[name] = var
            self.sliders[name] = scale
        trait_buttons = tk.Frame(left, bg=theme.BG)
        trait_buttons.pack(fill="x", pady=theme.PAD_SM)
        theme.button(trait_buttons, "保存", self.save_traits, kind="primary").pack(side="left")
        theme.button(trait_buttons, "恢复默认", self.reset_traits).pack(
            side="left", padx=theme.PAD_SM
        )
        self.conflict_label = theme.label(
            left, "", kind="small", fg=theme.WARN, wraplength=250, justify="left"
        )
        self.conflict_label.pack(anchor="w", pady=(theme.PAD_XS, 0))
        theme.label(
            left,
            "只影响表达方式。安全规则和权限不会因为性格改变。",
            kind="small",
            fg=theme.MUTED,
            wraplength=250,
            justify="left",
        ).pack(anchor="w", pady=(theme.PAD_XS, 0))

        theme.button(
            left, "撤销上一次改动", self.undo_traits, kind="danger"
        ).pack(anchor="w", pady=(theme.PAD_SM, 0))

        # 右：状态分区（人格设定 / 即时情绪 / 关系状态分开写，不混成一段）
        theme.label(right, "状态", kind="body").pack(anchor="w")
        self.persona_label = theme.label(right, "", kind="small", wraplength=430, justify="left")
        self.persona_label.pack(anchor="w")
        self.emotion_label = theme.label(right, "", kind="small", fg=theme.MUTED)
        self.emotion_label.pack(anchor="w")
        self.relation_label = theme.label(right, "", kind="small", fg=theme.MUTED)
        self.relation_label.pack(anchor="w")
        self.version_label = theme.label(right, "", kind="small", fg=theme.MUTED)
        self.version_label.pack(anchor="w")
        relation_buttons = tk.Frame(right, bg=theme.BG)
        relation_buttons.pack(anchor="w", pady=(theme.PAD_XS, theme.PAD_SM))
        theme.button(relation_buttons, "重置关系记录", self.reset_relationship, kind="danger").pack(
            side="left"
        )
        theme.label(
            relation_buttons, "只清天数与次数，聊天和记忆都留着。", kind="small", fg=theme.MUTED
        ).pack(side="left", padx=theme.PAD_SM)

        theme.label(right, "行为规则与模式", kind="body").pack(anchor="w", pady=(theme.PAD_SM, 0))
        self.mode_label = theme.label(right, "", kind="small", fg=theme.MUTED, justify="left")
        self.mode_label.pack(anchor="w")

        # 右：人格反馈
        theme.label(right, "人格反馈", kind="body").pack(anchor="w", pady=(theme.PAD_MD, 0))
        table = tk.Frame(right, bg=theme.BG)
        table.pack(fill="both", expand=True)
        columns = tuple(key for key, _, _ in FEEDBACK_COLUMNS)
        self.tree = ttk.Treeview(table, columns=columns, show="headings", height=5)
        for key, title, width in FEEDBACK_COLUMNS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="w", stretch=(key == "content"))
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        feedback_buttons = tk.Frame(right, bg=theme.BG)
        feedback_buttons.pack(fill="x", pady=theme.PAD_SM)
        theme.button(feedback_buttons, "撤销反馈", self.undo_feedback, kind="danger").pack(side="left")
        theme.button(
            feedback_buttons, "导出训练样本", self.export_samples, kind="primary"
        ).pack(side="left", padx=theme.PAD_SM)
        theme.label(
            right,
            "撤销只撤下这条不再生效，内容还在记忆里。导出是给以后训练用的样本，现在不做训练。",
            kind="small",
            fg=theme.MUTED,
            wraplength=430,
            justify="left",
        ).pack(anchor="w")
        theme.cardify(left)
        theme.cardify(right)

        self.hint = theme.label(self.win, "", kind="small", fg=theme.MUTED, anchor="w")
        # 先占住底部：分页内容再高也挤不掉关闭按钮和提示行。
        panel.close_button(self.win, self).pack(
            side="bottom", pady=theme.PAD_SM
        )
        self.hint.pack(side="bottom", fill="x", padx=theme.PAD_MD, pady=(theme.PAD_SM, 0))
        self.book.pack(fill="both", expand=True, padx=theme.PAD_SM, pady=(theme.PAD_SM, 0))
        theme.cardify(header)
        theme.cardify(self.book)

    def _setup_notebook(self):
        """面板分页：人格文本、版本、习惯、测试台与性格状态。"""
        style = ttk.Style(self.win)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TNotebook", background=theme.BG, borderwidth=0)
        style.configure(
            "TNotebook.Tab",
            background=theme.PANEL,
            foreground=theme.MUTED,
            padding=(12, 6),
            font=theme.FONT_BODY,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", theme.FIELD)],
            foreground=[("selected", theme.TEXT)],
        )
        style.configure("TFrame", background=theme.BG)
        # 表格跟面板一个底色。不配的话 ttk 会用系统白底，在深色面板里很跳。
        style.configure(
            "Treeview",
            background=theme.FIELD,
            fieldbackground=theme.FIELD,
            foreground=theme.TEXT,
            borderwidth=0,
        )
        style.map("Treeview", background=[("selected", theme.SELECT_BG)])
        style.configure(
            "Treeview.Heading",
            background=theme.PANEL,
            foreground=theme.MUTED,
            borderwidth=0,
        )
        self.book = ttk.Notebook(self.win)
        return self.book

    def _trait_tab(self, frame):
        """性格强度、状态与反馈就放这一页，内容和以前一样。"""
        self._trait_frame = frame

    # ---------- 数据 ----------
    def current_traits(self):
        """滑杆上现在的强度。"""
        return {name: int(var.get()) for name, var in self.vars.items()}

    def refresh(self):
        """重新读一遍人格、反馈和状态。读不到就显示提示，不弹错误。"""
        self._load_traits()
        rows = self._load_feedback()
        self.refresh_status()
        self._hint(f"反馈 {len(rows)} 条。选中一条可以撤销。")
        return rows

    def _load_traits(self):
        traits = {}
        persona = getattr(self.pet.agent, "persona", None)
        if persona is not None:
            try:
                traits = (persona.get_profile() or {}).get("traits") or {}
            except Exception:
                traits = {}
        if not traits:
            traits = traits_module.load(self.memory) or traits_module.parse(
                getattr(self.pet.config, "persona_traits", "")
            )
        for name, var in self.vars.items():
            try:
                var.set(int(traits.get(name, traits_module.DEFAULT_INTENSITY)))
            except (TypeError, ValueError):
                var.set(traits_module.DEFAULT_INTENSITY)

    def _load_feedback(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        if self.memory is None:
            return []
        try:
            rows = self.memory.browse(kind="feedback", limit=200)
        except Exception as exc:
            self._hint(f"读取反馈失败：{exc}")
            return []
        for row in rows:
            self.tree.insert(
                "",
                "end",
                iid=str(row["id"]),
                values=(
                    row["id"],
                    str(row.get("content") or "").replace("\n", " ")[:200],
                    row.get("created_at") or "",
                ),
            )
        return rows

    def refresh_status(self):
        """把人格设定、即时情绪、关系状态、行为模式分开显示。"""
        persona = getattr(self.pet.agent, "persona", None)
        profile = {}
        if persona is not None:
            try:
                profile = persona.get_profile() or {}
            except Exception:
                profile = {}
        name = str(profile.get("name") or "").strip() or "未命名"
        traits = profile.get("traits") or self.current_traits()
        shown = "、".join(
            f"{key} {value}" for key, value in sorted(traits.items(), key=lambda item: -item[1])
        )
        self.persona_label.config(text=f"人格设定：{name}（{shown or '全部关掉'}）")

        emotion = getattr(self.pet.agent, "emotion", None)
        if emotion is not None:
            try:
                data = emotion.to_dict()
                self.emotion_label.config(
                    text=f"即时情绪：{data.get('emotion')}（{data.get('intensity')}）"
                )
            except Exception:
                self.emotion_label.config(text="即时情绪：读取失败")
        else:
            self.emotion_label.config(text="即时情绪：未接入")

        if self.memory is None:
            self.relation_label.config(text="关系状态：记忆不可用")
        else:
            try:
                data = self.memory.relationship.describe()
                self.relation_label.config(
                    text=(
                        f"关系状态：认识 {data.get('days')} 天，"
                        f"聊过 {data.get('interactions')} 次，算{data.get('stage')}"
                    )
                )
            except Exception:
                self.relation_label.config(text="关系状态：读取失败")

        self.mode_label.config(text="\n".join(mode_lines(getattr(self.pet, "behavior", None))))

        current = self.current_traits()
        self.conflict_label.config(text=traits_module.conflict_note(current))
        version = 0
        getter = getattr(self.pet, "persona_version", None)
        if getter is not None:
            try:
                version = int(getter())
            except Exception:
                version = 0
        self.version_label.config(text=f"人格版本：第 {version} 次改动")

        samples = "0"
        training = getattr(self.pet.agent, "training", None)
        if training is not None:
            try:
                samples = str((training.status() or {}).get("total", 0))
            except Exception:
                samples = "?"
        self.stats_label.config(
            text=f"反馈 {len(self.tree.get_children())} 条  ·  训练样本 {samples} 条"
        )

    # ---------- 动作 ----------
    def save_traits(self):
        """保存并立刻生效。存不下来就如实说，不假装成功。"""
        traits = self.current_traits()
        before = traits_module.load(self.memory)
        persona = getattr(self.pet.agent, "persona", None)
        applied = False
        if persona is not None:
            try:
                persona.set_traits(traits)
                applied = True
            except Exception as exc:
                self._hint(f"性格没能应用：{exc}")
        saved = traits_module.save(self.memory, traits)
        if not saved:
            self._hint("已经生效，但没存下来（记忆不可用），重启后会回到原来的性格。")
            self.refresh_status()
            return applied
        recorder = getattr(self.pet, "record_persona_change", None)
        if recorder is not None:
            recorder(before, traits, note="保存性格强度")
        note = traits_module.conflict_note(traits)
        self._hint(
            ("已保存，之后的回答会按新的性格来。" + note)
            if note
            else "已保存，之后的回答会按新的性格来。"
        )
        self.refresh_status()
        return True

    def undo_traits(self):
        """撤回上一次改动。撤不动就如实说，不假装。"""
        undo = getattr(self.pet, "undo_persona_change", None)
        if undo is None:
            self._hint("没有接入改动记录。")
            return False
        result = undo()
        if not (result or {}).get("ok"):
            self._hint(str((result or {}).get("error") or "撤不了"))
            return False
        self._load_traits()
        self.refresh_status()
        return True

    def reset_traits(self):
        """恢复成配置里的性格；没配置过就回到默认强度。"""
        default = traits_module.parse(getattr(self.pet.config, "persona_traits", ""))
        if not default:
            default = {name: traits_module.DEFAULT_INTENSITY for name in self.vars}
        for name, var in self.vars.items():
            try:
                var.set(int(default.get(name, 0)))
            except (TypeError, ValueError):
                var.set(0)
        return self.save_traits()

    def _selected_feedback(self):
        selection = self.tree.selection()
        if not selection:
            self._hint("先选一条反馈。")
            return None
        if self.memory is None:
            self._hint("记忆不可用。")
            return None
        try:
            row = self.memory.repository.get_memory(selection[0])
        except Exception as exc:
            self._hint(f"读不到这条：{exc}")
            return None
        if row is None:
            self._hint("这条反馈已经不在库里了。")
        return row

    def undo_feedback(self):
        row = self._selected_feedback()
        if row is None:
            return False
        if not messagebox.askyesno(
            "撤销反馈",
            "撤销这条反馈吗？撤销后它不再影响桌宠的表达，内容仍留在记忆里，可以恢复。\n\n"
            + str(row.get("content") or ""),
            parent=self.win,
        ):
            self._hint("已取消。")
            return False
        if self.memory is None or not self.memory.archive_memory(row["id"]):
            self._hint("撤销失败。")
            return False
        self._hint("已撤销，这条不再影响表达。")
        self.refresh()
        return True

    def export_samples(self):
        """把收集到的样本导成一个 JSON 文件，交给用户以后的训练流程。"""
        training = getattr(self.pet.agent, "training", None)
        if training is None:
            self._hint("没有接入样本收集。")
            return False
        try:
            rows = (training.export(limit=500) or {}).get("samples") or []
        except Exception as exc:
            self._hint(f"读样本失败：{exc}")
            return False
        if not rows:
            self._hint("还没有可导出的样本。")
            return False
        target = self._export_dir() / "persona-samples.json"
        try:
            with open(target, "w", encoding="utf-8") as handle:
                json.dump(
                    {"version": 1, "samples": rows}, handle, ensure_ascii=False, indent=2
                )
        except Exception as exc:
            self._hint(f"导出失败：{exc}")
            return False
        self._hint(f"已导出到 {target}")
        return True

    def reset_relationship(self):
        if self.memory is None:
            self._hint("记忆不可用。")
            return False
        if not messagebox.askyesno(
            "重置关系",
            "清掉认识天数和互动次数吗？聊天记录和记忆都不会删。",
            parent=self.win,
        ):
            self._hint("已取消。")
            return False
        if not self.memory.relationship.reset(confirm=True):
            self._hint("重置失败。")
            return False
        self._hint("已回到刚认识。")
        self.refresh_status()
        return True

    # ---------- 小工具 ----------
    def _export_dir(self):
        base = getattr(self.pet.config, "data_dir", None)
        target = (base / "exports") if base is not None else None
        if target is None:
            target = self.win.master
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return target

    def _hint(self, text):
        try:
            self.hint.config(text=text or "")
        except Exception:
            pass
        return text
