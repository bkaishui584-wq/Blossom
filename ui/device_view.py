"""设备中心：接了什么、连上没有、能做什么、刚才做了什么。

页面上的每个数字都来自设备的真实回报。没连上就写未连接，允许切到模拟设备把链路
跑通。界面只提交已登记的动作，能不能做由设备权限服务决定，界面不替它放行。
"""

import threading
import time
import tkinter as tk

from ui import panel, theme

REFRESH_MS = 2500
HISTORY_ROWS = 8


class _Field:
    """一个参数输入框。界面不自己转类型，原样交给动作注册表判。"""

    def __init__(self, kind, variable):
        self.kind = str(kind or "str")
        self.variable = variable

    def value(self):
        raw = self.variable.get()
        if self.kind == "bool":
            return bool(raw)
        text = str(raw).strip()
        return text if text else None


class DeviceWindow:
    """设备中心。数据只读设备栈，写操作一律走动作注册表和权限服务。"""

    TITLE = "设备中心"
    GEOMETRY = (900, 640)

    def __init__(self, pet, parent=None):
        self.pet = pet
        # 嵌进主工作台是一块 Frame，单独打开才是一个窗口。
        self.embedded = parent is not None
        self.win = panel.container(pet, self.TITLE, self.GEOMETRY, parent)
        self.fields = {}
        self._tick = None
        self._build()
        self.refresh_ports()
        self.refresh()
        self._schedule()

    # ---------- 数据源 ----------
    def _stack(self):
        agent = getattr(self.pet, "agent", None)
        return getattr(agent, "device_stack", None)

    def _later(self, fn):
        try:
            self.pet.root.after(0, fn)
        except Exception:
            pass

    # ---------- 搭建 ----------
    def _build(self):
        header = theme.card(self.win)
        header.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_MD, theme.PAD_XS))
        head = tk.Frame(header, bg=theme.CARD)
        head.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_SM, theme.PAD_XS))
        theme.label(head, self.TITLE, kind="title").pack(side="left")
        self.state_label = theme.label(head, "", kind="small", fg=theme.MUTED)
        self.state_label.pack(side="left", padx=theme.PAD_MD)
        # 这个按钮只能让设备把当前动作收掉，不是硬件急停，名字就照实写。
        theme.button(head, "让设备停下", self.emergency_stop, kind="danger").pack(side="right")
        theme.button(head, "模拟设备", self.use_simulator).pack(side="right", padx=theme.PAD_XS)
        theme.button(head, "刷新", self.refresh, kind="primary").pack(side="right", padx=theme.PAD_XS)

        theme.label(
            header,
            "只发登记过的动作，等设备自己确认了才算做完。没连上就是没连上，不猜。",
            kind="small",
            fg=theme.MUTED,
            anchor="w",
        ).pack(fill="x", padx=theme.PAD_MD, pady=(0, theme.PAD_SM))
        theme.cardify(header)

        self._build_serial_row(self.win)

        self.body = theme.scroll_area(self.win)
        self.body.canvas = getattr(self.body, "canvas", None)
        self.hint = theme.label(self.win, "", kind="small", fg=theme.MUTED, anchor="w", justify="left")
        self.hint.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_XS, 0))
        panel.close_button(self.win, self).pack(pady=theme.PAD_SM)

    def _build_serial_row(self, master):
        """串口那一行：列出来、挑一个、接上。没装串口库就照实说。"""
        shell = theme.card(master)
        shell.pack(fill="x", padx=theme.PAD_MD, pady=(0, theme.PAD_XS))
        row = tk.Frame(shell, bg=theme.CARD)
        row.pack(fill="x", padx=theme.PAD_MD, pady=theme.PAD_SM)
        theme.label(row, "串口", kind="small", fg=theme.MUTED).pack(side="left")

        self.port_var = tk.StringVar(master=row, value="")
        self.port_menu = tk.OptionMenu(row, self.port_var, "")
        self.port_menu.configure(
            bg=theme.FIELD,
            fg=theme.TEXT,
            activebackground=theme.FIELD,
            activeforeground=theme.TEXT,
            highlightthickness=0,
            bd=0,
            relief="flat",
            font=theme.FONT_SMALL,
        )
        self.port_menu["menu"].configure(bg=theme.FIELD, fg=theme.TEXT)
        self.port_menu.pack(side="left", padx=theme.PAD_XS)

        theme.button(row, "刷新端口", self.refresh_ports).pack(side="left", padx=theme.PAD_XS)
        theme.button(row, "接上这台", self.use_serial_port, kind="primary").pack(side="left")
        self.port_note = theme.label(row, "", kind="small", fg=theme.MUTED)
        self.port_note.pack(side="left", padx=theme.PAD_SM)
        theme.cardify(shell)

    def _clear(self):
        for child in list(self.body.winfo_children()):
            try:
                child.destroy()
            except Exception:
                continue
        self.fields = {}

    # ---------- 刷新 ----------
    def refresh(self):
        self._clear()
        stack = self._stack()
        if stack is None:
            theme.label(
                self.body,
                "没有接上设备模块。这台机器上设备中心暂时不可用。",
                kind="small",
                fg=theme.MUTED,
                anchor="w",
            ).pack(fill="x", padx=theme.PAD_MD, pady=theme.PAD_SM)
            self._set_state("设备模块未接入")
            return
        try:
            rows = stack.catalog()
        except Exception as exc:
            rows = []
            self._hint("读设备状态失败：%s" % exc)
        self._paint_devices(stack, rows)
        self._paint_actions(stack, rows)
        self._paint_diagnostics(stack, rows)
        self._paint_history(stack)
        self._paint_grants(stack)
        connected = [row for row in rows if row["connected"]]
        self._set_state("接了 %d 台，连着 %d 台" % (len(rows), len(connected)))
        return rows

    def _set_state(self, text):
        try:
            self.state_label.config(text=str(text))
        except Exception:
            pass

    def _paint_devices(self, stack, rows):
        box, body = theme.section(
            self.body, "已接入设备", "类型、连接状态和固件版本都来自设备自己的回报"
        )
        box.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_SM, 0))
        if not rows:
            theme.label(body, "还没有接入任何设备。点「模拟设备」可以先用一台假设备试。", kind="small", fg=theme.MUTED, anchor="w").pack(fill="x")
            theme.cardify(box)
            return []
        for row in rows:
            card = theme.card(body)
            card.pack(fill="x", pady=theme.PAD_XS)
            if row["connected"]:
                theme.button(card, "断开", lambda name=row["name"]: self.disconnect(name)).pack(
                    side="right", padx=theme.PAD_XS, pady=theme.PAD_XS
                )
            else:
                theme.button(
                    card, "连接", lambda name=row["name"]: self.connect(name), kind="primary"
                ).pack(side="right", padx=theme.PAD_XS, pady=theme.PAD_XS)
            theme.label(card, row["name"], kind="title", bg=theme.CARD).pack(
                side="left", padx=theme.PAD_SM, pady=theme.PAD_SM
            )
            detail = " · ".join(
                part
                for part in (
                    "模拟设备" if row["simulated"] else "真机",
                    "已连接" if row["connected"] else "未连接",
                    row.get("port") or row.get("transport") or "",
                    "固件 %s" % row["firmware"] if row.get("firmware") else "固件未知",
                    "能力 %s" % "、".join(row.get("capabilities") or []) if row.get("capabilities") else "没报能力",
                )
                if part
            )
            theme.label(card, detail, kind="small", fg=theme.MUTED, bg=theme.CARD, anchor="w").pack(
                side="left", padx=theme.PAD_SM
            )
            if row.get("last_error"):
                theme.label(
                    card, "上次出错：%s" % row["last_error"], kind="small", fg=theme.ERROR, bg=theme.CARD
                ).pack(side="right", padx=theme.PAD_SM)
        theme.cardify(box)
        return rows

    def _paint_actions(self, stack, rows):
        box, body = theme.section(
            self.body,
            "动作测试",
            "只能挑已登记的动作；参数范围由设备声明，没声明的动作做不了",
        )
        box.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_MD, 0))
        live = [row for row in rows if row["connected"]]
        if not live:
            theme.label(body, "先连上一台设备，这里才会出现能做的动作。", kind="small", fg=theme.MUTED, anchor="w").pack(fill="x")
            theme.cardify(box)
            return
        for row in live:
            theme.label(body, row["name"], kind="body", anchor="w").pack(fill="x", pady=(theme.PAD_XS, 0))
            for action in row.get("actions") or []:
                self._paint_action_row(stack, body, row["name"], action)
        theme.cardify(box)
        return None

    def _paint_action_row(self, stack, master, device, action):
        line = tk.Frame(master, bg=theme.BG)
        line.pack(fill="x", pady=(2, 0))
        name = str(action.get("name") or "")
        available = bool(action.get("available"))
        theme.label(line, name, kind="small", fg=theme.TEXT if available else theme.MUTED, width=20, anchor="w").pack(side="left")
        if not available:
            theme.label(
                line, action.get("reason") or "这台设备做不了", kind="small", fg=theme.MUTED, anchor="w"
            ).pack(side="left", padx=theme.PAD_SM)
            return None
        for param in action.get("params") or []:
            label = str(param.get("label") or param.get("name") or "")
            hint = ""
            if param.get("min") is not None or param.get("max") is not None:
                hint = "%s-%s" % (param.get("min", ""), param.get("max", ""))
            theme.label(line, label, kind="small", fg=theme.MUTED).pack(side="left", padx=(theme.PAD_SM, 2))
            if param.get("kind") == "bool":
                variable = tk.BooleanVar(master=master, value=False)
                tk.Checkbutton(
                    line,
                    variable=variable,
                    bg=theme.BG,
                    activebackground=theme.BG,
                    highlightthickness=0,
                    bd=0,
                    selectcolor=theme.FIELD,
                ).pack(side="left")
            else:
                variable = tk.StringVar(master=master, value=str(param.get("default") or ""))
                widget = theme.entry(line, textvariable=variable, width=6)
                widget.pack(side="left")
                if hint:
                    theme.label(line, hint, kind="small", fg=theme.MUTED).pack(side="left", padx=2)
            self.fields[(device, name, param.get("name"))] = _Field(param.get("kind"), variable)
        if action.get("needs_confirmation"):
            theme.label(line, "要确认", kind="small", fg=theme.WARN).pack(side="left", padx=theme.PAD_SM)
        theme.button(line, "执行", lambda: self.run_action(device, name)).pack(side="right")
        return None

    def _paint_diagnostics(self, stack, rows):
        box, body = theme.section(self.body, "诊断", "设备自报的固件、能力与范围；日志只记设备说的话")
        box.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_MD, 0))
        if not rows:
            theme.label(body, "没有设备可诊断。", kind="small", fg=theme.MUTED, anchor="w").pack(fill="x")
            theme.cardify(box)
            return None
        for row in rows:
            lines = [
                "%s：固件 %s" % (row["name"], row.get("firmware") or "没报"),
                "　能力：%s" % ("、".join(row.get("capabilities") or []) or "没报"),
                "　范围：%s" % self._limits_text(row.get("limits") or {}),
                "　传输：%s%s" % (row.get("transport") or "未知", "（%s）" % row["port"] if row.get("port") else ""),
            ]
            for text in lines:
                theme.label(body, text, kind="small", fg=theme.MUTED, anchor="w").pack(fill="x")
        logs = []
        try:
            logs = stack.logs(limit=12)
        except Exception:
            logs = []
        if logs:
            theme.label(body, "设备端日志（最近 %d 条）" % len(logs), kind="small", fg=theme.MUTED, anchor="w").pack(fill="x", pady=(theme.PAD_XS, 0))
            for text in logs:
                theme.label(body, text, kind="small", fg=theme.MUTED, anchor="w").pack(fill="x")
        theme.cardify(box)
        return None

    def _paint_grants(self, stack):
        box, body = theme.section(
            self.body, "设备授权", "授权按 设备+动作 一条一条给，撤销立刻生效"
        )
        box.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_MD, 0))
        try:
            grants = stack.permissions.action_grants()
        except Exception as exc:
            grants = []
            self._hint("读设备授权失败：%s" % exc)
        if not grants:
            theme.label(
                body, "还没有给过设备授权。需要确认的动作每次都问一次。",
                kind="small", fg=theme.MUTED, anchor="w",
            ).pack(fill="x")
            theme.cardify(box)
            return None
        for grant in grants:
            line = tk.Frame(body, bg=theme.BG)
            line.pack(fill="x", pady=(2, 0))
            parts = str(grant.get("tool") or "").split(":", 2)
            device = parts[1] if len(parts) > 1 else "?"
            action = parts[2] if len(parts) > 2 else "?"
            theme.label(
                line, "%s · %s（%s）" % (device, action, grant.get("mode") or ""),
                kind="small", anchor="w",
            ).pack(side="left")
            theme.button(line, "撤销", lambda item=grant: self.revoke_grant(item)).pack(side="right")
        theme.button(body, "全部撤销", self.revoke_all_grants).pack(anchor="w", pady=(theme.PAD_XS, 0))
        theme.cardify(box)
        return None

    def revoke_grant(self, grant):
        """撤销一条授权。按 设备+动作 精确撤，不牵连别的。"""
        stack = self._stack()
        if stack is None:
            return None
        parts = str(grant.get("tool") or "").split(":", 2)
        device = parts[1] if len(parts) > 1 else ""
        action = parts[2] if len(parts) > 2 else ""
        if not device or not action:
            self._hint("这条授权认不出来，没撤。")
            return None
        removed = stack.permissions.revoke(device, action)
        self._hint("已撤销 %d 条授权。" % removed if removed else "这条授权已经不在了。")
        self.refresh()
        return removed

    def revoke_all_grants(self):
        stack = self._stack()
        if stack is None:
            return None
        removed = stack.permissions.revoke()
        self._hint("已撤销 %d 条设备授权。" % removed)
        self.refresh()
        return removed

    def _limits_text(self, limits):
        if not limits:
            return "设备没有声明范围"
        parts = []
        for key, value in limits.items():
            if isinstance(value, dict):
                parts.append("%s %s-%s" % (key, value.get("min"), value.get("max")))
            elif isinstance(value, (list, tuple)):
                parts.append("%s %s" % (key, "、".join(str(item) for item in value)))
        return "；".join(parts) or "设备没有声明范围"

    def _paint_history(self, stack):
        box, body = theme.section(self.body, "最近执行", "只记真实结果：完成、失败、超时、取消，都在这里")
        box.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_MD, theme.PAD_MD))
        try:
            rows = stack.tasks.recent(limit=HISTORY_ROWS)
        except Exception as exc:
            theme.label(body, "读执行记录失败：%s" % exc, kind="small", fg=theme.MUTED, anchor="w").pack(fill="x")
            theme.cardify(box)
            return None
        if not rows:
            theme.label(body, "还没有执行过设备动作。", kind="small", fg=theme.MUTED, anchor="w").pack(fill="x")
            theme.cardify(box)
            return None
        for row in rows:
            when = row.get("finished_wall") or row.get("created_wall")
            stamp = time.strftime("%H:%M:%S", time.localtime(when)) if when else "--:--:--"
            text = "%s  %s  %s  %s" % (stamp, row.get("device"), row.get("action"), row.get("status_label") or row.get("status"))
            if row.get("result"):
                text += "  %s" % row["result"]
            if row.get("error"):
                text += "  %s" % row["error"]
            theme.label(body, text, kind="small", fg=theme.MUTED, anchor="w").pack(fill="x")
        theme.cardify(box)
        return None

    # ---------- 动作 ----------
    def connect(self, name):
        stack = self._stack()
        if stack is None:
            return None
        self._hint("正在连接 %s…" % name)

        def work():
            try:
                result = stack.manager.connect(name)
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            self._later(lambda: self._connected(name, result))

        threading.Thread(target=work, daemon=True).start()
        return None

    def _connected(self, name, result):
        if result.get("ok"):
            self._hint("已连接 %s。" % name)
        else:
            self._hint("%s 没连上：%s" % (name, result.get("error") or "设备没有回应"))
        self.refresh()

    def disconnect(self, name):
        stack = self._stack()
        if stack is None:
            return None
        result = stack.manager.disconnect(name)
        self._hint("已断开 %s。" % name if result.get("ok") else "断开失败：%s" % result.get("error"))
        self.refresh()
        return result

    def use_simulator(self):
        """接一台模拟设备。没有硬件时靠它把整条链路跑通。"""
        stack = self._stack()
        if stack is None:
            return None
        from devices.stack import register_simulator

        result, provider = register_simulator(
            stack.manager, registry=stack.actions, on_event=stack.event_handler
        )
        if not result.get("ok"):
            self._hint("模拟设备没装上：%s" % result.get("error"))
            return None
        outcome = stack.manager.connect(provider.name)
        self._hint("模拟设备 %s 已就绪，可以在这里试动作。" % provider.name if outcome.get("ok") else "模拟设备没连上：%s" % outcome.get("error"))
        self.refresh()
        return outcome

    # ---------- 串口 ----------
    def refresh_ports(self):
        """列出本机认得的串口。列不出来就照实说，不编一个口出来。"""
        from devices import ports

        try:
            rows = ports.list_ports()
        except Exception as exc:
            rows = []
            self._hint("列串口失败：%s" % exc)
        self.ports = rows
        menu = self.port_menu["menu"]
        menu.delete(0, "end")
        if not rows:
            self.port_var.set("")
            menu.add_command(label="没发现串口", command=lambda: None)
            self.port_note.config(text="没发现串口（%s）" % ports.backend_name())
            return []
        for row in rows:
            port = row["port"]
            text = port if not row.get("description") else "%s %s" % (port, row["description"])
            menu.add_command(label=text, command=lambda value=port: self.port_var.set(value))
        self.port_var.set(rows[0]["port"])
        self.port_note.config(text="共 %d 个（%s）" % (len(rows), ports.backend_name()))
        return rows

    def use_serial_port(self):
        """把选中的串口接成一台真机。

        要过两道：设备权限服务的连接授权，以及 DeviceManager 的 allow_real 闸门。
        哪一道没过都照实说，不偷偷连。
        """
        stack = self._stack()
        if stack is None:
            return None
        port = str(self.port_var.get() or "").strip()
        known = {row["port"] for row in getattr(self, "ports", []) or []}
        if not port or port not in known:
            self._hint("先挑一个列出来的端口。")
            return None
        allowed, _needs_confirmation, _level, why = stack.permissions.check_connect(port)
        if not allowed:
            self._hint("这个口没接：%s" % (why or "没有授权"))
            return None
        self._hint("正在接 %s…" % port)

        def work():
            try:
                result = self._attach_serial(port)
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            self._later(lambda: self._connected(port, result))

        threading.Thread(target=work, daemon=True).start()
        return None

    def _attach_serial(self, port):
        """真的去开串口。名字按端口起，同一个口接两次不会变成两台。"""
        from devices.provider import SerialProvider, safe_device_id

        stack = self._stack()
        name = "esp32-%s" % safe_device_id(port, fallback="port").lower()
        provider = stack.manager.get(name)
        if provider is None:
            provider = SerialProvider(
                name=name,
                port=port,
                registry=stack.actions,
                on_event=stack.event_handler,
            )
            outcome = stack.manager.register(provider)
            if not outcome.get("ok"):
                return outcome
        return stack.manager.connect(name)

    def run_action(self, device, action):
        stack = self._stack()
        if stack is None:
            return None
        params = {}
        for (dev, act, key), field in list(self.fields.items()):
            if dev != device or act != action:
                continue
            params[key] = field.value()
        try:
            result = stack.tasks.submit(
                device=device, action=action, params=params, source="设备中心", wait=False
            )
        except Exception as exc:
            self._hint("这一步没提交成功：%s" % exc)
            return None
        if not result.get("ok") and result.get("status") != "queued":
            self._hint("%s：%s" % (action, result.get("error") or "没做成"))
        else:
            self._hint("%s 已交给设备，结果出来会记在下面。" % action)
        self.refresh()
        return result

    def emergency_stop(self):
        stack = self._stack()
        if stack is None:
            return None
        self._hint("正在让设备停下…")

        def work():
            try:
                report = stack.tasks.emergency_stop()
            except Exception as exc:
                report = {"devices": [{"device": "?", "ok": False, "error": str(exc)}]}
            self._later(lambda: self._stopped(report))

        threading.Thread(target=work, daemon=True).start()
        return None

    def _stopped(self, report):
        parts = []
        for item in report.get("devices") or []:
            parts.append("%s：%s" % (item.get("device"), "已停下" if item.get("ok") else item.get("error") or "停不了"))
        self._hint(
            "让设备停下 —— "
            + ("；".join(parts) if parts else "现在没有连着的设备")
            + "。这只是让固件把当前动作收掉，不是断电急停。"
        )
        self.refresh()

    # ---------- 小工具 ----------
    def _hint(self, text):
        try:
            self.hint.config(text=str(text))
        except Exception:
            pass
        return text

    def _schedule(self):
        """隔一会儿刷新一次，让排队和运行中的状态自己走过来。"""
        try:
            self._tick = self.win.after(REFRESH_MS, self._tick_once)
        except Exception:
            self._tick = None
        return None

    def _tick_once(self):
        """到点自己刷一次。页面被收起来时不重建控件，只留着定时器等回来。"""
        try:
            if not self.win.winfo_exists():
                return None
        except Exception:
            return None
        if self._on_screen():
            try:
                self.refresh()
            except Exception:
                pass
        self._schedule()
        return None

    def _on_screen(self):
        """这一页现在是不是真的摆在用户面前。读不到状态就照常刷。"""
        chat = getattr(self.pet, "chat", None)
        checker = getattr(chat, "page_is_open", None)
        if not callable(checker):
            return True
        try:
            if not checker():
                return False
            return str(getattr(chat, "page_key", "")) == "devices"
        except Exception:
            return True
