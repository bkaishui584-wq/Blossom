"""主工作台与 Agent 之间的接线：会话、消息、输入、工具结果都从这里走。

从 ui/desktop.py 整块搬过来的。需要弹对话框的会话操作仍留在 desktop.py。"""

import tkinter as tk
from pathlib import Path

from agent.router import MODE_LABELS
from ui import chat_view


class ChatWiring:
    """混入 DesktopPet。行为与搬之前完全一样。"""

    def _chat_was_open(self):
        saved = self._preference("chat.open")
        if saved is None or saved == "":
            return False
        return str(saved).strip() == "1"

    # ---------- 显示比例换算 ----------

    def _chat_status_detail(self):
        """状态详情：点开才看，普通界面不再常驻这些东西。"""
        lines = [self._status_full_text(), ""]
        for name, value in self.developer_rows():
            lines.append("%s：%s" % (name, value))
        body = "\n".join(str(item) for item in lines)
        self._show_detail_window("状态详情", body)
        return body

    def _chat_event(self, name, payload=None):
        """聊天窗口的所有按钮都从这里进来，统一转成真实动作。"""
        handlers = {
            "nav": self._chat_nav,
            "nav_toggle": self._chat_nav_toggle,
            "card": self._chat_card,
            "model_menu": self._chat_model_menu,
            "search": self._chat_search,
            "session_menu": self._chat_session_menu,
            "session_rename": self._chat_session_rename,
            "feedback": self._chat_feedback,
            "edit_resend": self._chat_edit_resend,
            "regenerate": self._chat_regenerate,
            "remove_attachment": self._chat_remove_attachment,
            "command": self._chat_command,
            "permission": self._chat_permission_answer,
            "voice": self._chat_voice,
            "web_search": self._chat_web_search,
            "minimize": self._chat_minimize,
            "maximize": self._chat_maximize,
            "status_detail": self._chat_status_detail,
            "mode_toggle": self._chat_mode_toggle,
            "attach_image": self.attach_image,
            "knowledge": self.open_knowledge,
            "open": self._chat_open,
            "dismiss": self.dismiss_feed,
        }
        handler = handlers.get(str(name))
        if handler is None:
            return None
        try:
            return handler(payload)
        except Exception as exc:
            self._warn("chat event %s failed: %s", name, exc)
            return None

    def _chat_status_labels(self):
        """顶栏的运行模式和模型状态，和状态行用的是同一批真实数据。"""
        agent = self.agent
        mode_name = getattr(agent, "mode_name", "")
        provider = None
        try:
            provider = agent.provider_for(self.session_id)
        except Exception:
            provider = None
        model = getattr(provider, "model", "") or self.config.companion_model
        try:
            self.chat.set_mode(MODE_LABELS.get(mode_name, mode_name) or "本地")
            self.chat.set_model("模型：%s" % (model or "未配置"))
        except Exception as exc:
            self._warn("chat status labels failed: %s", exc)
            return None
        return mode_name, model

    def _chat_nav_toggle(self, expanded):
        if not getattr(self, "_chat_ready", False):
            return None
        self._save_preference("chat.nav", "1" if expanded else "0")
        return expanded

    def _chat_nav(self, key):
        """左侧导航的十条路，能打开真面板就打开，没有的就直说。"""
        routes = {
            "home": self.open_home,
            "new": self.new_session,
            "search": self._chat_search,
            "calendar": self.open_calendar,
            "ideas": self.open_ideas,
            "goals": self.open_goals,
            "work": self.open_work,
            "projects": self.open_projects,
            "tasks": self.open_tasks,
            "skills": self.open_skills,
            "knowledge": self.open_knowledge,
            "persona": self.open_persona,
            "capabilities": self.open_capabilities,
            "devices": self.open_devices,
            "awareness": self.open_awareness,
            "settings": self.open_settings,
        }
        handler = routes.get(str(key))
        if handler is None:
            return None
        return handler()

    def _chat_card(self, key):
        """空会话的能力卡片：把话填进输入框，绝不代替用户发送。"""
        name = str(key or "")
        if name == "task":
            return self._chat_mode_toggle(True)
        if name == "persona":
            self.show_chat()
            return self.open_persona()
        prompt = self.CARD_PROMPTS.get(name)
        if not prompt:
            return None
        self.chat.insert_input(prompt)
        self.chat.focus_input()
        self.show_chat()
        return prompt

    def _chat_open(self, target):
        """信息流卡片上的入口：只开真实存在的界面。"""
        routes = {
            "home": self.open_home,
            "calendar": self.open_calendar,
            "tasks": self.open_tasks,
            "capabilities": self.open_capabilities,
            "devices": self.open_devices,
            "awareness": self.open_awareness,
            "persona": self.open_persona,
            "memory": self.open_persona,
            "skills": self.open_skills,
            "ideas": self.open_ideas,
            "goals": self.open_goals,
            "work": self.open_work,
        }
        handler = routes.get(str(target or ""))
        if handler is None:
            return None
        return handler()

    def _chat_mode_toggle(self, flag=None):
        """聊天 / 任务视图开关。只决定看到什么，不动执行规则和权限。"""
        if flag is None:
            flag = not bool(getattr(self, "_task_view_on", False))
        self._task_view_on = bool(flag)
        self.show_chat()
        self._save_preference("chat.task_view", "1" if self._task_view_on else "0")
        self._save_preference("chat.panel", "1" if self._task_view_on else "0")
        self.chat.set_task_mode(self._task_view_on)
        if self._task_view_on:
            self.chat.set_panel_text(self._task_panel_text())
        return self._task_view_on

    def _chat_model_menu(self, payload=None):
        """模型菜单里的每一行都是真实配置，不写「已连接」这种假状态。"""
        agent = self.agent
        companion = getattr(agent, "companion_slot", None)
        task = getattr(agent, "task_slot", None)
        menu = tk.Menu(self.card, tearoff=0)
        for label, slot, fallback in (
            ("陪伴", companion, self.config.companion_model),
            ("任务", task, self.config.task_model),
        ):
            name = slot.model if slot is not None else fallback
            ready = bool(slot.available()) if slot is not None else False
            menu.add_command(
                label="%s模型：%s（%s）" % (label, name or "未配置", "可用" if ready else "未配置"),
                state="disabled",
            )
        menu.add_separator()
        menu.add_command(
            label="任务确认：%s" % ("开" if self.config.task_confirm else "关"), state="disabled"
        )
        # 真实的模型切换：列出启用中的配置，点一条只改当前会话。
        repo = getattr(self.config, "model_configs", None)
        rows = repo.enabled("companion") if repo is not None else []
        if rows:
            menu.add_separator()
            try:
                current = self.agent.session_model(self.session_id)
            except Exception:
                current = ""
            default_row = repo.default("companion") if repo is not None else None
            default_id = str((default_row or {}).get("id") or "")
            for row in rows:
                active = current == str(row.get("id")) or (
                    not current and str(row.get("id")) == default_id
                )
                menu.add_command(
                    label="%s %s（%s）"
                    % ("✓" if active else "　", row.get("name", ""), row.get("model", "")),
                    command=lambda cid=str(row.get("id")): self._chat_use_model(cid),
                )
            if current:
                menu.add_command(label="跟随默认模型", command=lambda: self._chat_use_model(""))
            menu.add_separator()
        menu.add_command(label="打开模型设置…", command=self.open_settings)
        try:
            button = self.chat.model_button
            menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())
        finally:
            menu.grab_release()
        return menu

    def _chat_use_model(self, config_id):
        """切换当前会话使用的模型。只影响这个会话，别的会话不动。"""
        try:
            ok = self.agent.set_session_model(self.session_id, config_id)
        except Exception as exc:
            self._warn("switch model failed: %s", exc)
            ok = False
        if not ok:
            self._hint("这个模型配置不可用，先在「设置 → 模型」里测试一下。")
            return False
        try:
            self._refresh_status()
            self._chat_status_labels()
        except Exception:
            pass
        if config_id:
            row = self.config.model_configs.get(config_id) if hasattr(self.config, "model_configs") else None
            name = (row or {}).get("name") or config_id
            self._hint("当前会话改用：%s" % name)
        else:
            self._hint("当前会话跟随默认模型。")
        return True

    def _chat_target_session(self, session_id):
        return str(session_id or "").strip() or self.session_id

    def _chat_session_menu(self, payload=None):
        """会话列表上的操作菜单，点谁就对谁。"""
        session_id = self._chat_target_session(self.chat.selected_session())
        row = {}
        if self.memory is not None:
            row = self.memory.session_meta(session_id) or {}
        menu = tk.Menu(self.card, tearoff=0)
        menu.add_command(label="重命名…", command=lambda: self._chat_session_rename(session_id))
        menu.add_command(
            label="取消置顶" if row.get("pinned") else "置顶",
            command=lambda: self._chat_session_pin(session_id, not bool(row.get("pinned"))),
        )
        menu.add_command(
            label="取消归档" if row.get("archived") else "归档",
            command=lambda: self._chat_session_archive(session_id, not bool(row.get("archived"))),
        )
        menu.add_command(label="编辑标签…", command=lambda: self._chat_session_tags(session_id))
        menu.add_separator()
        menu.add_command(
            label="导出这个会话…", command=lambda: self._chat_session_export(session_id)
        )
        menu.add_command(
            label="不显示已归档" if self._show_archived else "显示已归档",
            command=self._chat_toggle_archived,
        )
        menu.add_separator()
        menu.add_command(
            label="删除这个会话…", command=lambda: self._chat_session_delete(session_id)
        )
        try:
            button = self.chat.session_menu_button
            menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())
        finally:
            menu.grab_release()
        return session_id

    def _chat_session_pin(self, session_id=None, pinned=True):
        session_id = self._chat_target_session(session_id)
        if self.memory is not None:
            self.memory.pin_session(session_id, bool(pinned))
        self.refresh_sessions()
        return bool(pinned)

    def _chat_session_archive(self, session_id=None, archived=True):
        session_id = self._chat_target_session(session_id)
        if self.memory is not None:
            self.memory.archive_session(session_id, bool(archived))
        if archived and session_id == self.session_id:
            self._append("hint", "这个会话已归档，消息都还在，随时能切回来。")
        self.refresh_sessions()
        return bool(archived)

    def _chat_toggle_archived(self):
        self._show_archived = not bool(self._show_archived)
        self.refresh_sessions()
        return self._show_archived

    def _chat_feedback(self, useful):
        """有用/没用都写进记忆，重启之后还在。"""
        last = getattr(self, "last_reply", None)
        body = getattr(last, "content", "") if last is not None else ""
        if isinstance(last, dict):
            body = last.get("content") or ""
        verdict = "有用" if useful else "没用"
        quote = str(body or self._last_user or "")[:60]
        if self.memory is not None and quote:
            self.memory.remember(
                "用户觉得「%s」这条回复%s。" % (quote, verdict),
                kind="feedback",
                source="USER_EXPLICIT",
                confirmed=True,
            )
        self.chat.set_state(chat_view.OK if useful else chat_view.WARN, "已记下你的反馈")
        return verdict

    def _chat_edit_resend(self, payload=None):
        """把上一条自己的话放回输入框，改完按 Enter 重发。"""
        text = self._last_user
        if not text and self.memory is not None:
            text = self.memory.last_exchange(self.session_id)[0]
        if not text:
            self.chat.set_state(chat_view.WARN, "还没有可以改的消息")
            return None
        self.chat.insert_input(text)
        self.chat.focus_input()
        self.chat.set_state(chat_view.IDLE, "改好按 Enter 重发")
        return text

    def _chat_regenerate(self, payload=None):
        """再想一次：不重复贴用户消息，只重跑这一轮回复。"""
        text = self._last_user
        if not text and self.memory is not None:
            text = self.memory.last_exchange(self.session_id)[0]
        if not text or self._busy:
            return False
        self._last_user = text
        self.last_reply = None
        self._set_busy(True)
        self._new_worker(self._worker_reply, text, self._next_reply_token())
        return True

    def _chat_add_attachment(self, path):
        """附件只记本地路径和真实状态，没解析就不假装解析过。"""
        path = str(path or "").strip()
        if not path:
            return None
        exists = Path(path).is_file()
        row = {
            "id": path,
            "path": path,
            "name": Path(path).name,
            "state": "pending" if exists else "error",
        }
        self._attachments = [item for item in self._attachments if item["id"] != path]
        self._attachments.append(row)
        self.chat.set_attachments(self._attachments)
        return row

    def _chat_remove_attachment(self, ident):
        self._attachments = [item for item in self._attachments if item["id"] != str(ident)]
        self.chat.set_attachments(self._attachments)
        return len(self._attachments)

    def _chat_command(self, word):
        word = str(word or "").strip()
        if not word:
            return None
        lowered = word.lower()
        if lowered in ("new", "新建", "新建对话"):
            return self.new_session()
        if lowered in ("search", "搜索", "搜索历史"):
            return self._chat_search()
        if lowered in ("tasks", "任务", "任务中心"):
            return self.open_tasks()
        if lowered in ("skills", "技能"):
            return self.open_skills()
        if lowered in ("settings", "设置"):
            return self.open_settings()
        if lowered in ("clear", "清空", "清空当前对话"):
            return self._clear_chat()
        if ":" in word and word.split(":", 1)[0].strip().lower() in ("skill", "技能"):
            name = word.split(":", 1)[1].strip()
            return self.use_skill(name) if name else None
        # 认不出的命令不猜，直接把原文当话发出去。
        self.chat.insert_input(word)
        return self._send()

    def _chat_permission_answer(self, approved):
        holder = getattr(self, "_permission_wait", None)
        if holder is None:
            return None
        holder["ok"] = bool(approved)
        holder["event"].set()
        self.chat.set_state(
            chat_view.OK if approved else chat_view.WARN,
            "已允许这次操作" if approved else "已拒绝这次操作",
        )
        return bool(approved)

    def _chat_voice(self, payload=None):
        voice = getattr(self, "voice", None)
        if voice is None or not voice.available():
            self.chat.set_state(chat_view.WARN, "没有检测到可用的语音输入")
            return None
        started = voice.start()
        self.chat.set_state(chat_view.IDLE, "正在听…" if started else "麦克风打不开")
        return started

    def _chat_web_search(self, payload=None):
        """没接搜索服务就不装样子，写清原因。"""
        self.chat.set_state(chat_view.WARN, "没有配置搜索服务，这一版先用本地工具")
        return None

    def _chat_minimize(self, payload=None):
        """最小化到任务栏：图标留着，点一下能回来。只有 × 才真正收掉。"""
        from ui import pet_host

        if pet_host.minimize_window(self.card):
            self._chat_minimized = True
            self.chat_visible = True
            self._save_preference("chat.open", "1")
            return True
        # 极少数系统上原生最小化失败：安静收起，至少不报错。
        return self.hide_chat()

    def _chat_maximize(self, payload=None):
        if getattr(self, "_chat_minimized", False):
            from ui import pet_host

            pet_host.restore_window(self.card)
            self._chat_minimized = False
        if getattr(self, "_maxed", False):
            self._maxed = False
            self._card_size = self._saved_card_size()
            self._place_card()
            return False
        try:
            width = int(self.root.winfo_screenwidth())
            height = int(self.root.winfo_screenheight())
        except Exception:
            return False
        self._maxed = True
        self._card_size = (width, height)
        self.card.geometry("%dx%d+0+0" % (width, height))
        return True
