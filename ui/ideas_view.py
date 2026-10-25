"""灵感建议页：只列有依据的建议，每条都写清为什么提、用了什么、要不要模型。

没有真实依据就不提。忽略只影响这一次；「以后不提这类」才写进偏好。
"""

import tkinter as tk

from ui import panel, theme


class IdeasWindow:
    """灵感建议。数据全部来自宿主上的真实记录。"""

    TITLE = "灵感建议"
    GEOMETRY = (880, 620)

    def __init__(self, pet, parent=None):
        self.pet = pet
        # 嵌进主工作台是一块 Frame，单独打开才是一个窗口。
        self.embedded = parent is not None
        self.win = panel.container(pet, self.TITLE, self.GEOMETRY, parent)
        self.cards = []
        self._build()
        self.refresh()

    # ---------- 搭建 ----------
    def _build(self):
        head = tk.Frame(self.win, bg=theme.BG)
        head.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_MD, theme.PAD_SM))
        theme.label(head, "灵感建议", kind="title").pack(side="left")
        self.state_label = theme.label(head, "", kind="small", fg=theme.MUTED)
        self.state_label.pack(side="left", padx=theme.PAD_MD)
        theme.button(head, "刷新", self.refresh, kind="primary").pack(side="right")
        theme.button(head, "撤销「以后不提」", self.unmute_all).pack(
            side="right", padx=theme.PAD_SM
        )

        theme.label(
            self.win,
            "每条都写清为什么提、用了什么、要不要模型。没有依据的时候，这一页就是空的。",
            kind="small",
            fg=theme.MUTED,
            anchor="w",
        ).pack(fill="x", padx=theme.PAD_MD)

        self.body = theme.scroll_area(self.win)
        self.hint = theme.label(self.win, "", kind="small", fg=theme.MUTED, anchor="w")
        self.hint.pack(fill="x", padx=theme.PAD_MD)
        panel.close_button(self.win, self).pack(pady=theme.PAD_MD)

    # ---------- 数据 ----------
    def refresh(self):
        """重新读建议。读不到就摆空状态，不编内容。"""
        for child in list(self.body.winfo_children()):
            try:
                child.destroy()
            except Exception:
                continue
        try:
            self.cards = list(self.pet.idea_cards() or [])
        except Exception as exc:
            self.cards = []
            self._hint("读建议失败：%s" % exc)
        for card in self.cards:
            self._paint(card)
        if not self.cards:
            empty = theme.card(self.body)
            empty.pack(fill="x", padx=theme.PAD_MD, pady=theme.PAD_SM)
            theme.label(empty, "现在没有可以提的建议", kind="title", bg=theme.CARD).pack(
                anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_SM, 0)
            )
            theme.label(
                empty,
                "等我攒到真东西再说。多聊几句、多安排几个提醒，这里就会自己冒出来。",
                kind="small",
                bg=theme.CARD,
                fg=theme.MUTED,
                anchor="w",
                justify="left",
            ).pack(anchor="w", fill="x", padx=theme.PAD_MD, pady=(0, theme.PAD_SM))
        self._paint_state()
        return self.cards

    def _paint_state(self):
        count = len(self.cards)
        muted = len(self.pet.muted_suggestions()) if hasattr(self.pet, "muted_suggestions") else 0
        text = "现在 %d 条" % count
        if muted:
            text += "，另有 %d 条被设成以后不提" % muted
        try:
            self.state_label.config(text=text)
        except Exception:
            pass
        return text

    def _paint(self, card):
        box = theme.card(self.body)
        box.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_SM, 0))
        theme.label(
            box, str(card.get("title") or ""), kind="title", bg=theme.CARD, fg=theme.TEXT
        ).pack(anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_SM, 0))
        for prefix, field in (
            ("为什么提：", "why"),
            ("用到什么：", "context"),
            ("需要什么：", "requires"),
        ):
            theme.label(
                box,
                prefix + str(card.get(field) or ""),
                kind="small",
                bg=theme.CARD,
                fg=theme.MUTED,
                anchor="w",
                justify="left",
            ).pack(anchor="w", fill="x", padx=theme.PAD_MD)
        row = tk.Frame(box, bg=theme.CARD)
        row.pack(anchor="w", fill="x", padx=theme.PAD_MD, pady=(theme.PAD_XS, theme.PAD_SM))
        label, kind, value = card.get("action") or ("", "", "")
        if label:
            theme.button(row, label, lambda: self.run_action(card)).pack(side="left")
        theme.button(row, "忽略", lambda: self.ignore(card)).pack(
            side="left", padx=theme.PAD_SM
        )
        theme.button(row, "以后不提这类", lambda: self.ignore(card, forever=True)).pack(
            side="left", padx=(0, theme.PAD_SM)
        )
        return box

    # ---------- 动作 ----------
    def run_action(self, card):
        """行动按钮：要么切到真的页面，要么把一句话填进输入框。"""
        label, kind, value = card.get("action") or ("", "", "")
        if kind == "panel":
            return self.pet._chat_open(value)
        if kind == "continue":
            return self.continue_goal(value)
        self._hint("这条建议还没有可点的动作。")
        return None

    def continue_goal(self, goal):
        """接着做：没有任务模型就如实说，不假装已经开工。"""
        try:
            ready = self.pet._task_model_ready()
        except Exception:
            ready = False
        if not ready:
            self._hint("要接着做需要先配好任务模型，配好之后点这里就能继续。")
            return False
        text = "接着做上次没做完的那件事：%s" % str(goal or "")[:40]
        self.pet.prefill_chat(text)
        self._hint("已经把这件事放回输入框，你看一眼再发。")
        return text

    def ignore(self, card, forever=False):
        key = card.get("key")
        self.pet.dismiss_suggestion("idea", key, forever=forever)
        self.refresh()
        self._hint("以后这类不再提。" if forever else "这条先不看了。")
        return key

    def unmute_all(self):
        self.pet.unmute_suggestions()
        self.refresh()
        self._hint("之前设成「以后不提」的建议都恢复了。")
        return True

    # ---------- 给测试看的内容 ----------
    def lines(self):
        """这一页真正写出来的字，一行一条建议。不摆卡片时返回空。"""
        rows = []
        for card in self.cards:
            rows.append(
                "\n".join(
                    [
                        str(card.get("title") or ""),
                        "为什么提：" + str(card.get("why") or ""),
                        "用到什么：" + str(card.get("context") or ""),
                        "需要什么：" + str(card.get("requires") or ""),
                    ]
                )
            )
        return rows

    def text(self):
        """这一页的字，连空状态一起。测试拿它检查页面到底写了什么。"""
        rows = self.lines()
        if rows:
            return rows
        return ["现在没有可以提的建议"]

    def _hint(self, text):
        try:
            self.hint.config(text=str(text or ""))
        except Exception:
            return None
        return text