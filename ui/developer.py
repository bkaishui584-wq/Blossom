"""开发者诊断面板：只读状态，不动任何业务数据。

从 ui/desktop.py 整块搬过来的。DesktopPet 通过混入拿到这些方法，
self 上的东西和原来一模一样，所以行为不变、测试不变。
"""

from agent.router import MODE_LABELS
from agent.task import TASK_STATE_LABELS


class DeveloperPanel:
    """混入 DesktopPet：把程序状态拼成给人看的一行行。"""

    def _live2d_state_text(self):
        if self.live2d is None:
            provider = self._avatar_provider()
            if provider is None:
                return "未启用"
            return "没有可用的渲染环境"
        try:
            status = self.live2d.get_status()
        except Exception as exc:
            self._warn("live2d status failed: %s", exc)
            return "状态未知"
        mapping = {
            "idle": "还没加载",
            "starting": "正在加载",
            "ready": "已显示",
            "failed": "加载失败",
            "stopped": "已停止",
        }
        text = mapping.get(str(status.get("state") or ""), "未知")
        error = status.get("error")
        return f"{text}（{error}）" if error else text

    def _live2d_backend_text(self):
        if self.live2d is None:
            return "透明载体"
        try:
            status = self.live2d.get_status()
        except Exception:
            status = {}
        backend = status.get("backend") or "webgl"
        core = status.get("core") or ""
        version = ""
        if core:
            try:
                raw = int(core)
                version = f"{raw >> 24}.{(raw >> 16) & 0xFF}.{raw & 0xFFFF:04d}"
            except (TypeError, ValueError):
                version = str(core)
        return f"独立透明窗口（{backend}" + (f" · Cubism Core {version}" if version else "") + "）"

    def _live2d_webview2_text(self):
        """画面能不能起来，一半取决于系统里的 WebView2。查清了的结论才写出来。"""
        try:
            from avatar.web2d import webview2_note

            return webview2_note()
        except Exception as exc:
            self._warn("webview2 probe failed: %s", exc)
            return "状态未知"

    def developer_rows(self):
        """开发者面板的内容。全是程序状态，绝不发送给模型。"""
        agent = self.agent
        rows = []

        def add(name, reader):
            try:
                rows.append((name, reader()))
            except Exception as exc:
                rows.append((name, f"读取失败：{exc}"))

        add("运行模式", lambda: MODE_LABELS.get(agent.mode_name, agent.mode_name))
        add("最近意图", lambda: getattr(agent, "last_intent", "") or "无")
        add("任务状态", lambda: TASK_STATE_LABELS.get(agent.task_state, agent.task_state))
        add("陪伴模型", lambda: self._slot_label(getattr(agent, "companion_slot", None)))
        add("任务模型", lambda: self._slot_label(getattr(agent, "task_slot", None)))
        add("角色后端", self._live2d_backend_text)
        add("角色状态", self._live2d_state_text)
        add("角色包", self._character_text)
        add("记忆条数", self._memory_stats_text)
        add("累计 Token", self._token_text)
        add("主动行为", self._mode_row)
        add("生命状态", self._life_text)
        add("隐私开关", self._privacy_text)
        add("设备", self._device_text)
        add("技能", self._skills_text)
        add("界面尺寸", lambda: "%d×%d" % self.host.size())
        add("桌宠位置", lambda: "%d,%d" % self.host.position())
        add("任务记录", self._task_rows)
        rows.extend(self._agent_diag_rows())
        return rows

    def _pending_tasks_text(self):
        """待续任务只报事实，不替用户做决定。"""
        rows = self.pending_tasks[:3]
        items = "；".join(
            "%s（%s）" % (str(item.goal)[:30], item.state_label) for item in rows
        )
        extra = "" if len(self.pending_tasks) <= 3 else "等 %d 条" % len(self.pending_tasks)
        line = "上次还有没做完的事：%s%s。要接着做就说一声。" % (items, extra)
        capsule = self.continuity_capsule()
        if capsule and capsule.get("next_step"):
            line += " 上次那件下一步是%s。" % capsule["next_step"]
        return line

    def _agent_diag_rows(self):
        """本轮诊断：只给开发者看，普通界面不显示。"""
        diagnostics = getattr(self.agent, "diagnostics", None)
        if diagnostics is None:
            return []
        try:
            return list(diagnostics.rows())
        except Exception as exc:
            self._warn("diagnostics rows failed: %s", exc)
            return [("本轮诊断", "读取失败")]

    def _task_rows(self):
        store = getattr(self.agent, "task_store", None)
        if store is None:
            return "没有接任务库"
        try:
            stats = store.stats()
        except Exception as exc:
            return "读取失败：%s" % exc
        return "共 %(total)d 个，完成 %(completed)d 个，待续 %(unfinished)d 个" % stats

    def _character_text(self):
        manager = getattr(self.agent, "characters", None)
        if manager is None:
            return "未接入"
        try:
            data = manager.describe() or {}
        except Exception as exc:
            self._warn("character describe failed: %s", exc)
            return "读取失败"
        installed = [str(item) for item in (data.get("installed") or [])]
        if not installed:
            return "还没有角色包"
        return f"{len(installed)} 个（{'、'.join(installed)}），可用动作 {data.get('actions')} 个"

    def _slot_label(self, slot):
        if slot is None:
            return "未配置"
        return getattr(slot, "label", "") or getattr(slot, "model", "") or "未配置"

    def _memory_stats_text(self):
        if self.memory is None:
            return "未接入（无记忆对话）"
        data = self.memory.stats() or {}
        parts = [f"{data.get('total', 0)} 条"]
        if data.get("archived"):
            parts.append(f"已归档 {data['archived']}")
        if data.get("capacity"):
            parts.append(f"容量 {data['capacity']}")
        return "，".join(parts)

    def _token_text(self):
        if self.memory is None:
            return "未接入"
        totals = self.memory.usage_totals() or {}
        return "输入 %s / 输出 %s / 合计 %s" % (
            totals.get("prompt_tokens", 0),
            totals.get("completion_tokens", 0),
            totals.get("total_tokens", 0),
        )

    def _privacy_text(self):
        if self.privacy is None:
            return "未接入"
        data = self.privacy.describe() or {}
        on = [str(key) for key, value in data.items() if value]
        return "、".join(on) if on else "全部关闭"

    def _device_text(self):
        summary = getattr(self.agent, "_device_summary", None)
        if summary is None:
            return "未接入"
        data = summary() or {}
        names = list(data.get("names") or [])
        if not names:
            return "没有设备"
        tail = "（允许真机）" if data.get("allow_real") else "（仅模拟）"
        return "、".join(str(name) for name in names) + tail

    def _mode_row(self):
        """现在是不是静默、免打扰、专注、睡着，一眼能看明白。"""
        behavior = getattr(self, "behavior", None)
        if behavior is None:
            return "未接入"
        try:
            data = behavior.describe() or {}
        except Exception:
            return "读取失败"
        parts = ["静默" if data.get("quiet") else "正常"]
        hours = list(data.get("quiet_hours") or [])
        if len(hours) == 2:
            parts.append(f"免打扰 {hours[0]}-{hours[1]}")
        parts.append("主动开" if data.get("proactive_allowed") else "主动关")
        if data.get("sleeping"):
            parts.append("睡着了")
        elif data.get("user_away"):
            parts.append("用户不在")
        if data.get("window_busy"):
            parts.append("专注中")
        life = getattr(self.agent, "life", None)
        if life is not None:
            try:
                parts.append(life.label())
            except Exception:
                pass
        return "、".join(parts)

    def _life_text(self):
        """生命状态：待机 / 活跃 / 工作 / 休息 / 睡觉。程序判定，模型不参与。"""
        life = getattr(self.agent, "life", None)
        if life is None:
            return "未接入"
        try:
            data = life.describe()
        except Exception:
            return "读取失败"
        return "%s（%s）" % (data.get("label"), data.get("reason") or "无")

    def _skills_text(self):
        rows = self.skill_rows()
        if not rows:
            return "未接入或没有 Skill"
        enabled = [row for row in rows if row.get("enabled")]
        last = getattr(self.agent, "last_skill", None)
        tail = ("，最近用过 %s" % last) if last else ""
        return "%d 个，启用 %d 个%s" % (len(rows), len(enabled), tail)

    def developer_rows_dict(self):
        """开发者面板的那几行，整理成字典给诊断包用。"""
        try:
            return {str(name): str(value) for name, value in self.developer_rows()}
        except Exception:
            return {}
