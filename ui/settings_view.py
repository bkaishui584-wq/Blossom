"""设置中心：按九个类别分区，普通用户和开发者看到的东西分开。

基础 / 模型 / 角色 / 人格 / 记忆 / 语音 / 主动行为 / 权限 / 开发者。
状态仍放在宿主（DesktopPet）上，这里只负责呈现与收集，避免把业务判断搬进界面。
"""

import json
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, simpledialog, ttk

from ui import panel, theme
from ui.subtitle_overlay import TEXT_COLOR_LABELS


class SettingsWindow:
    """独立设置窗口，用标签页分区，别把所有选项挤成一长条。"""
    TITLE = "设置"


    GEOMETRY = (620, 560)

    def __init__(self, pet, parent=None):

        self.pet = pet
        # 嵌进主工作台是一块 Frame，单独打开才是一个窗口。
        self.embedded = parent is not None
        self.win = panel.container(pet, self.TITLE, self.GEOMETRY, parent)
        self._build()

    # ---------- 搭建 ----------
    def _build(self):
        style = ttk.Style(self.win)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        # 分页做成浅色胶囊：没有系统灰边，选中项是白底 + 主文字色。
        style.configure(
            "TNotebook", background=theme.BG, borderwidth=0, tabmargins=(0, 0, 0, 0)
        )
        style.configure(
            "TNotebook.Tab",
            background=theme.PANEL_2,
            foreground=theme.MUTED,
            padding=(theme.PAD_SM + 4, theme.PAD_XS + 4),
            font=theme.FONT_SMALL,
            borderwidth=0,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", theme.PANEL), ("active", theme.SIDEBAR_ACTIVE)],
            foreground=[("selected", theme.TEXT), ("active", theme.TEXT)],
            expand=[("selected", (0, 0, 0, 0))],
        )
        style.configure("TFrame", background=theme.BG)

        self.book = ttk.Notebook(self.win)
        self.book.pack(fill="both", expand=True, padx=theme.PAD_SM, pady=(theme.PAD_SM, 0))
        for title, builder in (
            ("基础", self._general_tab),
            ("个性化", self._personalize_tab),
            ("模型", self._model_tab),
            ("角色", self._avatar_tab),
            ("人格", self._persona_tab),
            ("记忆", self._memory_tab),
            ("技能", self._skills_tab),
            ("语音", self._voice_tab),
            ("主动行为", self._proactive_tab),
            ("权限", self._permission_tab),
            ("开发者", self._developer_tab),
        ):
            frame = theme.card(self.book)
            self.book.add(frame, text=title)
            try:
                builder(frame)
                theme.cardify(frame)
            except Exception as exc:  # 某一页出错不能拖垮整个设置窗口
                theme.label(frame, f"这一页暂时不可用：{exc}", kind="small", fg=theme.ERROR).pack(
                    anchor="w", padx=theme.PAD_MD, pady=theme.PAD_MD
                )

        self.pet.settings_hint = theme.label(
            self.win, "", kind="small", fg=theme.MUTED, anchor="w"
        )
        self.pet.settings_hint.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_SM, 0))
        panel.close_button(self.win, self).pack(pady=theme.PAD_MD)

    # ---------- 分区 ----------
    def _personalize_tab(self, parent):
        """个性化：日夜开关、渐变主题、背景图片、背景动效。"""
        from ui import dnd, glass
        from ui.daynight import DayNightSwitch

        pet = self.pet
        outer = theme.scroll_area(parent)

        # 1) 日夜
        day_card = theme.card(outer)
        day_card.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_MD, theme.PAD_SM))
        theme.label(day_card, "日夜模式", kind="title", bg=theme.CARD).pack(
            anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_MD, theme.PAD_XS)
        )
        self.daynight = DayNightSwitch(
            day_card,
            mode=pet.day_night_mode(),
            command=self._pick_daynight,
            bg=theme.CARD,
        )
        self.daynight.pack(anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_XS, theme.PAD_SM))
        theme.label(
            day_card,
            "只改工作台的配色和背景，不碰桌宠模型、动作、物理和渲染进程。",
            kind="small",
            fg=theme.MUTED,
            bg=theme.CARD,
            wraplength=560,
            justify="left",
        ).pack(anchor="w", padx=theme.PAD_MD, pady=(0, theme.PAD_MD))

        # 2) 渐变主题
        preset_card = theme.card(outer)
        preset_card.pack(fill="x", padx=theme.PAD_MD, pady=(0, theme.PAD_SM))
        theme.label(preset_card, "渐变主题", kind="title", bg=theme.CARD).pack(
            anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_MD, theme.PAD_XS)
        )
        grid = tk.Frame(preset_card, bg=theme.CARD)
        grid.pack(fill="x", padx=theme.PAD_MD)
        for index, (key, item) in enumerate(theme.presets().items()):
            column = index % 2
            if column == 0:
                row = tk.Frame(grid, bg=theme.CARD)
                row.pack(fill="x", pady=(0, theme.PAD_XS))
            theme.button(
                row,
                item.get("label", key),
                lambda chosen=key: self._later(pet.set_theme_preset, chosen),
                kind="primary" if pet.theme_preset() == key else "chip",
            ).pack(side="left", padx=(0, theme.PAD_SM))
        theme.label(
            preset_card,
            "主题只换背景渐变和强调色，文字色阶单独保证对比度。",
            kind="small",
            fg=theme.MUTED,
            bg=theme.CARD,
            wraplength=560,
            justify="left",
        ).pack(anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_XS, theme.PAD_MD))

        # 3) 背景
        bg_card = theme.card(outer)
        bg_card.pack(fill="x", padx=theme.PAD_MD, pady=(0, theme.PAD_SM))
        head = tk.Frame(bg_card, bg=theme.CARD)
        head.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_MD, theme.PAD_XS))
        theme.label(head, "背景图片", kind="title", bg=theme.CARD).pack(side="left")
        theme.button(head, "选择图片", self._pick_background, kind="primary").pack(side="right")
        theme.button(head, "恢复渐变", self._clear_background).pack(
            side="right", padx=(0, theme.PAD_SM)
        )
        self.bg_preview_photo = None
        self.bg_preview = tk.Label(bg_card, bd=0, highlightthickness=1,
                                   highlightbackground=theme.BORDER, bg=theme.PANEL_2)
        self.bg_preview.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_XS, theme.PAD_XS))
        self.bg_status = theme.label(
            bg_card, "", kind="small", fg=theme.MUTED, bg=theme.CARD,
            wraplength=560, justify="left",
        )
        self.bg_status.pack(anchor="w", padx=theme.PAD_MD)
        self.bg_drop_hint = theme.label(
            bg_card, "", kind="small", fg=theme.MUTED, bg=theme.CARD,
            wraplength=560, justify="left",
        )
        self.bg_drop_hint.pack(anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_XS, 0))
        zoom_row = tk.Frame(bg_card, bg=theme.CARD)
        zoom_row.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_XS, theme.PAD_MD))
        theme.label(zoom_row, "缩放", kind="small", fg=theme.MUTED, bg=theme.CARD).pack(
            side="left"
        )
        theme.button(zoom_row, "缩小", lambda: self._bg_zoom(-0.1)).pack(
            side="left", padx=(theme.PAD_SM, 0)
        )
        theme.button(zoom_row, "放大", lambda: self._bg_zoom(0.1)).pack(
            side="left", padx=(theme.PAD_XS, 0)
        )
        self.bg_zoom_label = theme.label(zoom_row, "", kind="small", fg=theme.MUTED, bg=theme.CARD)
        self.bg_zoom_label.pack(side="left", padx=theme.PAD_SM)
        # 位置、模糊、暗化：都用滑动条，拖动时防抖重绘。
        info = self.pet.background_info()
        self.bg_value_labels = {}
        self._bg_slider(bg_card, "横向位置", "x", 0.0, 1.0, info.get("x", 0.5), "%.2f")
        self._bg_slider(bg_card, "纵向位置", "y", 0.0, 1.0, info.get("y", 0.5), "%.2f")
        self._bg_slider(bg_card, "模糊", "blur", 0, 30, info.get("blur", 10), "%.0f px")
        self._bg_slider(bg_card, "暗化", "dim", 0.0, 0.7, info.get("dim", 0.08), "%.2f")

        # 4) 动效
        motion_card = theme.card(outer)
        motion_card.pack(fill="x", padx=theme.PAD_MD, pady=(0, theme.PAD_MD))
        theme.label(motion_card, "背景动效", kind="title", bg=theme.CARD).pack(
            anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_MD, theme.PAD_XS)
        )
        motion_row = tk.Frame(motion_card, bg=theme.CARD)
        motion_row.pack(fill="x", padx=theme.PAD_MD)
        for level in theme.BG_MOTION_NAMES:
            theme.button(
                motion_row,
                theme.BG_MOTION_LABELS.get(level, level),
                lambda chosen=level: self._set_bg_motion(chosen),
                kind="primary" if pet.background_motion() == level else "chip",
            ).pack(side="left", padx=(0, theme.PAD_SM))
        theme.label(
            motion_card,
            "低强度每 30 秒、标准每 20 秒才重绘一次；窗口最小化时自动停。",
            kind="small",
            fg=theme.MUTED,
            bg=theme.CARD,
            wraplength=560,
            justify="left",
        ).pack(anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_XS, theme.PAD_MD))
        drop_ready = dnd.bind_tree(
            bg_card,
            self._drop_background,
            root=self.win,
            enter=self._bg_drop_enter,
            leave=self._bg_drop_leave,
        )
        self.bg_drop_hint.configure(
            text=(
                "也可以把 PNG / JPG / WebP / BMP 图片拖进这一块，直接用成背景。"
                if drop_ready
                else "当前环境没有装拖放组件，请用「选择图片」。"
            )
        )
        self._refresh_background_state()

    # ---------- 个性化：动作 ----------
    def _bg_slider(self, parent, label, name, low, high, value, fmt):
        row = tk.Frame(parent, bg=theme.CARD)
        row.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_XS, 0))
        theme.label(row, label, kind="small", fg=theme.MUTED, bg=theme.CARD, width=8).pack(
            side="left"
        )
        scale = ttk.Scale(
            row,
            from_=low,
            to=high,
            value=float(value),
            command=lambda current, key=name: self._bg_param(key, current),
        )
        scale.pack(side="left", fill="x", expand=True, padx=theme.PAD_SM)
        shown = theme.label(row, fmt % float(value), kind="small", fg=theme.MUTED, bg=theme.CARD)
        shown.pack(side="right")
        self.bg_value_labels[name] = (shown, fmt)
        return scale

    def _bg_param(self, name, value):
        result = self.pet.set_background_param(name, value)
        if result is None:
            return None
        entry = self.bg_value_labels.get(name)
        if entry:
            widget, fmt = entry
            try:
                widget.configure(text=fmt % float(result))
            except Exception:
                pass
        return result

    def _pick_daynight(self, mode):
        # 让开关先滑完，再重建界面；重建会销毁这个控件。
        self.win.after(260, lambda: self._later(self.pet.set_day_night, mode))
        return mode

    def _pick_background(self):
        path = filedialog.askopenfilename(
            parent=self.win,
            title="选一张背景图片",
            filetypes=[
                ("图片", "*.png *.jpg *.jpeg *.webp *.bmp"),
                ("所有文件", "*.*"),
            ],
        )
        if not path:
            return False
        result = self.pet.set_background(path)
        if not result.get("ok"):
            self._hint(str(result.get("error") or "这张图用不了"))
            return False
        self._refresh_background_state()
        self._hint("背景已换成：%s" % Path(path).name)
        return True

    def _drop_background(self, paths):
        """拖进来的图片只读取原文件，复制和覆盖都不做。"""
        paths = [str(item) for item in (paths or []) if str(item).strip()]
        if not paths:
            self._hint("没有收到图片文件。")
            return False
        result = self.pet.set_background(paths[0])
        if not result.get("ok"):
            self._hint(str(result.get("error") or "这张图用不了"))
            return False
        self._refresh_background_state()
        extra = "（一次拖了 %d 张，只用了第一张）" % len(paths) if len(paths) > 1 else ""
        self._hint("背景已换成：%s%s" % (Path(paths[0]).name, extra))
        return True

    def _bg_drop_enter(self):
        try:
            self.bg_preview.configure(highlightbackground=theme.ACCENT)
        except Exception:
            pass

    def _bg_drop_leave(self):
        try:
            self.bg_preview.configure(highlightbackground=theme.BORDER)
        except Exception:
            pass

    def _clear_background(self):
        self.pet.clear_background()
        self._refresh_background_state()
        self._hint("已恢复渐变背景。")
        return True

    def _bg_zoom(self, delta):
        info = self.pet.background_info()
        zoom = self.pet.set_background_zoom(float(info.get("zoom", 1.0)) + delta)
        self._refresh_background_state()
        return zoom

    def _set_bg_motion(self, level):
        self.pet.set_background_motion(level)
        self._hint("背景动效：%s" % theme.BG_MOTION_LABELS.get(level, level))
        return level

    def _refresh_background_state(self):
        from ui import glass

        info = self.pet.background_info()
        path = info.get("path") or ""
        if path and info.get("exists"):
            photo = glass.thumbnail(path, (520, 180))
            if photo is not None:
                self.bg_preview_photo = photo
                self.bg_preview.configure(image=photo, text="", height=180)
            else:
                self.bg_preview.configure(image="", text="预览生成失败", height=4)
            text = "%s" % Path(path).name
        else:
            self.bg_preview_photo = None
            self.bg_preview.configure(image="", text="当前用主题渐变", height=4)
            text = "没有设置背景图，用主题渐变。"
        error = str(info.get("error") or "")
        self.bg_status.configure(text=(text + ("（%s）" % error if error else "")))
        self.bg_zoom_label.configure(text="%.2f×" % float(info.get("zoom", 1.0)))

    def _model_tab(self, parent):
        # 多服务商、密钥、用途和连接测试都在这一块里；不再把配置拆成只读几行。
        from ui.model_panel import ModelPanel

        self.model_panel = ModelPanel(self.pet, parent)

    def _avatar_tab(self, parent):
        pet = self.pet
        for name, value in pet.avatar_rows():
            self._row(parent, name, value)
        theme.label(
            parent,
            "角色由独立透明窗口显示，模型和贴图都是你自己的文件；"
            "缺渲染环境时只保留透明拖动区域，聊天不受影响。",
            kind="small",
            fg=theme.MUTED,
            wraplength=self.GEOMETRY[0] - 40,
            justify="left",
        ).pack(anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_SM, theme.PAD_XS))

        row = tk.Frame(parent, bg=theme.BG)
        row.pack(fill="x", padx=theme.PAD_MD, pady=theme.PAD_SM)
        theme.button(row, "重新加载", pet.live2d_reload, kind="primary").pack(side="left")
        theme.button(row, "卸载", pet.live2d_unload).pack(side="left", padx=theme.PAD_SM)
        theme.button(row, "显示/隐藏", pet.live2d_toggle_visible).pack(side="left")

        zoom = tk.Frame(parent, bg=theme.BG)
        zoom.pack(fill="x", padx=theme.PAD_MD)
        theme.button(zoom, "调小", lambda: pet.live2d_scale(-0.1)).pack(side="left")
        theme.button(zoom, "调大", lambda: pet.live2d_scale(0.1)).pack(
            side="left", padx=theme.PAD_SM
        )

        packs = tk.Frame(parent, bg=theme.BG)
        packs.pack(fill="x", padx=theme.PAD_MD, pady=theme.PAD_SM)
        theme.label(packs, "角色包", kind="small", fg=theme.MUTED).pack(side="left")
        theme.button(packs, "新建", self._new_character, kind="primary").pack(
            side="left", padx=theme.PAD_SM
        )
        theme.button(packs, "删除", self._delete_character).pack(side="left")
        theme.button(packs, "备份", lambda: pet.backup_characters()).pack(
            side="left", padx=theme.PAD_SM
        )
        theme.button(packs, "恢复", self._restore_characters).pack(side="left")
        self.character_rows = tk.Frame(parent, bg=theme.BG)
        self.character_rows.pack(fill="x")
        self._render_character_rows()
        theme.label(
            parent,
            "角色包只记资源引用；删除不会动 live2d/ 里的模型源文件，备份也不含模型和密钥。",
            kind="small",
            fg=theme.MUTED,
            wraplength=self.GEOMETRY[0] - 40,
            justify="left",
        ).pack(anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_XS, 0))

    def _voice_tab(self, parent):
        pet = self.pet
        parent = theme.scroll_area(parent)
        for name, value in pet.voice_center_rows():
            self._row(parent, name, value)
        theme.label(
            parent,
            "唤醒词与持续监听是两回事：开了唤醒词要先说出关键词；停止监听后不再采集。",
            kind="small",
            fg=theme.MUTED,
            wraplength=self.GEOMETRY[0] - 40,
            justify="left",
        ).pack(anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_SM, theme.PAD_XS))

        listen = tk.Frame(parent, bg=theme.BG)
        listen.pack(fill="x", padx=theme.PAD_MD)
        theme.button(listen, "开始监听", lambda: pet.set_listening(True)).pack(side="left")
        theme.button(listen, "停止监听", lambda: pet.set_listening(False)).pack(
            side="left", padx=theme.PAD_SM
        )
        theme.button(listen, "试听一句", self._test_speech, kind="primary").pack(side="left")

        record = tk.Frame(parent, bg=theme.BG)
        record.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_SM, 0))
        self.seconds_var = tk.StringVar(value="5")
        theme.label(record, "录一句话", kind="small", fg=theme.MUTED).pack(side="left")
        tk.Spinbox(
            record,
            from_=1,
            to=30,
            width=3,
            textvariable=self.seconds_var,
            font=theme.FONT_SMALL,
        ).pack(side="left", padx=theme.PAD_XS)
        theme.label(record, "秒", kind="small", fg=theme.MUTED).pack(side="left")
        theme.button(record, "开始录", self._record, kind="primary").pack(
            side="left", padx=theme.PAD_SM
        )
        theme.label(
            parent,
            "录音要用麦克风，还要自己装识别引擎（例如 vosk）并配好模型；"
            "两样缺一样都只影响说话输入，打字聊天不受影响。",
            kind="small",
            fg=theme.MUTED,
            wraplength=self.GEOMETRY[0] - 40,
            justify="left",
        ).pack(anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_XS, 0))

        wake = tk.Frame(parent, bg=theme.BG)
        wake.pack(fill="x", padx=theme.PAD_MD, pady=theme.PAD_SM)
        theme.button(wake, "启用唤醒词", lambda: pet.set_wake(True)).pack(side="left")
        theme.button(wake, "停用唤醒词", lambda: pet.set_wake(False)).pack(
            side="left", padx=theme.PAD_SM
        )

        packs = tk.Frame(parent, bg=theme.BG)
        packs.pack(fill="x", padx=theme.PAD_MD)
        theme.label(packs, "语音包", kind="small", fg=theme.MUTED).pack(side="left")
        self.pack_var = tk.StringVar(value="")
        self.pack_box = ttk.Combobox(
            packs,
            values=[pack.label() for pack in pet.voice_packs()],
            textvariable=self.pack_var,
            state="readonly",
            width=16,
            font=theme.FONT_BODY,
        )
        self.pack_box.pack(side="left", padx=theme.PAD_SM)
        theme.button(packs, "使用", self._use_voice_pack).pack(side="left")
        theme.button(packs, "用系统默认", lambda: pet.select_voice_pack("")).pack(
            side="left", padx=theme.PAD_XS
        )
        theme.button(packs, "导入", self._import_voice_pack).pack(side="left")
        theme.label(
            parent,
            "语音包只提供名字、版本和语速音量；导入只记元数据，不执行包里的任何程序。",
            kind="small",
            fg=theme.MUTED,
            wraplength=self.GEOMETRY[0] - 40,
            justify="left",
        ).pack(anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_XS, 0))
        self._subtitle_section(parent)

    def _subtitle_section(self, parent):
        pet = self.pet
        theme.label(parent, "实时字幕", kind="title").pack(
            anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_MD, theme.PAD_XS)
        )
        self._subtitle_source_rows = pet.subtitle_sources()
        labels = []
        for item in self._subtitle_source_rows:
            kind = "系统音频" if item.get("kind") == "system" else "麦克风"
            labels.append("%s · %s" % (kind, item.get("name") or "未命名音源"))
        source_row = tk.Frame(parent, bg=theme.BG)
        source_row.pack(fill="x", padx=theme.PAD_MD)
        theme.label(source_row, "音源", kind="small", fg=theme.MUTED).pack(side="left")
        saved_source = str(pet._preference("subtitle.source") or "")
        selected = 0
        for index, item in enumerate(self._subtitle_source_rows):
            if str(item.get("id") or "") == saved_source:
                selected = index
                break
        self.subtitle_source_var = tk.StringVar(
            value=labels[selected] if labels else "没有可用音源"
        )
        self.subtitle_source_box = ttk.Combobox(
            source_row,
            values=labels,
            textvariable=self.subtitle_source_var,
            state="readonly" if labels else "disabled",
            width=31,
            font=theme.FONT_BODY,
        )
        self.subtitle_source_box.pack(side="left", padx=theme.PAD_SM)
        theme.label(
            parent,
            pet.subtitle_capture_note(),
            kind="small",
            fg=theme.MUTED,
            wraplength=self.GEOMETRY[0] - 40,
            justify="left",
        ).pack(anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_XS, 0))
        source_test_row = tk.Frame(parent, bg=theme.BG)
        source_test_row.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_XS, 0))
        theme.button(source_test_row, "测试音源", self._test_subtitle_source).pack(
            side="left"
        )
        self.subtitle_source_test_label = theme.label(
            source_test_row,
            "未测试",
            kind="small",
            fg=theme.MUTED,
            wraplength=420,
            justify="left",
        )
        self.subtitle_source_test_label.pack(side="left", padx=theme.PAD_SM)

        model_row = tk.Frame(parent, bg=theme.BG)
        model_row.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_SM, 0))
        theme.label(model_row, "识别模型", kind="small", fg=theme.MUTED).pack(side="left")
        self.subtitle_model_var = tk.StringVar(
            value=str(
                pet._preference("subtitle.model")
                or getattr(pet.config, "stt_model", "")
                or ""
            )
        )
        theme.entry(model_row, textvariable=self.subtitle_model_var, width=34).pack(
            side="left", padx=theme.PAD_SM
        )
        theme.button(model_row, "选择", self._choose_subtitle_model).pack(side="left")
        theme.button(model_row, "检查状态", self._refresh_subtitle_model_state).pack(
            side="left", padx=theme.PAD_XS
        )
        theme.label(
            parent,
            "在线模型目录需要 tokens.txt 加 encoder/decoder；transducer 还要 joiner。"
            "没有模型时不会假装能识别。",
            kind="small",
            fg=theme.MUTED,
            wraplength=self.GEOMETRY[0] - 40,
            justify="left",
        ).pack(anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_XS, 0))
        self.subtitle_model_state_label = theme.label(
            parent,
            "识别状态：%s" % pet.subtitle_asr_note(
                self.subtitle_model_var.get(),
                str(pet._preference("subtitle.asr_kind") or "auto"),
            ),
            kind="small",
            fg=theme.MUTED,
            wraplength=self.GEOMETRY[0] - 40,
            justify="left",
        )
        self.subtitle_model_state_label.pack(
            anchor="w", padx=theme.PAD_MD, pady=(2, 0)
        )

        perf_row = tk.Frame(parent, bg=theme.BG)
        perf_row.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_XS, 0))
        theme.label(perf_row, "识别线程", kind="small", fg=theme.MUTED).pack(
            side="left"
        )
        self.subtitle_threads_var = tk.StringVar(
            value=str(pet.subtitle_asr_threads())
        )
        self._spin(perf_row, self.subtitle_threads_var, 1, 8).pack(
            side="left", padx=theme.PAD_XS
        )
        theme.label(
            perf_row,
            "默认 2；线程多不等于更快，按机器负载调。",
            kind="small",
            fg=theme.MUTED,
        ).pack(side="left", padx=theme.PAD_XS)

        language_row = tk.Frame(parent, bg=theme.BG)
        language_row.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_SM, 0))
        theme.label(language_row, "源语言", kind="small", fg=theme.MUTED).pack(side="left")
        self.subtitle_source_language_var = tk.StringVar(
            value=str(pet._preference("subtitle.source_language") or "auto")
        )
        theme.entry(
            language_row, textvariable=self.subtitle_source_language_var, width=8
        ).pack(side="left", padx=(theme.PAD_XS, theme.PAD_MD))
        theme.label(language_row, "目标语言", kind="small", fg=theme.MUTED).pack(side="left")
        self.subtitle_target_var = tk.StringVar(
            value=str(pet._preference("subtitle.target_language") or "中文")
        )
        theme.entry(language_row, textvariable=self.subtitle_target_var, width=10).pack(
            side="left", padx=theme.PAD_XS
        )
        theme.label(language_row, "留空只显示原文", kind="small", fg=theme.MUTED).pack(
            side="left", padx=theme.PAD_XS
        )
        translation_row = tk.Frame(parent, bg=theme.BG)
        translation_row.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_XS, 0))
        theme.label(translation_row, "翻译引擎", kind="small", fg=theme.MUTED).pack(
            side="left"
        )
        self.subtitle_translation_roles = dict(pet.SUBTITLE_TRANSLATION_ROLES)
        role_names = {value: key for key, value in self.subtitle_translation_roles.items()}
        self.subtitle_translation_role_var = tk.StringVar(
            value=role_names.get(pet.subtitle_translation_role(), "任务模型")
        )
        ttk.Combobox(
            translation_row,
            values=list(self.subtitle_translation_roles),
            textvariable=self.subtitle_translation_role_var,
            state="readonly",
            width=10,
            font=theme.FONT_BODY,
        ).pack(side="left", padx=theme.PAD_XS)
        theme.button(
            translation_row, "应用引擎", self._apply_subtitle_translation_role
        ).pack(side="left", padx=theme.PAD_XS)
        theme.button(
            translation_row, "检查状态", pet.check_subtitle_translation
        ).pack(side="left")
        theme.label(
            parent,
            "字幕原文会发送给所选模型；云端地址还要单独打开「字幕云端翻译」权限。",
            kind="small",
            fg=theme.MUTED,
            wraplength=self.GEOMETRY[0] - 40,
            justify="left",
        ).pack(anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_XS, 0))

        mode_row = tk.Frame(parent, bg=theme.BG)
        mode_row.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_SM, 0))
        theme.label(mode_row, "显示", kind="small", fg=theme.MUTED).pack(side="left")
        self.subtitle_mode_labels = {
            "原文": "source",
            "仅译文": "translation",
            "双语": "bilingual",
        }
        mode_names = {value: key for key, value in self.subtitle_mode_labels.items()}
        self.subtitle_mode_var = tk.StringVar(
            value=mode_names.get(
                str(pet._preference("subtitle.mode") or "bilingual"), "双语"
            )
        )
        ttk.Combobox(
            mode_row,
            values=list(self.subtitle_mode_labels),
            textvariable=self.subtitle_mode_var,
            state="readonly",
            width=8,
            font=theme.FONT_BODY,
        ).pack(side="left", padx=theme.PAD_XS)
        theme.button(mode_row, "应用显示方式", self._apply_subtitle_mode).pack(
            side="left", padx=theme.PAD_XS
        )

        buttons = tk.Frame(parent, bg=theme.BG)
        buttons.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_SM, 0))
        theme.button(buttons, "开始", self._start_subtitles, kind="primary").pack(
            side="left"
        )
        theme.button(buttons, "重新连接", pet.reconnect_subtitles).pack(
            side="left", padx=theme.PAD_XS
        )
        theme.button(buttons, "暂停", pet.pause_subtitles).pack(
            side="left", padx=theme.PAD_XS
        )
        theme.button(buttons, "继续", pet.resume_subtitles).pack(
            side="left", padx=theme.PAD_XS
        )
        theme.button(buttons, "停止", pet.stop_subtitles).pack(side="left")

        window_buttons = tk.Frame(parent, bg=theme.BG)
        window_buttons.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_XS, 0))
        theme.button(window_buttons, "显示字幕窗", pet.show_subtitle_overlay).pack(
            side="left"
        )
        theme.button(window_buttons, "隐藏", pet.hide_subtitle_overlay).pack(
            side="left", padx=theme.PAD_XS
        )
        theme.button(window_buttons, "复制当前字幕", pet.copy_current_subtitles).pack(
            side="left", padx=theme.PAD_XS
        )

        self.subtitle_status_label = theme.label(
            parent,
            "状态：%s" % pet.subtitle_status_text(),
            kind="small",
            fg=theme.MUTED,
            wraplength=self.GEOMETRY[0] - 40,
            justify="left",
        )
        self.subtitle_status_label.pack(
            anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_XS, 0)
        )
        theme.label(
            parent,
            "翻译：%s。字幕只留在内存，不保存音频，也不自动写入长期记忆。"
            % pet.subtitle_translation_note(),
            kind="small",
            fg=theme.MUTED,
            wraplength=self.GEOMETRY[0] - 40,
            justify="left",
        ).pack(anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_XS, 0))

        theme.label(parent, "字幕窗外观", kind="title").pack(
            anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_MD, theme.PAD_XS)
        )
        settings = pet.subtitle_overlay_settings()
        look = tk.Frame(parent, bg=theme.BG)
        look.pack(fill="x", padx=theme.PAD_MD)
        theme.label(look, "字体", kind="small", fg=theme.MUTED).pack(side="left")
        try:
            families = sorted(set(tkfont.families(self.win)), key=str.lower)
        except Exception:
            families = [theme.FAMILY]
        self.subtitle_family_var = tk.StringVar(value=str(settings["font_family"]))
        family_box = ttk.Combobox(
            look,
            values=families,
            textvariable=self.subtitle_family_var,
            width=18,
            font=theme.FONT_SMALL,
        )
        family_box.pack(side="left", padx=(theme.PAD_XS, theme.PAD_MD))
        theme.label(look, "字号", kind="small", fg=theme.MUTED).pack(side="left")
        self.subtitle_font_var = tk.StringVar(value=str(settings["font_size"]))
        self._spin(look, self.subtitle_font_var, 9, 30).pack(
            side="left", padx=(theme.PAD_XS, theme.PAD_MD)
        )
        theme.label(look, "行距", kind="small", fg=theme.MUTED).pack(side="left")
        self.subtitle_spacing_var = tk.StringVar(
            value=str(settings["line_spacing"])
        )
        self._spin(look, self.subtitle_spacing_var, 0, 12).pack(
            side="left", padx=(theme.PAD_XS, theme.PAD_MD)
        )
        theme.label(look, "不透明度 %", kind="small", fg=theme.MUTED).pack(side="left")
        self.subtitle_alpha_var = tk.StringVar(
            value=str(int(round(float(settings["alpha"]) * 100)))
        )
        self._spin(look, self.subtitle_alpha_var, 82, 100).pack(
            side="left", padx=theme.PAD_XS
        )
        theme.button(look, "应用外观", self._apply_subtitle_appearance).pack(
            side="left", padx=theme.PAD_SM
        )

        detail = tk.Frame(parent, bg=theme.BG)
        detail.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_XS, 0))
        theme.label(detail, "最近字幕", kind="small", fg=theme.MUTED).pack(side="left")
        self.subtitle_rows_var = tk.StringVar(value=str(settings["recent_lines"]))
        self._spin(detail, self.subtitle_rows_var, 1, 12).pack(
            side="left", padx=(theme.PAD_XS, theme.PAD_MD)
        )
        theme.label(detail, "文字颜色", kind="small", fg=theme.MUTED).pack(side="left")
        color_names = {value: key for key, value in TEXT_COLOR_LABELS.items()}
        self.subtitle_color_var = tk.StringVar(
            value=color_names.get(str(settings["text_color"]), "跟随主题")
        )
        ttk.Combobox(
            detail,
            values=list(TEXT_COLOR_LABELS.values()),
            textvariable=self.subtitle_color_var,
            state="readonly",
            width=10,
            font=theme.FONT_SMALL,
        ).pack(side="left", padx=theme.PAD_XS)
        self.subtitle_auto_scroll_var = tk.BooleanVar(
            value=bool(settings["auto_scroll"])
        )
        tk.Checkbutton(
            detail,
            text="自动滚动",
            variable=self.subtitle_auto_scroll_var,
            command=self._apply_subtitle_scroll,
            bg=theme.BG,
            fg=theme.TEXT,
            selectcolor=theme.FIELD,
            activebackground=theme.BG,
            activeforeground=theme.TEXT,
            font=theme.FONT_SMALL,
        ).pack(side="left", padx=theme.PAD_MD)

        overlay_flags = tk.Frame(parent, bg=theme.BG)
        overlay_flags.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_XS, 0))
        self.subtitle_topmost_var = tk.BooleanVar(value=bool(settings["topmost"]))
        tk.Checkbutton(
            overlay_flags,
            text="字幕窗始终置顶",
            variable=self.subtitle_topmost_var,
            command=lambda: pet.set_subtitle_overlay_option(
                "topmost", bool(self.subtitle_topmost_var.get())
            ),
            bg=theme.BG,
            fg=theme.TEXT,
            selectcolor=theme.FIELD,
            activebackground=theme.BG,
            activeforeground=theme.TEXT,
            font=theme.FONT_SMALL,
        ).pack(side="left")
        self.subtitle_click_var = tk.BooleanVar(
            value=bool(settings["click_through"])
        )
        tk.Checkbutton(
            overlay_flags,
            text="点击穿透（用设置或快捷键恢复）",
            variable=self.subtitle_click_var,
            command=self._apply_subtitle_click_through,
            bg=theme.BG,
            fg=theme.TEXT,
            selectcolor=theme.FIELD,
            activebackground=theme.BG,
            activeforeground=theme.TEXT,
            font=theme.FONT_SMALL,
        ).pack(side="left", padx=theme.PAD_MD)
        theme.label(
            parent,
            "不透明度最低 82%，保证字幕文字仍然清楚。点击穿透只影响字幕窗，"
            "设置页和全局快捷键始终可用。",
            kind="small",
            fg=theme.MUTED,
            wraplength=self.GEOMETRY[0] - 40,
            justify="left",
        ).pack(anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_XS, 0))

        theme.label(parent, "字幕快捷键", kind="title").pack(
            anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_MD, theme.PAD_XS)
        )
        hotkeys = pet.subtitle_hotkeys()
        self.subtitle_hotkey_vars = {}
        for label, action in (
            ("开始", "start_subtitles_from_preferences"),
            ("暂停 / 继续", "toggle_subtitle_pause"),
            ("停止", "stop_subtitles"),
            ("显示 / 隐藏", "toggle_subtitle_overlay"),
        ):
            row = tk.Frame(parent, bg=theme.BG)
            row.pack(fill="x", padx=theme.PAD_MD, pady=(0, theme.PAD_XS))
            theme.label(row, label, kind="small", fg=theme.MUTED, width=12).pack(
                side="left", anchor="w"
            )
            var = tk.StringVar(value=hotkeys.get(action, ""))
            self.subtitle_hotkey_vars[action] = var
            theme.entry(row, textvariable=var, width=18).pack(side="left")
        theme.button(
            parent, "保存字幕快捷键", self._save_subtitle_hotkeys
        ).pack(anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_XS, 0))
        theme.label(
            parent,
            "留空表示不用。例：ctrl+alt+s。重复或已被占用的键会明确提示。",
            kind="small",
            fg=theme.MUTED,
            wraplength=self.GEOMETRY[0] - 40,
            justify="left",
        ).pack(anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_XS, 0))

        theme.label(parent, "字幕历史", kind="title").pack(
            anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_MD, theme.PAD_XS)
        )
        self.subtitle_history_var = tk.BooleanVar(
            value=pet.subtitle_history_enabled()
        )
        tk.Checkbutton(
            parent,
            text="保存最终字幕历史（默认关闭，只存文字，不存音频）",
            variable=self.subtitle_history_var,
            command=lambda: pet.set_subtitle_history(
                bool(self.subtitle_history_var.get())
            ),
            bg=theme.BG,
            fg=theme.TEXT,
            selectcolor=theme.FIELD,
            activebackground=theme.BG,
            activeforeground=theme.TEXT,
            font=theme.FONT_SMALL,
        ).pack(anchor="w", padx=theme.PAD_MD)
        history_row = tk.Frame(parent, bg=theme.BG)
        history_row.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_XS, 0))
        theme.button(history_row, "查看最近记录", self._show_subtitle_history).pack(
            side="left"
        )
        theme.button(
            history_row, "清空历史", self._clear_subtitle_history, kind="danger"
        ).pack(side="left", padx=theme.PAD_XS)
        self.subtitle_export_format = tk.StringVar(value="SRT")
        ttk.Combobox(
            history_row,
            values=("SRT", "VTT", "TXT"),
            textvariable=self.subtitle_export_format,
            state="readonly",
            width=6,
            font=theme.FONT_SMALL,
        ).pack(side="left", padx=(theme.PAD_MD, theme.PAD_XS))
        theme.button(history_row, "导出", self._export_subtitle_history).pack(
            side="left"
        )
        theme.label(
            parent,
            "历史记录存在本地数据库里，可随时清空或导出；不会自动进入长期记忆。",
            kind="small",
            fg=theme.MUTED,
            wraplength=self.GEOMETRY[0] - 40,
            justify="left",
        ).pack(anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_XS, 0))

    def _choose_subtitle_model(self):
        folder = filedialog.askdirectory(
            title="选择在线语音识别模型目录", parent=self.win
        )
        if folder:
            self.subtitle_model_var.set(folder)
            self._refresh_subtitle_model_state()

    def _refresh_subtitle_model_state(self):
        state = self.pet.subtitle_asr_state(
            self.subtitle_model_var.get(),
            str(self.pet._preference("subtitle.asr_kind") or "auto"),
        )
        note = str(state.get("note") or "识别状态未知")
        self.subtitle_model_state_label.configure(text="识别状态：%s" % note)
        if not state.get("ready"):
            self.pet._hint(note)
        return state

    def _start_subtitles(self):
        index = self.subtitle_source_box.current()
        if index < 0 or index >= len(self._subtitle_source_rows):
            self.pet._hint("没有可用音源，先安装可选的 soundcard 组件。")
            return False
        source = self._subtitle_source_rows[index]
        mode = self.subtitle_mode_labels.get(self.subtitle_mode_var.get(), "bilingual")
        try:
            threads = max(1, min(8, int(float(self.subtitle_threads_var.get()))))
        except (TypeError, ValueError):
            self.pet._hint("识别线程要填 1 到 8 的数字。")
            return False
        ok = self.pet.start_subtitles(
            source_id=str(source.get("id") or ""),
            model_path=self.subtitle_model_var.get().strip(),
            source_language=self.subtitle_source_language_var.get().strip() or "auto",
            target_language=self.subtitle_target_var.get().strip(),
            mode=mode,
            asr_threads=threads,
        )
        self.subtitle_status_label.configure(text="状态：%s" % self.pet.subtitle_status_text())
        self._refresh_subtitle_model_state()
        return ok

    def _test_subtitle_source(self):
        index = self.subtitle_source_box.current()
        if index < 0 or index >= len(self._subtitle_source_rows):
            self.pet._hint("没有可用音源，先安装可选的 soundcard 组件。")
            return False
        source = self._subtitle_source_rows[index]
        result = self.pet.test_subtitle_source(str(source.get("id") or ""))
        if result.get("ok"):
            note = str(result.get("note") or "音源可用")
            self.subtitle_source_test_label.configure(text=note, fg=theme.OK)
        else:
            note = str(result.get("error") or "测试失败")
            self.subtitle_source_test_label.configure(text=note, fg=theme.ERROR)
        return bool(result.get("ok"))

    def _apply_subtitle_mode(self):
        mode = self.subtitle_mode_labels.get(self.subtitle_mode_var.get(), "bilingual")
        self.pet.set_subtitle_mode(mode)

    def _spin(self, parent, variable, low, high):
        return tk.Spinbox(
            parent,
            from_=low,
            to=high,
            width=4,
            textvariable=variable,
            bg=theme.FIELD,
            fg=theme.TEXT,
            buttonbackground=theme.PANEL_2,
            insertbackground=theme.TEXT,
            relief="flat",
            highlightthickness=1,
            highlightbackground=theme.BORDER,
            font=theme.FONT_SMALL,
        )

    def _apply_subtitle_appearance(self):
        try:
            family = self.subtitle_family_var.get().strip()
            font_size = int(float(self.subtitle_font_var.get()))
            spacing = int(float(self.subtitle_spacing_var.get()))
            alpha = int(float(self.subtitle_alpha_var.get())) / 100.0
            rows = int(float(self.subtitle_rows_var.get()))
        except (TypeError, ValueError):
            self.pet._hint("字号、行距、最近字幕数和不透明度都要填数字。")
            return False
        color_names = {value: key for key, value in TEXT_COLOR_LABELS.items()}
        color = color_names.get(self.subtitle_color_var.get(), "theme")
        results = [
            self.pet.set_subtitle_overlay_option("font_family", family),
            self.pet.set_subtitle_overlay_option("font_size", font_size),
            self.pet.set_subtitle_overlay_option("line_spacing", spacing),
            self.pet.set_subtitle_overlay_option("recent_lines", rows),
            self.pet.set_subtitle_overlay_option("text_color", color),
            self.pet.set_subtitle_overlay_option("alpha", alpha),
        ]
        if all(results):
            self.pet._hint("字幕窗外观已应用。")
        return all(results)

    def _apply_subtitle_scroll(self):
        return self.pet.set_subtitle_overlay_option(
            "auto_scroll", bool(self.subtitle_auto_scroll_var.get())
        )

    def _apply_subtitle_translation_role(self):
        role_names = {value: key for key, value in self.subtitle_translation_roles.items()}
        role = role_names.get(self.subtitle_translation_role_var.get(), "task")
        ok = self.pet.set_subtitle_translation_role(role)
        if hasattr(self, "subtitle_status_label"):
            self.subtitle_status_label.configure(
                text="状态：%s" % self.pet.subtitle_status_text()
            )
        return ok

    def _apply_subtitle_click_through(self):
        ok = self.pet.set_subtitle_overlay_option(
            "click_through", bool(self.subtitle_click_var.get())
        )
        self.subtitle_click_var.set(
            bool(self.pet.subtitle_overlay_settings()["click_through"])
        )
        return ok

    def _save_subtitle_hotkeys(self):
        return self.pet.apply_subtitle_hotkeys(
            {
                action: variable.get()
                for action, variable in self.subtitle_hotkey_vars.items()
            }
        )

    def _show_subtitle_history(self):
        return self.pet._show_detail_window(
            "字幕历史", self.pet.subtitle_history_text(200)
        )

    def _clear_subtitle_history(self):
        from tkinter import messagebox

        if not messagebox.askyesno(
            "清空字幕历史",
            "会删除本地数据库里的全部字幕记录。这个操作不能撤销，继续吗？",
            parent=self.win,
        ):
            return 0
        return self.pet.clear_subtitle_history(confirm=True)

    def _export_subtitle_history(self):
        fmt = str(self.subtitle_export_format.get() or "SRT").strip().lower()
        ext = "." + fmt
        target = filedialog.asksaveasfilename(
            parent=self.win,
            title="导出字幕历史",
            defaultextension=ext,
            filetypes=[("字幕文件", "*" + ext), ("所有文件", "*.*")],
        )
        if not target:
            return {"ok": False, "error": "没有选择保存位置"}
        return self.pet.export_subtitle_history(target, fmt=fmt)

    def _persona_tab(self, parent):
        pet = self.pet
        for name, value in pet.persona_summary():
            self._row(parent, name, value)
        theme.label(
            parent,
            "性格只影响怎么说，不影响能不能做；改完立刻生效，也会存下来。",
            kind="small",
            fg=theme.MUTED,
            wraplength=self.GEOMETRY[0] - 40,
            justify="left",
        ).pack(anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_SM, theme.PAD_XS))
        theme.button(parent, "打开人格面板", pet.open_persona, kind="primary").pack(
            anchor="w", padx=theme.PAD_MD, pady=theme.PAD_SM
        )

    def _memory_tab(self, parent):
        pet = self.pet
        memory = pet.memory
        self._row(parent, "数据位置", str(getattr(pet.config, "db_path", "")))
        if memory is None:
            self._row(parent, "状态", "记忆不可用")
            return
        for name, value in (
            ("累计 Token", memory.usage_totals()),
            ("关系", getattr(memory, "relationship", None)),
            ("日记", getattr(memory, "diary", None)),
            ("时间线", getattr(memory, "timeline", None)),
            ("习惯", getattr(memory, "habit", None)),
        ):
            if value is None:
                continue
            self._row(parent, name, self._describe(value))

        try:
            stats = memory.stats()
        except Exception as exc:
            stats = None
            self._row(parent, "记忆统计", f"读取失败：{exc}")
        if stats is not None:
            kinds = stats.get("kinds") or {}
            summary = " / ".join(f"{name} {count}" for name, count in kinds.items())
            self._row(parent, "记忆统计", f"共 {stats.get('total', 0)} 条 {summary}")
            self._row(
                parent,
                "容量策略",
                f"上限 {stats.get('capacity')} 条，超出只归档不删除，锁定项不受限",
            )
        theme.button(parent, "打开记忆管理", self.pet.open_memory, kind="primary").pack(
            anchor="w", padx=theme.PAD_MD, pady=theme.PAD_SM
        )

    def _permission_tab(self, parent):
        pet = self.pet
        privacy = getattr(pet, "privacy", None)
        theme.label(parent, "本地能力与权限", kind="title").pack(
            anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_SM, theme.PAD_XS)
        )
        theme.label(
            parent,
            "看清哪些能力不用模型、每个工具要什么权限、哪些目录被持久授权过。",
            kind="small",
            fg=theme.MUTED,
            wraplength=self.GEOMETRY[0] - 40,
            justify="left",
        ).pack(anchor="w", padx=theme.PAD_MD)
        theme.button(
            parent, "打开能力与权限中心", pet.open_capabilities, kind="primary"
        ).pack(anchor="w", padx=theme.PAD_MD, pady=theme.PAD_SM)
        if privacy is None:
            theme.label(parent, "隐私开关暂不可用。", kind="small", fg=theme.MUTED).pack(
                anchor="w", padx=theme.PAD_MD, pady=theme.PAD_MD
            )
            return
        pet._privacy_vars = {}
        theme.label(parent, "你说了算", kind="title").pack(
            anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_SM, theme.PAD_XS)
        )
        for item in privacy.controls():
            var = tk.BooleanVar(value=bool(item.get("enabled")))
            pet._privacy_vars[item["key"]] = var
            tk.Checkbutton(
                parent,
                text=item.get("label", item["key"]),
                variable=var,
                command=lambda key=item["key"]: pet._save_privacy(key),
                bg=theme.BG,
                fg=theme.TEXT,
                selectcolor=theme.FIELD,
                activebackground=theme.BG,
                activeforeground=theme.ACCENT,
                font=theme.FONT_BODY,
                anchor="w",
            ).pack(fill="x", padx=theme.PAD_MD)
        for name, value in privacy.describe().items():
            self._row(parent, name, str(value))

    def _general_tab(self, parent):
        """基础：主题、开机启动、置顶、快捷键、位置与大小、日志与退出。"""
        pet = self.pet
        try:
            from logkit.report import version_text

            self._row(parent, "版本", version_text())
        except Exception:
            pass
        theme.label(parent, "外观", kind="title").pack(
            anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_SM, theme.PAD_XS)
        )
        self._row(parent, "主题与背景", "%s / %s（去「个性化」里改）" % (
            theme.preset_label(), "夜晚" if theme.active() == "dark" else "白天"
        ))

        theme.label(parent, "显示与缩放", kind="title").pack(
            anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_SM, theme.PAD_XS)
        )
        scale_row = tk.Frame(parent, bg=theme.BG)
        scale_row.pack(fill="x", padx=theme.PAD_MD)
        theme.button(scale_row, "缩小", lambda: self._later(pet.zoom_out)).pack(side="left")
        theme.label(scale_row, pet.scale_text(), kind="small").pack(
            side="left", padx=theme.PAD_SM
        )
        theme.button(scale_row, "放大", lambda: self._later(pet.zoom_in)).pack(side="left")
        theme.button(
            scale_row, "恢复默认", lambda: self._later(pet.zoom_reset)
        ).pack(side="left", padx=theme.PAD_SM)
        theme.label(scale_row, "Ctrl + 加号 / 减号 / 0", kind="small", fg=theme.MUTED).pack(
            side="left"
        )

        slider_row = tk.Frame(parent, bg=theme.BG)
        slider_row.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_XS, 0))
        theme.label(slider_row, "界面缩放", kind="small", fg=theme.MUTED).pack(side="left")
        self.scale_slider = tk.Scale(
            slider_row,
            from_=theme.SCALE_MIN,
            to=theme.SCALE_MAX,
            resolution=0.05,
            orient="horizontal",
            length=240,
            width=14,
            sliderlength=22,
            showvalue=1,
            font=theme.FONT_SMALL,
            bg=theme.BG,
            fg=theme.TEXT,
            troughcolor=theme.PANEL_2,
            activebackground=theme.ACCENT,
            highlightthickness=0,
            bd=0,
            relief="flat",
            sliderrelief="flat",
        )
        self.scale_slider.set(theme.user_scale())
        self.scale_slider.pack(side="left", padx=theme.PAD_SM)
        self.scale_slider.bind(
            "<ButtonRelease-1>", lambda event: self._later(
                pet.set_ui_scale, self.scale_slider.get()
            )
        )
        self.scale_slider.bind(
            "<Right>", lambda event: self._later(pet.zoom_in)
        )
        self.scale_slider.bind(
            "<Left>", lambda event: self._later(pet.zoom_out)
        )

        font_row = tk.Frame(parent, bg=theme.BG)
        font_row.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_XS, 0))
        theme.label(font_row, "字号", kind="small", fg=theme.MUTED).pack(side="left")
        for name in theme.FONT_SCALE_STEPS:
            theme.button(
                font_row,
                theme.FONT_SCALE_LABELS.get(name, name),
                lambda chosen=name: self._later(pet.set_font_scale, chosen),
                kind="primary" if pet.font_scale_name() == name else "ghost",
            ).pack(side="left", padx=(theme.PAD_SM, 0))
        theme.label(
            parent,
            "缩放只影响 DeskPet 自己，不会改 Windows 的显示设置。改完主界面会重新打开一次，"
            "聊天记录和设置都留着。",
            kind="small",
            fg=theme.MUTED,
            wraplength=self.GEOMETRY[0] - 40,
            justify="left",
        ).pack(anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_XS, theme.PAD_SM))

        theme.label(parent, "新手说明与建议", kind="title").pack(
            anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_SM, theme.PAD_XS)
        )
        help_row = tk.Frame(parent, bg=theme.BG)
        help_row.pack(fill="x", padx=theme.PAD_MD)
        theme.button(help_row, "再看一遍新手说明", self._reopen_wizard).pack(side="left")
        theme.button(help_row, "恢复「以后不提」的建议", self._unmute_suggestions).pack(
            side="left", padx=theme.PAD_SM
        )
        theme.label(
            parent,
            "新手说明只在第一次启动时自动出现。被「以后不提」挡掉的建议会一直不出现，"
            "想找回来就点右边这个按钮。",
            kind="small",
            fg=theme.MUTED,
            wraplength=self.GEOMETRY[0] - 40,
            justify="left",
        ).pack(anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_XS, theme.PAD_SM))

        theme.label(parent, "启动与窗口", kind="title").pack(
            anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_SM, theme.PAD_XS)
        )
        pet.motion_var = tk.BooleanVar(value=pet.motion_reduced())
        tk.Checkbutton(
            parent,
            text="减少动态效果（角色不再自动摆动和呼吸，眨眼保留）",
            variable=pet.motion_var,
            command=pet._save_motion,
            bg=theme.BG,
            fg=theme.TEXT,
            selectcolor=theme.FIELD,
            activebackground=theme.BG,
            activeforeground=theme.TEXT,
            font=theme.FONT_BODY,
        ).pack(anchor="w", padx=theme.PAD_MD, pady=(0, theme.PAD_XS))

        pet.startup_var = tk.BooleanVar(value=pet.startup_enabled())
        tk.Checkbutton(
            parent,
            text="开机自动启动桌宠",
            variable=pet.startup_var,
            command=pet._save_startup,
            bg=theme.BG,
            fg=theme.TEXT,
            selectcolor=theme.FIELD,
            activebackground=theme.BG,
            activeforeground=theme.ACCENT,
            font=theme.FONT_BODY,
            anchor="w",
        ).pack(fill="x", padx=theme.PAD_MD)

        pet.topmost_var = tk.BooleanVar(value=pet.topmost_enabled())
        tk.Checkbutton(
            parent,
            text="桌宠窗口始终置顶",
            variable=pet.topmost_var,
            command=pet._save_topmost,
            bg=theme.BG,
            fg=theme.TEXT,
            selectcolor=theme.FIELD,
            activebackground=theme.BG,
            activeforeground=theme.ACCENT,
            font=theme.FONT_BODY,
            anchor="w",
        ).pack(fill="x", padx=theme.PAD_MD)

        hotkey_row = tk.Frame(parent, bg=theme.BG)
        hotkey_row.pack(fill="x", padx=theme.PAD_MD, pady=theme.PAD_SM)
        theme.label(hotkey_row, "全局快捷键", kind="small", fg=theme.MUTED).pack(side="left")
        pet.hotkey_entry = theme.entry(hotkey_row, width=14)
        pet.hotkey_entry.insert(0, pet._hotkey_spec())
        pet.hotkey_entry.pack(side="left", padx=theme.PAD_SM, ipady=4)
        theme.button(hotkey_row, "保存", pet._save_hotkey).pack(side="left")
        theme.label(hotkey_row, "例：ctrl+alt+d（留空=不用）", kind="small", fg=theme.MUTED).pack(
            side="left", padx=theme.PAD_SM
        )

        theme.label(parent, "位置与大小", kind="title").pack(
            anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_SM, theme.PAD_XS)
        )
        place_row = tk.Frame(parent, bg=theme.BG)
        place_row.pack(fill="x", padx=theme.PAD_MD)
        theme.button(place_row, "回到右下角", pet.reset_position).pack(side="left")
        theme.button(place_row, "调小一些", lambda: pet.live2d_scale(-0.1)).pack(
            side="left", padx=theme.PAD_SM
        )
        theme.button(place_row, "调大一些", lambda: pet.live2d_scale(0.1)).pack(side="left")
        theme.button(place_row, "显示 / 隐藏桌宠", pet.toggle_visible).pack(
            side="left", padx=theme.PAD_SM
        )
        self._row(parent, "现在的坐标", pet.position_text())

        theme.label(parent, "贴边与缩入", kind="title").pack(
            anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_SM, theme.PAD_XS)
        )
        dock_row = tk.Frame(parent, bg=theme.BG)
        dock_row.pack(fill="x", padx=theme.PAD_MD)
        theme.button(dock_row, "贴到最近的边", lambda: pet.dock_pet()).pack(side="left")
        theme.button(dock_row, "取消贴边", lambda: pet.undock_pet()).pack(
            side="left", padx=theme.PAD_SM
        )
        theme.button(dock_row, "缩入 / 滑出", lambda: pet.toggle_peek()).pack(side="left")
        pet.dock_var = tk.BooleanVar(value=pet.dock_enabled())
        tk.Checkbutton(
            parent,
            text="拖到屏幕边上自动吸附",
            variable=pet.dock_var,
            command=lambda: pet.set_dock(pet.dock_var.get()),
            bg=theme.BG,
            fg=theme.TEXT,
            selectcolor=theme.FIELD,
            activebackground=theme.BG,
            activeforeground=theme.ACCENT,
            font=theme.FONT_BODY,
            anchor="w",
        ).pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_XS, 0))
        pet.dock_auto_var = tk.BooleanVar(value=pet.dock_auto_hide())
        tk.Checkbutton(
            parent,
            text="吸附后自动缩到边上，鼠标碰一下再出来",
            variable=pet.dock_auto_var,
            command=lambda: pet.set_dock_auto(pet.dock_auto_var.get()),
            bg=theme.BG,
            fg=theme.TEXT,
            selectcolor=theme.FIELD,
            activebackground=theme.BG,
            activeforeground=theme.ACCENT,
            font=theme.FONT_BODY,
            anchor="w",
        ).pack(fill="x", padx=theme.PAD_MD)
        self._row(parent, "贴边状态", pet.dock_text())

        theme.label(
            parent,
            "角色大小只有在接上 Live2D 形象后才生效；没接上时只保留固定大小的透明拖动区域。",
            kind="small",
            fg=theme.MUTED,
            wraplength=self.GEOMETRY[0] - 40,
            justify="left",
        ).pack(anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_XS, theme.PAD_SM))

        info = tk.Frame(parent, bg=theme.BG)
        info.pack(fill="x", padx=theme.PAD_MD, pady=theme.PAD_SM)
        theme.button(info, "打开日志位置", self._open_logs).pack(side="left")
        theme.button(info, "导出诊断包", self._export_diagnostics).pack(
            side="left", padx=theme.PAD_SM
        )
        theme.button(info, "退出桌宠", pet.close, kind="danger").pack(
            side="left", padx=theme.PAD_SM
        )

    def _proactive_tab(self, parent):
        """主动行为：什么时候可以主动出声，以及怎么随时让它安静。"""
        pet = self.pet
        pet.proactive_var = tk.BooleanVar(value=pet._preference_enabled())
        tk.Checkbutton(
            parent,
            text="允许主动提醒",
            variable=pet.proactive_var,
            command=pet._save_proactive,
            bg=theme.BG,
            fg=theme.TEXT,
            selectcolor=theme.FIELD,
            activebackground=theme.BG,
            activeforeground=theme.ACCENT,
            font=theme.FONT_BODY,
            anchor="w",
        ).pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_SM, 0))

        pet.quiet_var = tk.BooleanVar(value=pet._quiet_enabled())
        tk.Checkbutton(
            parent,
            text="静默模式（不发声、不主动说话）",
            variable=pet.quiet_var,
            command=pet._save_quiet,
            bg=theme.BG,
            fg=theme.TEXT,
            selectcolor=theme.FIELD,
            activebackground=theme.BG,
            activeforeground=theme.ACCENT,
            font=theme.FONT_BODY,
            anchor="w",
        ).pack(fill="x", padx=theme.PAD_MD)

        hours = tk.Frame(parent, bg=theme.BG)
        hours.pack(fill="x", padx=theme.PAD_MD, pady=theme.PAD_SM)
        theme.label(hours, "免打扰时段", kind="small", fg=theme.MUTED).pack(side="left")
        pet.quiet_entry = theme.entry(hours, width=10)
        pet.quiet_entry.insert(0, pet._preference("quiet_hours") or "23-7")
        pet.quiet_entry.pack(side="left", padx=theme.PAD_SM, ipady=4)
        theme.button(hours, "保存", pet._save_quiet_hours).pack(side="left")
        theme.label(hours, "例：23-7", kind="small", fg=theme.MUTED).pack(
            side="left", padx=theme.PAD_SM
        )

        limits = tk.Frame(parent, bg=theme.BG)
        limits.pack(fill="x", padx=theme.PAD_MD, pady=theme.PAD_SM)
        theme.label(limits, "主动冷却(秒)", kind="small", fg=theme.MUTED).pack(side="left")
        pet.cooldown_entry = theme.entry(limits, width=7)
        pet.cooldown_entry.insert(
            0,
            str(
                pet.proactive_number(
                    "proactive.cooldown_seconds",
                    getattr(pet.config, "proactive_cooldown", 0),
                )
            ),
        )
        pet.cooldown_entry.pack(side="left", padx=theme.PAD_SM, ipady=4)
        theme.button(
            limits,
            "保存",
            lambda: pet._save_proactive_number(
                "proactive.cooldown_seconds", pet.cooldown_entry, 0, 86400
            ),
        ).pack(side="left")
        theme.label(limits, "每天最多", kind="small", fg=theme.MUTED).pack(
            side="left", padx=(theme.PAD_MD, 0)
        )
        pet.daily_entry = theme.entry(limits, width=5)
        pet.daily_entry.insert(
            0,
            str(
                pet.proactive_number(
                    "proactive.max_per_day",
                    getattr(pet.config, "proactive_max_per_day", 0),
                )
            ),
        )
        pet.daily_entry.pack(side="left", padx=theme.PAD_SM, ipady=4)
        theme.button(
            limits,
            "保存",
            lambda: pet._save_proactive_number(
                "proactive.max_per_day", pet.daily_entry, 0, 100
            ),
        ).pack(side="left")
        theme.label(
            parent,
            "冷却和每日上限只会让桌宠更安静，0 表示不加额外限制。"
            "待机动作不调用模型，主动说话才在规则允许时问一次模型。",
            kind="small",
            fg=theme.MUTED,
            wraplength=self.GEOMETRY[0] - 40,
            justify="left",
        ).pack(anchor="w", padx=theme.PAD_MD)

        self._row(parent, "现在的模式", pet._mode_row())

        focus_row = tk.Frame(parent, bg=theme.BG)
        focus_row.pack(fill="x", padx=theme.PAD_MD, pady=theme.PAD_SM)
        theme.label(focus_row, "专注模式", kind="small", fg=theme.MUTED).pack(side="left")
        theme.button(focus_row, "进入", pet.enter_focus).pack(
            side="left", padx=theme.PAD_SM
        )
        theme.button(focus_row, "退出", pet.exit_focus).pack(side="left")
        theme.label(
            parent,
            "专注时桌宠变淡、少说话；全屏时程序自动进入，也可以在这里手动切。",
            kind="small",
            fg=theme.MUTED,
            wraplength=self.GEOMETRY[0] - 40,
            justify="left",
        ).pack(anchor="w", padx=theme.PAD_MD, pady=(0, theme.PAD_SM))

    def _reopen_wizard(self):
        """把新手说明再打开一次，不动已经记下的设置。"""
        window = self.pet.open_wizard(force=True)
        if window is None:
            self.pet._hint("新手说明打不开了，看看日志里怎么说。")
            return False
        self.pet._hint("新手说明已经打开。")
        return window

    def _unmute_suggestions(self):
        """把「以后不提」的建议全部恢复，首页会重新出现它们。"""
        muted = self.pet.muted_suggestions()
        self.pet.unmute_suggestions()
        if muted:
            self.pet._hint("已恢复 %d 条建议。" % len(muted))
        else:
            self.pet._hint("本来就没有被屏蔽的建议。")
        return len(muted)

    def _later(self, handler, *args):
        """缩放会重建整个工作台，回调要等这一轮事件走完再执行。"""
        def run():
            try:
                handler(*args)
            except Exception as exc:
                self.pet._warn("scale change failed: %s", exc)
            return None

        try:
            self.pet.root.after(80, run)
        except Exception:
            run()
        return None

    def _theme_is(self, name):
        stored = self.pet._preference(theme.THEME_PREFERENCE) or "dark"
        return str(stored).strip().lower() == str(name).strip().lower()

    def _pick_theme(self, name):
        """换主题会重建设置窗口，所以等这一轮回调走完再动手。"""
        pet = self.pet
        try:
            self.win.after(60, lambda: pet.set_theme(name))
        except Exception:
            pet.set_theme(name)
        return name

    def _accent_is(self, name):
        return theme.accent() == str(name or "").strip().lower()

    def _pick_accent(self, name):
        """强调色也走同一套重画流程，等这一轮回调走完再动手。"""
        pet = self.pet
        try:
            self.win.after(60, lambda: pet.set_accent(name))
        except Exception:
            pet.set_accent(name)
        return name

    def _skills_tab(self, parent):
        """技能：看清单、看权限、导入与管理。这里不执行任何技能。"""
        pet = self.pet
        theme.label(parent, "已装技能", kind="title").pack(
            anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_SM, theme.PAD_XS)
        )
        theme.label(
            parent,
            "技能只描述怎么做；真正执行的是工具，每个工具仍然要单独过权限和确认。",
            kind="small",
            fg=theme.MUTED,
            wraplength=self.GEOMETRY[0] - 40,
            justify="left",
        ).pack(anchor="w", padx=theme.PAD_MD)
        rows = pet.skill_rows()
        self._row(parent, "数量", pet._skills_text())
        if not rows:
            self._row(parent, "状态", "还没有装技能")
        for row in rows[:10]:
            detail = "%s · %s · %s 个步骤%s" % (
                row.get("version"), row.get("permission"), row.get("steps"),
                "" if row.get("enabled") else " · 已停用",
            )
            self._row(parent, str(row.get("name")), detail)
        buttons = tk.Frame(parent, bg=theme.BG)
        buttons.pack(fill="x", padx=theme.PAD_MD, pady=theme.PAD_SM)
        theme.button(buttons, "打开技能管理", pet.open_skills, kind="primary").pack(side="left")
        theme.button(buttons, "重新扫描", self._reload_skills).pack(
            side="left", padx=theme.PAD_SM
        )
        theme.label(
            parent,
            "导入别人的技能时，只会读说明文件；脚本和依赖安装必须单独审查后授权。",
            kind="small",
            fg=theme.MUTED,
            wraplength=self.GEOMETRY[0] - 40,
            justify="left",
        ).pack(anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_XS, theme.PAD_SM))

    def _reload_skills(self):
        manager = getattr(getattr(self.pet, "agent", None), "skills", None)
        if manager is None:
            self.pet._hint("没有接入技能系统。")
            return False
        try:
            manager.reload()
        except Exception as exc:
            self.pet._hint("重新扫描失败：%s" % exc)
            return False
        self.pet._hint("已重新扫描技能目录。")
        window = getattr(self.pet, "skills_window", None)
        if window is not None:
            try:
                window.refresh()
            except Exception:
                pass
        return True

    def _developer_tab(self, parent):
        """开发者面板：程序状态、Token、记忆与设备。全都不发送给模型。"""
        pet = self.pet
        if not getattr(pet.config, "dev_mode", False):
            theme.label(
                parent,
                "开发者模式没有打开。设好 DESKPET_DEV_MODE=1 再重启，"
                "就能看到模型状态、Token 用量、记忆条数、渲染后端和权限情况。",
                kind="small",
                fg=theme.MUTED,
                wraplength=self.GEOMETRY[0] - 40,
                justify="left",
            ).pack(anchor="w", padx=theme.PAD_MD, pady=theme.PAD_MD)
            return

        def refresh():
            for child in parent.winfo_children():
                child.destroy()
            self._developer_tab(parent)

        for name, value in pet.developer_rows():
            self._row(parent, name, value)
        theme.label(
            parent,
            "这些只是程序自己的状态，不会发送给模型，也不含密钥。",
            kind="small",
            fg=theme.MUTED,
            wraplength=self.GEOMETRY[0] - 40,
            justify="left",
        ).pack(anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_SM, 0))
        row = tk.Frame(parent, bg=theme.BG)
        row.pack(fill="x", padx=theme.PAD_MD, pady=theme.PAD_SM)
        theme.button(row, "刷新", refresh, kind="primary").pack(side="left")
        theme.button(row, "打开日志位置", self._open_logs).pack(
            side="left", padx=theme.PAD_SM
        )
        theme.button(row, "导出诊断包", self._export_diagnostics).pack(
            side="left", padx=theme.PAD_SM
        )


    # ---------- 小工具 ----------
    def _row(self, parent, name, value):
        line = tk.Frame(parent, bg=theme.BG)
        line.pack(fill="x", padx=theme.PAD_MD, pady=1)
        theme.label(line, str(name), kind="small", fg=theme.MUTED, width=10, anchor="w").pack(
            side="left"
        )
        theme.label(
            line,
            str(value),
            kind="small",
            anchor="w",
            justify="left",
            wraplength=self.GEOMETRY[0] - 150,
        ).pack(side="left")

    def _mic_text(self):
        mic = getattr(self.pet, "mic", None)
        if mic is None:
            return "未接入"
        try:
            return str(mic.label())
        except Exception:
            return "状态未知"

    def _describe(self, obj):
        """把状态对象、字典或字符串统一成一行能看的文字。"""
        if obj is None:
            return "未接入"
        if isinstance(obj, str):
            return obj
        describe = getattr(obj, "describe", None)
        if describe is None:
            return self._dump(obj)
        try:
            data = describe()
        except Exception as exc:
            return f"读取失败：{exc}"
        return self._dump(data)

    def _dump(self, data):
        if isinstance(data, str):
            return data
        try:
            return json.dumps(data, ensure_ascii=False)
        except Exception:
            return str(data)

    def _use_voice_pack(self):
        """按下拉框里选中的那一个切换语音包。"""
        target = self.pack_var.get()
        for pack in self.pet.voice_packs():
            if pack.label() == target:
                return self.pet.select_voice_pack(pack.id)
        self.pet._hint("先选一个语音包。")
        return False

    def _import_voice_pack(self):
        """导入语音包：只读元数据，不执行包里的任何东西。"""
        folder = filedialog.askdirectory(title="选一个语音包文件夹", parent=self.win)
        if not folder:
            return False
        return bool(self.pet.import_voice_pack(folder))

    def _render_character_rows(self):
        """角色包统计每次都重新读真实文件，不用重开设置窗。"""
        frame = getattr(self, "character_rows", None)
        if frame is None:
            return []
        for child in frame.winfo_children():
            child.destroy()
        rows = self.pet.character_package_rows()
        for name, value in rows:
            self._row(frame, name, value)
        return rows

    def _record(self):
        """录一句话再交给识别管线。缺设备或缺引擎时宿主会如实提示。"""
        try:
            seconds = float(self.seconds_var.get())
        except (TypeError, ValueError):
            self.pet._hint("录音秒数填数字。")
            return False
        return self.pet.record_phrase(seconds)

    def _new_character(self):
        """新建角色包：只问名字，模型自动引用当前正在显示的那个。"""
        name = simpledialog.askstring("新建角色包", "给这个角色起个名字：", parent=self.win)
        if not name:
            return False
        self.pet.create_character_package(identifier=name, name=name)
        self._render_character_rows()
        return True

    def _delete_character(self):
        """删角色包。先让用户点名字，再让宿主报影响范围并二次确认。"""
        from tkinter import messagebox

        name = simpledialog.askstring(
            "删除角色包", "要删除的角色包名字：", parent=self.win
        )
        if not name:
            return False
        preview = self.pet.delete_character_package(name)
        if preview.get("ok"):
            self._render_character_rows()
            return True
        if not preview.get("needs_confirm"):
            return False
        if not messagebox.askyesno(
            "删除角色包",
            f"{preview.get('error')}\n\n确认删除吗？live2d/ 里的模型文件不会被删。",
            parent=self.win,
        ):
            self.pet._hint("已取消。")
            return False
        self.pet.delete_character_package(name, confirm=True)
        self._render_character_rows()
        return True

    def _restore_characters(self):
        """从备份 zip 里恢复角色包，只解出清单类小文件。"""
        source = filedialog.askopenfilename(
            title="选一个角色包备份",
            filetypes=[("角色包备份", "*.zip"), ("所有文件", "*.*")],
            parent=self.win,
        )
        if not source:
            return False
        self.pet.restore_characters(source)
        self._render_character_rows()
        return True

    def _test_speech(self):
        pet = self.pet
        if pet.speech is None:
            pet._hint("没有接入语音输出。")
            return False
        pet._hint("试听中……")
        try:
            ok = pet.speech.say("你好，我是桌宠。")
        except Exception as exc:
            pet._hint(f"发声失败：{exc}")
            return False
        pet._hint("已经念出去了。" if ok else "语音输出当前不可用（可能被静默模式挡住）。")
        return bool(ok)

    def _export_diagnostics(self):
        """导出一个脱敏的诊断包。密钥、密码都不会进包。"""
        pet = self.pet
        export = getattr(pet, "export_diagnostics", None)
        if not callable(export):
            pet._hint("这一层没有导出入口。")
            return None
        return export()

    def _open_logs(self):
        import os

        pet = self.pet
        try:
            path = str(pet.config.log_dir)
            os.startfile(path)  # noqa: S606 - 只打开自己的日志目录
            pet._hint(f"已打开：{path}")
        except Exception as exc:
            pet._hint(f"打不开日志目录：{exc}")
