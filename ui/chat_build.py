"""主工作台的搭界面部分：顶栏、左导航、会话抽屉、消息区、输入区。

从 ui/chat_view.py 整块搬过来的，只负责把控件摆好；状态和交互在别处。"""

import tkinter as tk
from tkinter import ttk

from ui import glass_native, theme
from ui.chat_input import ChatInput


class ChatBuild:
    """混入 DesktopPet。行为与搬之前完全一样。"""

    def _build(self):
        # 重建界面时换个主题，外层窗口的底色也得跟着换，不然边框还留着旧颜色。
        try:
            self.parent.configure(bg=theme.BG)
        except Exception:
            pass
        # 顶栏 60px：左边品牌与会话名，右边运行模式、模型和窗口控制。
        self.bar = tk.Frame(self.parent, bg=theme.TOPBAR_BG, height=theme.TOPBAR_HEIGHT)
        self.bar.pack(fill="x")
        self.bar.pack_propagate(False)

        self.nav_button = theme.button(self.bar, "\u2630", self.toggle_nav, kind="toolbar")
        self.nav_button.pack(side="left", padx=(theme.PAD_SM, 0))
        theme.tooltip(self.nav_button, "收起或展开左侧导航")
        self.brand = theme.label(
            self.bar, "Blossom", kind="title", bg=theme.TOPBAR_BG, fg=theme.ACCENT
        )
        self.brand.pack(side="left", padx=theme.PAD_SM)
        self.brand_icon = theme.label(
            self.bar, "/", kind="small", bg=theme.TOPBAR_BG, fg=theme.BORDER
        )
        self.brand_icon.pack(side="left", padx=(theme.PAD_SM, 0))
        self.title = theme.label(
            self.bar, "\u5bf9\u8bdd", kind="small", bg=theme.TOPBAR_BG, fg=theme.MUTED
        )
        self.title.pack(side="left", padx=(theme.PAD_SM, 0))

        # 右侧窗口控制与入口，自右向左摆。
        self.close_button = theme.button(
            self.bar, "\u2715", self._close, kind="toolbar_danger"
        )
        self.close_button.pack(side="right", padx=theme.PAD_SM)
        theme.tooltip(self.close_button, "收起聊天，桌宠继续待着")
        self.max_button = theme.button(
            self.bar, "\u25a1", self.toggle_maximize, kind="toolbar"
        )
        self.max_button.pack(side="right")
        theme.tooltip(self.max_button, "最大化或还原窗口")
        self.min_button = theme.button(self.bar, "\u2013", self.minimize, kind="toolbar")
        self.min_button.pack(side="right")
        theme.tooltip(self.min_button, "最小化")

        self.settings_button = theme.button(
            self.bar, "\u2699", self._settings, kind="toolbar"
        )
        self.settings_button.pack(side="right", padx=(0, theme.PAD_XS))
        theme.tooltip(self.settings_button, "设置：模型、人格、记忆、能力都在里面")
        self.panel_button = theme.button(
            self.bar, "\u25a4", self.toggle_panel, kind="toolbar"
        )
        self.panel_button.pack(side="right", padx=(0, theme.PAD_XS))
        theme.tooltip(self.panel_button, "展开或收起右侧任务面板")
        self.search_button = theme.button(
            self.bar, "\u2315", self._search, kind="toolbar"
        )
        self.search_button.pack(side="right", padx=(0, theme.PAD_XS))
        theme.tooltip(self.search_button, "在当前会话的历史消息里找一句")
        self.tasks_button = theme.button(self.bar, "\u2714", self._tasks, kind="toolbar")
        self.tasks_button.pack(side="right", padx=(0, theme.PAD_XS))
        theme.tooltip(self.tasks_button, "任务中心：看做过什么、正在做什么")
        self.clear_button = theme.button(self.bar, "\u232b", self._clear, kind="toolbar")
        self.clear_button.pack(side="right", padx=(0, theme.PAD_XS))
        theme.tooltip(self.clear_button, "清空当前对话（会先问你一次）")

        self.model_button = theme.button(
            self.bar, "模型：未配置", self._model_menu, kind="chip", font=theme.FONT_SMALL
        )
        self.model_button.pack(side="right", padx=(0, theme.PAD_SM))
        theme.tooltip(self.model_button, "切换陪伴模型")
        self.mode_label = theme.label(
            self.bar, "\u672c\u5730", kind="small", bg=theme.TOPBAR_BG, fg=theme.MUTED
        )
        self.mode_label.pack(side="right", padx=(0, theme.PAD_SM))

        # 聊天 / 任务两种视图的开关。切的是「看什么」，权限和执行规则都不变。
        self._task_mode = False
        self.mode_toggle = theme.button(self.bar, "任务模式", self._mode_toggle, kind="ghost")
        self.mode_toggle.pack(side="right", padx=(0, theme.PAD_XS))
        theme.tooltip(self.mode_toggle, "在普通聊天和任务视图之间切换")

        self.drag_handles = (self.bar, self.brand, self.brand_icon, self.title)

        # A|B|C：左导航、中央工作区、右侧任务面板。
        self.body = tk.Frame(self.parent, bg=theme.BG)
        # 留一圈边：液态玻璃背景在四周露出来，面板才像浮在背景上。
        self.body.pack(fill="both", expand=True, padx=14, pady=(0, 14))

        self.nav = glass_native.round_widget(tk.Frame(
            self.body,
            bg=theme.SIDEBAR_BG,
            width=theme.NAV_COLLAPSED_WIDTH,
            highlightthickness=1,
            highlightbackground=theme.BORDER,
        ), theme.RADIUS_LG)
        # 导航用 place 叠在内容上：展开/悬停只改它自己的宽度，
        # 不让中央聊天区跟着重新排版（那会挤压并闪动）。
        self.nav.place(x=0, y=0, relheight=1.0, width=theme.NAV_COLLAPSED_WIDTH)
        self._nav_width_applied = theme.NAV_COLLAPSED_WIDTH
        self._main_pad_applied = None
        self.nav_rows = {}
        self.nav_collapsed = False
        self.nav_texts = {}
        self.nav_expanded = True
        # 「固定展开」和「鼠标悬停临时展开」是两件事：悬停不改偏好。
        self.nav_pinned = True
        self._nav_hover = False
        self._nav_leave_job = None
        self.nav_active = ""
        self._build_nav()

        self.panel = glass_native.round_widget(tk.Frame(
            self.body,
            bg=theme.CARD,
            width=theme.PANEL_WIDTH,
            highlightthickness=1,
            highlightbackground=theme.BORDER,
        ), theme.RADIUS_LG)
        self.panel.pack_propagate(False)
        self.panel_title = theme.label(self.panel, "任务详情", kind="title", bg=theme.CARD)
        self.panel_title.pack(anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_MD, theme.PAD_XS))
        self.panel_text = theme.text_area(self.panel, height=8, width=1, bg=theme.CARD)
        self.panel_text.pack(fill="both", expand=True, padx=theme.PAD_MD, pady=(0, theme.PAD_MD))
        self.panel_shown = False
        self._panel_cache = ""
        self.panel_placeholder = "还没有正在跑的任务。任务开始后，步骤和结果会显示在这里。"

        self.main = tk.Frame(self.body, bg=theme.BG)
        self.main.pack(
            side="left",
            fill="both",
            expand=True,
            padx=(theme.NAV_COLLAPSED_WIDTH + theme.PAD_SM + 4, 0),
        )

        # 会话抽屉：平时收起，展开时宽窗口占一列、窄窗口浮在中央区上面。
        self.side = glass_native.round_widget(tk.Frame(
            self.body,
            bg=theme.SIDEBAR_BG,
            width=self.SIDEBAR_WIDTH,
            highlightthickness=1,
            highlightbackground=theme.BORDER,
        ), theme.RADIUS_LG)
        head = tk.Frame(self.side, bg=theme.SIDEBAR_BG)
        head.pack(fill="x", padx=theme.PAD_SM, pady=(theme.PAD_SM, theme.PAD_XS))
        self.side_title = theme.label(
            head, "最近会话", kind="small", fg=theme.MUTED, bg=theme.SIDEBAR_BG
        )
        self.side_title.pack(side="left")
        self.session_menu_button = theme.button(
            head, "\u22ef", self._session_menu, kind="ghost", font=theme.FONT_SMALL
        )
        self.session_menu_button.pack(side="right")
        theme.tooltip(self.session_menu_button, "重命名、置顶、归档、删除、导出")
        self.session_list = tk.Listbox(
            self.side,
            bg=theme.SIDEBAR_BG,
            fg=theme.TEXT,
            bd=0,
            highlightthickness=0,
            selectbackground=theme.SIDEBAR_ACTIVE,
            selectforeground=theme.TEXT,
            font=theme.FONT_SMALL,
            activestyle="none",
            exportselection=False,
        )
        self.session_list.pack(fill="both", expand=True, padx=theme.PAD_XS)
        self.session_list.bind("<<ListboxSelect>>", self._session_picked)
        self.session_list.bind("<Double-Button-1>", self._session_rename_request)
        self.new_session_button = theme.button(
            self.side, "\uff0b 新对话", self._new_session, kind="ghost", font=theme.FONT_SMALL
        )
        self.new_session_button.pack(fill="x", padx=theme.PAD_SM, pady=theme.PAD_SM)

        # 状态行：常驻只写一行状态，模型和 token 收进「详情」。
        self.status_row = tk.Frame(self.main, bg=theme.BG)
        self.status = theme.label(
            self.status_row, "", kind="small", fg=theme.MUTED, anchor="w"
        )
        self.status.pack(side="left", fill="x", expand=True)
        self.status_detail = theme.button(
            self.status_row, "详情", self._status_detail, kind="chip", font=theme.FONT_SMALL
        )
        self.status_detail.pack(side="right")
        theme.tooltip(self.status_detail, "模型、任务和这一轮用了多少 token，点这里看")

        # 空会话：品牌标记 + 一句问话 + 人格问候 + 四张能力卡片（2×2）。
        self.empty = tk.Frame(self.main, bg=theme.BG)
        # 背景图横幅：有图时直接把用户选的图铺出来，不再是纯色打底。
        self.hero_banner = tk.Label(
            self.empty, bd=0, highlightthickness=1, highlightbackground=theme.BORDER, bg=theme.BG
        )
        glass_native.round_widget(self.hero_banner, theme.RADIUS_LG)
        self._hero_photo = None
        hero = tk.Frame(self.empty, bg=theme.BG)
        hero.pack(anchor="center", pady=(theme.PAD_LG, theme.PAD_SM))
        self.empty_hero = hero
        self.empty_icon = theme.label(
            hero, "\u25c8", kind="hero", bg=theme.BG, fg=theme.ACCENT
        )
        self.empty_icon.pack(anchor="center")
        self.empty_title = theme.label(
            hero, self.EMPTY_TITLE, kind="hero", bg=theme.BG, fg=theme.TEXT
        )
        self.empty_title.pack(anchor="center", pady=(theme.PAD_XS, theme.PAD_XS))
        self.empty_text = theme.label(
            hero, self.DEFAULT_GREETING, kind="body", fg=theme.MUTED,
            anchor="center", justify="center",
        )
        self.empty_text.pack(anchor="center")

        self.empty_cards_box = tk.Frame(self.empty, bg=theme.BG)
        self.empty_cards_box.pack(fill="x")
        self.empty_actions = tk.Frame(self.empty_cards_box, bg=theme.BG)
        self.empty_actions.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_MD, theme.PAD_SM))
        self.empty_cards = {}
        self.empty_card_icons = {}
        rows = []
        for index, (key, title, note, colour) in enumerate(self.EMPTY_CARDS):
            line_index, column = divmod(index, 2)
            if column == 0:
                rows.append(tk.Frame(self.empty_actions, bg=theme.BG))
                rows[-1].pack(fill="x")
            card = tk.Frame(
                rows[line_index],
                bg=theme.CARD,
                highlightthickness=1,
                highlightbackground=theme.BORDER,
                cursor="hand2",
            )
            glass_native.round_widget(card, theme.RADIUS)
            card.pack(
                side="left", fill="both", expand=True,
                padx=(0, theme.PAD_SM) if column == 0 else (0, 0),
                pady=(0, theme.PAD_SM),
            )
            icon = theme.label(
                card, self.CARD_ICONS.get(key, "\u25c7"), kind="body",
                bg=theme.CARD, fg=getattr(theme, colour, theme.ACCENT),
                font=theme.FONT_TITLE,
            )
            icon.pack(anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_SM, 0))
            heading = theme.label(card, title, kind="body", bg=theme.CARD, fg=theme.TEXT)
            heading.pack(anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_XS, 0))
            note_label = theme.label(card, note, kind="small", bg=theme.CARD, fg=theme.MUTED)
            note_label.pack(anchor="w", padx=theme.PAD_MD, pady=(0, theme.PAD_SM))
            for widget in (card, icon, heading, note_label):
                widget.bind("<Button-1>", lambda event, name=key: self._card_clicked(name))

            # 悬浮反馈：底色挪一档、边框换成强调色。不动布局，只改颜色。
            def _hover(event=None, box=card, items=(card, icon, heading, note_label)):
                for item in items:
                    try:
                        item.configure(bg=theme.SIDEBAR_ACTIVE)
                    except Exception:
                        continue
                try:
                    box.configure(highlightbackground=theme.ACCENT)
                except Exception:
                    pass

            def _leave(event=None, box=card, items=(card, icon, heading, note_label)):
                for item in items:
                    try:
                        item.configure(bg=theme.CARD)
                    except Exception:
                        continue
                try:
                    box.configure(highlightbackground=theme.BORDER)
                except Exception:
                    pass

            for widget in (card, icon, heading, note_label):
                widget.bind("<Enter>", _hover, add="+")
                widget.bind("<Leave>", _leave, add="+")
            self.empty_cards[key] = card
            self.empty_card_icons[key] = (icon, colour)

        # 首页信息流：宿主按真实数据给卡片，没数据就不摆。
        self.feed_box = tk.Frame(self.empty, bg=theme.BG)
        self.feed_cards = []

        # 推荐操作：宿主按真实能力给，没给就整块不出现。
        self.empty_notes = tk.Frame(self.empty, bg=theme.BG)
        self.empty_notes.pack(anchor="w", fill="x", padx=theme.PAD_MD)
        self.tip_chips = []

        # 消息区
        self.wrap = tk.Frame(self.main, bg=theme.BG)
        # 请求高度只留 4 行：窗口矮的时候先让消息区收缩，
        # 别把任务条和输入区挤出可视范围（字号放大后尤其明显）。
        self.transcript = theme.text_area(self.wrap, height=4, width=1)
        self.transcript.pack(side="left", fill="both", expand=True)
        # ttk 的滚动条跟主题走；tk.Scrollbar 在 Windows 上是系统灰条，深色界面里很跳。
        self.scroll = ttk.Scrollbar(
            self.wrap, command=self.transcript.yview, style="Vertical.TScrollbar"
        )
        self.scroll.pack(side="right", fill="y")
        self.transcript.config(yscrollcommand=self.scroll.set, state="normal")
        self._paint_transcript_bg()
        self.transcript.config(state="disabled")

        # 单行任务条：正在跑的那一步。
        self.task_box = tk.Frame(self.main, bg=theme.BUBBLE_SYS)
        glass_native.round_widget(self.task_box, theme.RADIUS_SM)
        self.task_label = theme.label(
            self.task_box, "", kind="small", bg=theme.BUBBLE_SYS, fg=theme.WARN, anchor="w",
            justify="left",
        )
        self.task_label.pack(side="left", padx=theme.PAD_SM, pady=theme.PAD_XS)
        self.task_cancel = theme.button(self.task_box, "中止", self._cancel, kind="danger")

        # 等待授权卡片：桌宠要动敏感操作时摆出来，点了才继续。
        self.perm_box = tk.Frame(self.main, bg=theme.BUBBLE_SYS)
        glass_native.round_widget(self.perm_box, theme.RADIUS_SM)
        self.perm_label = theme.label(
            self.perm_box, "", kind="small", bg=theme.BUBBLE_SYS, fg=theme.WARN, anchor="w",
            justify="left",
        )
        self.perm_label.pack(side="left", padx=theme.PAD_SM, pady=theme.PAD_XS)
        self.perm_allow = theme.button(self.perm_box, "允许", self._perm_allow, kind="primary")
        self.perm_deny = theme.button(self.perm_box, "拒绝", self._perm_deny, kind="danger")
        self._permission_open = False

        self.state = theme.label(self.main, "", kind="small", fg=theme.MUTED, anchor="w")

        # 输入工作台
        self.composer = tk.Frame(self.main, bg=theme.BG)
        self.attach_box = tk.Frame(self.composer, bg=theme.BG)
        self.attach_chips = {}
        self.action_bar = tk.Frame(self.composer, bg=theme.BG)
        self.action_copy = theme.button(
            self.action_bar, "复制回复", self.copy_all, kind="chip", font=theme.FONT_SMALL
        )
        self.action_copy.pack(side="left")
        self.action_edit = theme.button(
            self.action_bar, "编辑重发", self._edit_resend, kind="chip", font=theme.FONT_SMALL
        )
        self.action_edit.pack(side="left", padx=(theme.PAD_XS, 0))
        theme.tooltip(self.action_edit, "把上一条自己的消息放回输入框重发")
        self.action_regen = theme.button(
            self.action_bar, "重新生成", self._regen_last, kind="chip", font=theme.FONT_SMALL
        )
        self.action_regen.pack(side="left", padx=(theme.PAD_XS, 0))
        self.action_good = theme.button(
            self.action_bar, "有用", self._feedback_good, kind="chip", font=theme.FONT_SMALL
        )
        self.action_good.pack(side="left", padx=(theme.PAD_XS, 0))
        self.action_bad = theme.button(
            self.action_bar, "没用", self._feedback_bad, kind="chip", font=theme.FONT_SMALL
        )
        self.action_bad.pack(side="left", padx=(theme.PAD_XS, 0))

        self.command_hint = theme.label(self.composer, "", kind="small", fg=theme.MUTED)

        self.field = tk.Frame(
            self.composer,
            bg=theme.FIELD,
            highlightthickness=1,
            highlightbackground=theme.BORDER,
            highlightcolor=theme.ACCENT,
        )
        glass_native.round_widget(self.field, theme.RADIUS_LG)
        self.field.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_XS, 0))
        self.input = ChatInput(self.field, on_send=self.on_send)
        self.input.pack(fill="x", padx=theme.PAD_SM, pady=(theme.PAD_SM, theme.PAD_XS))

        row = tk.Frame(self.composer, bg=theme.BG)
        row.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_XS, theme.PAD_SM))
        self.send_button = theme.button(row, "\u2191", self._send, kind="primary")
        self.send_button.pack(side="right")
        theme.tooltip(self.send_button, "发送（Enter）")
        self.more_button = theme.button(
            row, "＋", self.toggle_tools, kind="chip", font=theme.icon_font(2)
        )
        self.more_button.pack(side="left")
        theme.tooltip(self.more_button, "附件、工具、技能、语音都收在这里")
        # 图一那排图标入口，点下去走的还是原来那套真实动作。
        self.bar_attach = theme.button(
            row, "\u2709", self._attach, kind="chip", font=theme.icon_font(2)
        )
        self.bar_attach.pack(side="left", padx=(theme.PAD_XS, 0))
        theme.tooltip(self.bar_attach, "挑一个文件给我看")
        self.bar_web = theme.button(
            row, "\u2316", self._web_search, kind="chip", font=theme.icon_font(2)
        )
        self.bar_web.pack(side="left", padx=(theme.PAD_XS, 0))
        theme.tooltip(self.bar_web, "网页搜索：还没配置，先用不了")
        self.bar_tools = theme.button(
            row, "\u2726", self.toggle_tools, kind="chip", font=theme.icon_font(2)
        )
        self.bar_tools.pack(side="left", padx=(theme.PAD_XS, 0))
        theme.tooltip(self.bar_tools, "技能和工具都在这儿")
        self.bar_voice = theme.button(
            row, "\u266a", self._voice, kind="chip", font=theme.icon_font(2)
        )
        self.bar_voice.pack(side="left", padx=(theme.PAD_XS, 0))
        theme.tooltip(self.bar_voice, "语音：现在没有麦克风，用不了")
        self.bar_knowledge = theme.button(
            row, "\u25a4", self._knowledge, kind="chip", font=theme.icon_font(2)
        )
        self.bar_knowledge.pack(side="left", padx=(theme.PAD_XS, 0))
        theme.tooltip(self.bar_knowledge, "在本地文件里找资料，片段能回原文")
        self.bar_web.config(state="disabled")
        self.bar_voice.config(state="disabled")
        self.composer_model = theme.button(
            row, "模型", self._model_menu, kind="chip", font=theme.FONT_SMALL
        )
        self.composer_model.pack(side="left", padx=(theme.PAD_XS, 0))
        theme.tooltip(self.composer_model, "换陪伴模型")
        self.retry_button = theme.button(row, "重试", self._retry, kind="ghost")
        self.cancel_button = theme.button(row, "停止", self._cancel, kind="danger")
        theme.tooltip(self.cancel_button, "停掉这一轮生成")
        self.input_row = row

        # 分组工具面板：默认收起，点「＋」才出来，不占常驻位置。
        self.tool_open = False
        self.tool_box = tk.Frame(
            self.composer, bg=theme.PANEL_2, highlightthickness=1,
            highlightbackground=theme.BORDER,
        )
        glass_native.round_widget(self.tool_box, theme.RADIUS_LG)
        self._build_tools(self.tool_box)

        # 右下角缩放把手；由宿主决定何时 pack。
        self.grip = tk.Frame(self.parent, bg=theme.BG, cursor="size_nw_se", height=10)
        self.grip_label = theme.label(self.grip, "\u25e2", kind="small", fg=theme.MUTED)
        self.grip_label.pack(side="right", padx=theme.PAD_SM)

        self.menu = tk.Menu(self.parent, tearoff=0)
        self.menu.add_command(label="复制这条", command=self.copy_message_at)
        self.menu.add_command(label="重新生成这条", command=self.regenerate_message_at)
        self.menu.add_command(label="编辑并重新发送", command=self._edit_resend)
        self.menu.add_separator()
        self.menu.add_command(label="这条有用", command=self._feedback_good)
        self.menu.add_command(label="这条没用", command=self._feedback_bad)
        self.menu.add_separator()
        self.menu.add_command(label="复制选中", command=self.copy_selection)
        self.menu.add_command(label="复制全部", command=self.copy_all)
        self.menu.add_separator()
        self.menu.add_command(label="清空对话", command=self._clear)
        # 右键点在哪一行，菜单才知道「这条」是哪条。
        self._menu_line = None
        self.transcript.bind("<Button-3>", self._popup)

        # 斜杠命令菜单：输入 / 时弹出来，挑一条就走。
        self.command_menu = tk.Menu(self.parent, tearoff=0)
        self.commands = []
        self.input.bind("<KeyRelease>", self._slash_changed, add="+")

        # 排布顺序要紧：输入区先按自己的高度占位，剩下多少才归消息区。
        self.status_row.pack(side="top", fill="x", padx=theme.PAD_MD, pady=(theme.PAD_SM, theme.PAD_XS))
        self.composer.pack(side="bottom", fill="x")
        self.state.pack(side="bottom", fill="x", padx=theme.PAD_MD)
        self.wrap.pack(side="top", fill="both", expand=True, padx=theme.PAD_SM)
        self._apply_action_bar()
        self._build_page_host()
        self.show_empty(True)

    # ---------- 中央功能页 ----------

    def _build_page_host(self):
        """人格、记忆、设置、任务这些页面都挂在这里，不再另开大窗口。"""
        self.page_host = tk.Frame(self.main, bg=theme.BG)
        self._page_open = False
        self._page_wanted_empty = True
        self.page_key = ""
        self._stashed_chat = []
        head = tk.Frame(self.page_host, bg=theme.BG)
        head.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_SM, theme.PAD_SM))
        self.page_back = theme.button(head, "\u2190 \u8fd4\u56de\u804a\u5929", self.close_page, kind="ghost")
        self.page_back.pack(side="left")
        self.page_title = theme.label(head, "", kind="title", bg=theme.BG, fg=theme.TEXT)
        self.page_title.pack(side="left", padx=(theme.PAD_SM, 0))
        self.page_body = theme.scroll_area(self.page_host)
        return self.page_body

    def _build_tools(self, box):
        """分组工具面板：文件、能力、语音各一行，每项都写清用途。

        没有后端支撑的入口照旧灰着并说明原因，不做只能看的假按钮。
        """
        def group(title):
            line = tk.Frame(box, bg=theme.PANEL_2)
            line.pack(fill="x", padx=theme.PAD_SM, pady=(theme.PAD_SM, 0))
            theme.label(
                line, title, kind="small", fg=theme.MUTED, bg=theme.PANEL_2, width=4, anchor="w"
            ).pack(side="left")
            return line

        line = group("文件")
        self.attach_button = theme.button(
            line, "附件", self._attach, kind="chip", font=theme.FONT_SMALL
        )
        self.attach_button.pack(side="left")
        theme.tooltip(self.attach_button, "挑一个文件，把路径填进输入框")
        self.image_button = theme.button(
            line, "图片", self._attach_image, kind="chip", font=theme.FONT_SMALL
        )
        self.image_button.pack(side="left", padx=(theme.PAD_XS, 0))
        theme.tooltip(self.image_button, "挑一张图片，路径会写进输入框")

        line = group("能力")
        self.skill_button = theme.button(
            line, "技能", self._skills, kind="chip", font=theme.FONT_SMALL
        )
        self.skill_button.pack(side="left")
        theme.tooltip(self.skill_button, "选一个技能来干这件事")
        self.web_button = theme.button(
            line, "网页搜索", self._web_search, kind="chip", font=theme.FONT_SMALL
        )
        self.web_button.pack(side="left", padx=(theme.PAD_XS, 0))
        theme.tooltip(self.web_button, "没有配置搜索服务，暂时不可用")
        self.knowledge_button = theme.button(
            line, "知识库", self._knowledge, kind="chip", font=theme.FONT_SMALL
        )
        self.knowledge_button.pack(side="left", padx=(theme.PAD_XS, 0))
        theme.tooltip(self.knowledge_button, "在本地文件里找资料，片段能回原文")

        line = group("语音")
        self.voice_button = theme.button(
            line, "语音输入", self._voice, kind="chip", font=theme.FONT_SMALL
        )
        self.voice_button.pack(side="left")
        theme.tooltip(self.voice_button, "没有检测到可用的语音输入")

        self.web_button.config(state="disabled")
        self.voice_button.config(state="disabled")

        self.drop_hint = theme.label(
            box,
            "聊天附件先用上面的「附件」挑；背景图片可以直接拖进设置 → 个性化。",
            kind="small", fg=theme.MUTED, bg=theme.PANEL_2, anchor="w",
        )
        self.drop_hint.pack(fill="x", padx=theme.PAD_SM, pady=(theme.PAD_XS, theme.PAD_SM))

    def _build_nav(self):
        """固定入口，按定稿图分组摆放；「新建对话」单独做成顶上的大按钮。"""
        self.nav_icons = {}
        self.nav_group_labels = {}
        for key, label in self.NAV_ITEMS:
            heading = self.NAV_GROUPS.get(key)
            if heading:
                group_label = theme.label(
                    self.nav, heading, kind="small", bg=theme.SIDEBAR_BG, fg=theme.MUTED
                )
                group_label.pack(
                    anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_SM, theme.PAD_XS)
                )
                self.nav_group_labels[key] = group_label
            top = key == "new"
            base = theme.SIDEBAR_ACTIVE if top else theme.SIDEBAR_BG
            row = tk.Frame(
                self.nav, bg=base, cursor="hand2",
                highlightthickness=1 if top else 0,
                highlightbackground=theme.BORDER,
            )
            row.pack(fill="x", padx=theme.PAD_XS, pady=theme.PAD_XS if top else 1)
            icon = theme.label(
                row, self.NAV_ICONS.get(key, "\u25c7"), kind="body" if top else "small",
                bg=base, fg=theme.ACCENT if top else theme.MUTED, width=2,
                font=theme.icon_font(2),
            )
            icon.pack(side="left", padx=(theme.PAD_SM if top else theme.PAD_XS, 0),
                      pady=theme.PAD_SM)
            text = theme.label(
                row, label, kind="body" if top else "small", bg=base, fg=theme.TEXT
            )
            text.pack(side="left", padx=(theme.PAD_XS, 0))
            for widget in (row, icon, text):
                widget.bind("<Button-1>", lambda event, name=key: self._nav_clicked(name))
            # 键盘也能用：Tab 到条目，回车/空格等于点击；焦点有高亮环。
            try:
                row.configure(takefocus=1)
                row.bind("<Return>", lambda event, name=key: self._nav_clicked(name))
                row.bind("<space>", lambda event, name=key: self._nav_clicked(name))
                row.bind(
                    "<FocusIn>",
                    lambda event, target=row: target.configure(
                        highlightthickness=1, highlightbackground=theme.ACCENT
                    ),
                )
                row.bind(
                    "<FocusOut>",
                    lambda event, target=row: target.configure(highlightthickness=0),
                )
            except Exception:
                pass
            self.nav_rows[key] = row
            self.nav_icons[key] = icon
            self.nav_texts[key] = text
            if top:
                theme.tooltip(row, "开一段新对话")
        # 定稿图里这个大按钮在导航最上面，按入口顺序建完之后再挪上去。
        new_row = self.nav_rows.get("new")
        children = self.nav.pack_slaves()
        if new_row is not None and children and children[0] is not new_row:
            try:
                new_row.pack_configure(before=children[0])
            except Exception as exc:
                if self.logger:
                    self.logger.warning("nav order failed: %s", exc)
        self._bind_nav_hover()
        return len(self.nav_rows)

    def _bind_nav_hover(self):
        """给导航和它的每个子控件都挂上进出事件：悬停临时展开，移开收回。"""
        stack = [self.nav]
        while stack:
            widget = stack.pop()
            try:
                widget.bind("<Enter>", self._nav_enter, add="+")
                widget.bind("<Leave>", self._nav_leave, add="+")
            except Exception:
                pass
            try:
                stack.extend(widget.winfo_children())
            except Exception:
                continue
        return True
