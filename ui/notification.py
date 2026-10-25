"""Independent desktop reminder card for local calendar events."""

import tkinter as tk

from ui import glass_native, theme


class ReminderPopup:
    """A small always-on-top card with real completion/postpone actions."""

    WIDTH = 390
    HEIGHT = 230

    def __init__(self, owner, payload, offset=0):
        self.owner = owner
        self.payload = dict(payload or {})
        self.closed = False
        self.win = tk.Toplevel(owner.root)
        self.win.overrideredirect(True)
        self.win.configure(bg=theme.BG)
        self.win.attributes("-topmost", True)
        try:
            glass_native.apply_window(
                self.win, radius=theme.RADIUS_LG, alpha=0.99, tint=theme.CARD
            )
        except Exception:
            pass
        card = theme.card(self.win)
        card.pack(fill="both", expand=True, padx=10, pady=10)
        theme.label(card, "日程提醒", kind="small", fg=theme.ACCENT, bg=theme.CARD).pack(
            anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_SM, 0)
        )
        theme.label(
            card,
            str(self.payload.get("title") or "未命名日程"),
            kind="title",
            bg=theme.CARD,
            wraplength=self.WIDTH - 50,
            justify="left",
        ).pack(anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_XS, 0))
        when = str(self.payload.get("when_at") or "")
        end = str(self.payload.get("end_at") or "")
        when_text = when if not end else "%s - %s" % (when[11:16], end[11:16])
        theme.label(card, when_text, kind="small", fg=theme.MUTED, bg=theme.CARD).pack(
            anchor="w", padx=theme.PAD_MD
        )
        note = str(self.payload.get("note") or "").strip()
        if note:
            theme.label(
                card, note[:120], kind="small", fg=theme.MUTED, bg=theme.CARD,
                wraplength=self.WIDTH - 50, justify="left",
            ).pack(anchor="w", padx=theme.PAD_MD, pady=(theme.PAD_XS, 0))

        row = tk.Frame(card, bg=theme.CARD)
        row.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_SM, 0))
        theme.button(row, "完成", lambda: self._act("complete"), kind="primary").pack(
            side="left"
        )
        theme.button(row, "稍后 10 分钟", lambda: self._act("snooze")).pack(
            side="left", padx=theme.PAD_XS
        )
        theme.button(row, "延期 15 分钟", lambda: self._act("delay")).pack(side="left")

        row2 = tk.Frame(card, bg=theme.CARD)
        row2.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_XS, theme.PAD_SM))
        theme.button(row2, "打开详情", lambda: self._act("open")).pack(side="left")
        theme.button(row2, "忽略", lambda: self._act("ignore"), kind="ghost").pack(
            side="right"
        )
        self.win.bind("<Escape>", lambda event: self._act("ignore"))
        self._place(offset)

    def _place(self, offset=0):
        try:
            self.win.update_idletasks()
            x = self.win.winfo_screenwidth() - self.WIDTH - 24
            y = self.win.winfo_screenheight() - self.HEIGHT - 76 - int(offset) * 16
            self.win.geometry("%dx%d+%d+%d" % (self.WIDTH, self.HEIGHT, x, max(20, y)))
        except Exception:
            self.win.geometry("%dx%d" % (self.WIDTH, self.HEIGHT))
        self.win.deiconify()
        self.win.lift()

    def _act(self, action):
        if self.closed:
            return None
        self.closed = True
        handler = getattr(self.owner, "handle_reminder_action", None)
        try:
            if callable(handler):
                handler(action, self.payload)
        finally:
            self.destroy()
        return action

    def destroy(self):
        self.closed = True
        try:
            self.win.destroy()
        except Exception:
            pass


__all__ = ["ReminderPopup"]
