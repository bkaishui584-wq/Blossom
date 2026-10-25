"""主工作台里各功能页的打开入口。

从 ui/desktop.py 整块搬过来的：只负责把页面挂到中央区，业务状态都留在宿主上。
同名页面只建一次，来回切不丢状态。"""

import tkinter as tk

from ui import chat_view
from ui.awareness_view import AwarenessWindow
from ui.capability_view import CapabilityWindow
from ui.device_view import DeviceWindow
from ui.goals_view import GoalsWindow
from ui.ideas_view import IdeasWindow
from ui.calendar_view import CalendarWindow
from ui.knowledge_view import KnowledgeWindow
from ui.memory_view import MemoryWindow
from ui.schedule_view import ScheduleWindow
from ui.settings_view import SettingsWindow
from ui.skills_view import SkillsWindow
from ui.task_view import TaskWindow
from ui.work_view import WorkWindow


class PanelOpener:
    """混入 DesktopPet。行为与搬之前完全一样。"""

    def open_awareness(self):
        """屏幕感知页：开关、层级、当前应用、排除名单都在这一页。"""
        return self.open_panel(
            "awareness", "屏幕感知", lambda parent: AwarenessWindow(self, parent=parent)
        )

    def open_work(self):
        """工作记录页：默认关闭，显式开启后才采样本机活动。"""
        return self.open_panel(
            "work", "工作记录", lambda parent: WorkWindow(self, parent=parent)
        )

    def open_menu(self, event=None):
        """点一下桌宠才出现的轻量菜单，不是又一个主窗口。"""
        menu = tk.Menu(self.root, tearoff=0)
        for label, action in self.MENU_ITEMS:
            menu.add_command(label=label, command=getattr(self, action))
        x, y = self.host.position()
        if event is not None:
            x, y = event.x_root, event.y_root
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()
        return menu

    def open_skills(self):
        """技能管理，挂在主工作台中央。"""
        return self.open_panel(
            "skills", "技能", lambda parent: SkillsWindow(self, parent=parent)
        )

    def open_home(self, event=None):
        """首页：按最新的真实数据把信息流重摆一遍。"""
        self.close_panel()
        self.show_chat()
        self.chat.set_feed(self.home_feed())
        self.chat.set_tips(self.chat_tips())
        if self.chat.history_text().strip():
            self.chat.set_state(
                chat_view.IDLE, "信息流只摆在空会话上，点「新建对话」看完整的。"
            )
        return True

    def open_ideas(self, event=None):
        """灵感建议：挂在主工作台中央，没有依据就直说没有。"""
        rows = self.idea_cards()
        panel = self.open_panel(
            "ideas", "灵感建议", lambda parent: IdeasWindow(self, parent=parent)
        )
        if panel is not None:
            try:
                panel.refresh()
            except Exception as exc:
                self._warn("ideas refresh failed: %s", exc)
        return rows

    # ---------- 目标与追踪 ----------

    def open_goals(self, event=None):
        """目标与追踪：也进主工作台，只摆任务库里真在追的事。"""
        rows = self.goal_cards()
        panel = self.open_panel(
            "goals", "目标与追踪", lambda parent: GoalsWindow(self, parent=parent)
        )
        if panel is not None:
            try:
                panel.refresh()
            except Exception as exc:
                self._warn("goals refresh failed: %s", exc)
        return rows

    def open_calendar(self, event=None):
        """日历日程：本地 SQLite 里的真实日程，增删都走 CalendarService。"""
        panel = self.open_panel(
            "calendar", "日历日程", lambda parent: CalendarWindow(self, parent=parent)
        )
        if panel is not None:
            try:
                panel.refresh()
            except Exception as exc:
                self._warn("calendar refresh failed: %s", exc)
        return panel

    def open_projects(self, event=None):
        """项目入口：有记录就列出来，没记录就说还没有。"""
        lines = []
        if self.memory is not None:
            try:
                lines = list(self.memory.projects.context_lines() or [])
            except Exception as exc:
                self._warn("projects failed: %s", exc)
                lines = []
        self.show_chat()
        if lines:
            self._append("notice", "记着的项目：\n" + "\n".join(lines[:8]))
        else:
            self._append("notice", "还没有记录项目。聊到具体项目时我会记下来，也可以直接让我记住。")
        return lines

    def open_knowledge(self, event=None):
        """知识库页：加目录、扫描、搜片段。"""
        return self.open_panel(
            "knowledge", "文件与知识", lambda parent: KnowledgeWindow(self, parent=parent)
        )

    def open_panel(self, key, title, factory):
        """把功能页挂到主工作台中央。同一页只建一次，来回切不丢状态。"""
        key = str(key or "").strip()
        if not key:
            return None
        panels = getattr(self, "_panels", None)
        if panels is None:
            panels = self._panels = {}
        panel = panels.get(key)
        alive = False
        if panel is not None:
            try:
                alive = bool(panel.win.winfo_exists())
            except Exception:
                alive = False
        if not alive:
            try:
                body = self.chat.open_page(key, title)
                panel = factory(body)
            except Exception as exc:
                self._warn("panel %s failed: %s", key, exc)
                try:
                    self.chat.close_page()
                except Exception:
                    pass
                self._hint("这一页暂时打不开：%s" % exc)
                panels.pop(key, None)
                return None
            panels[key] = panel
        else:
            self.chat.open_page(key, title)
        try:
            self.chat.register_page(key, panel.win)
        except Exception as exc:
            self._warn("panel %s show failed: %s", key, exc)
        self.show_chat()
        return panel

    def open_settings(self):
        """设置：模型、角色、人格、记忆、语音、权限都进主工作台，不再另开大窗。"""
        return self.open_panel(
            "settings", "设置", lambda parent: SettingsWindow(self, parent=parent)
        )

    def open_persona(self):
        """人格与记忆并成一页，都挂在主工作台中央。

        返回人格那一块：老调用方（含测试）拿到的还是人格面板本身。
        """
        panel = self.open_panel("persona", "人格与记忆", self._persona_page)
        return getattr(panel, "persona", panel)

    def open_wizard(self, force=False):
        """再看一遍首次说明。force=True 表示顺便记成「已看过」。"""
        from ui import wizard

        try:
            window = wizard.FirstRunWizard(self)
        except Exception as exc:
            self._warn("wizard reopen failed: %s", exc)
            return None
        if force:
            wizard.mark_done(self)
        return window

    def open_tasks(self):
        """任务中心：挂在主工作台中央，刷新时只重画自己。"""
        return self.open_panel(
            "tasks", "任务中心", lambda parent: TaskWindow(self, parent=parent)
        )

    def open_schedule(self):
        """计划任务：持久调度、下次执行时间和真实运行记录。"""
        panel = self.open_panel(
            "schedule", "计划任务", lambda parent: ScheduleWindow(self, parent=parent)
        )
        if panel is not None:
            try:
                panel.refresh()
            except Exception as exc:
                self._warn("schedule refresh failed: %s", exc)
        return panel

    def open_memory(self):
        """记忆管理：单独进来时也走中央页面，配置和人格页共用同一份数据。"""
        return self.open_panel(
            "memory", "记忆", lambda parent: MemoryWindow(self, parent=parent)
        )

    def open_capabilities(self):
        """本地能力与权限中心，挂在主工作台中央。"""
        return self.open_panel(
            "capabilities",
            "本地能力与权限",
            lambda parent: CapabilityWindow(self, parent=parent),
        )

    def open_devices(self):
        """设备中心：接了什么、能做什么、刚做了什么。挂进主工作台中央。"""
        return self.open_panel(
            "devices",
            "设备中心",
            lambda parent: DeviceWindow(self, parent=parent),
        )
