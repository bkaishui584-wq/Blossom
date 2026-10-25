"""把窗口位置、尺寸、界面偏好这些状态记下来，下次启动照着恢复。

从 ui/desktop.py 整块搬过来的。写入一律进本地偏好，不动数据库。"""

import tkinter as tk

from ui import tray as tray_module

# 以前的小尺寸默认值。存着这两个数说明用户从没调过窗口，升级时换成大的。
# 放在这一层：判断"是不是老默认值"本来就是状态恢复的活儿。
LEGACY_CARD_SIZES = ((400, 520),)


class StateStore:
    """混入 DesktopPet。行为与搬之前完全一样。"""

    def _saved_scale(self, fallback):
        """上次调过的缩放重启后照旧；读不到就用配置里的值。"""
        raw = self._preference("live2d.scale")
        try:
            return max(0.4, min(3.0, float(raw)))
        except (TypeError, ValueError):
            return fallback

    def _restore_placeholder(self):
        try:
            width, height = self._placeholder_size
            self.pet.size = int(width)
            self.host.resize_body(int(width), int(height))
        except Exception as exc:
            self._warn("placeholder restore failed: %s", exc)

    def _save_position(self):
        if self.memory is None:
            return
        # 缩入时窗口大半在屏幕外，存那个坐标重启就找不回来了，所以存贴边时的位置。
        x, y = self.host.dock_rest_position()
        self._save_preference(
            self.POSITION_KEY, "%d,%d" % (self._unpx(x), self._unpx(y))
        )

    # ---------- 贴边与缩入 ----------
    DOCK_PREFERENCE = "pet.dock"
    DOCK_AUTO_PREFERENCE = "pet.dock.auto"
    DOCK_EDGE_PREFERENCE = "pet.dock.edge"

    def _restore_dock_edge(self):
        """重启后按记着的边重新贴一次，位置不会因为缩入而丢。"""
        edge = str(self._preference(self.DOCK_EDGE_PREFERENCE) or "").strip()
        if edge not in ("left", "right", "top", "bottom"):
            return ""
        return self.host.snap_to_edge(edge)

    def _saved_card_size(self):
        raw = self._preference("chat.size")
        if not raw:
            return self._default_card_size()
        try:
            width, height = (int(part) for part in str(raw).split("x", 1))
        except (TypeError, ValueError):
            return self._default_card_size()
        if (width, height) in LEGACY_CARD_SIZES:
            # 老默认值说明用户没调过窗口，直接换成新的工作台尺寸。
            return self._default_card_size()
        return (
            max(self._px(self.MIN_CARD_WIDTH), self._px(width)),
            max(self._px(self.MIN_CARD_HEIGHT), self._px(height)),
        )

    # ---------- 界面缩放 ----------
    # Ctrl + 加号 / 减号 / 0。只改自己的缩放，不动 Windows 的显示设置。
    ZOOM_KEYS = (
        ("<Control-plus>", "in"),
        ("<Control-equal>", "in"),
        ("<Control-KP_Add>", "in"),
        ("<Control-minus>", "out"),
        ("<Control-KP_Subtract>", "out"),
        ("<Control-Key-0>", "reset"),
        ("<Control-KP_0>", "reset"),
    )

    def _save_card_size(self):
        # 存设计值：换到别的显示比例上，窗口还是同样的观感大小。
        self._save_preference(
            "chat.size", "%dx%d" % (self._unpx(self._card_size[0]), self._unpx(self._card_size[1]))
        )

    # ---------- 轻量菜单 ----------

    def _save_startup(self):
        ok = self.set_startup(bool(self.startup_var.get()))
        self.startup_var.set(self.startup_enabled())
        return ok

    def _save_motion(self):
        """界面上那个勾：勾上就是减少动态效果。"""
        return self.set_motion(bool(self.motion_var.get()))

    def _save_hotkey(self):
        """全局快捷键存进内部偏好，重启后仍然生效。"""
        spec = str(self.hotkey_entry.get()).strip()
        if spec and not all(tray_module.parse_hotkey(spec)):
            self._hint("快捷键写法看不懂，例：ctrl+alt+d")
            return False
        if not self._save_preference("shell.hotkey", spec):
            return False
        icon = getattr(self, "tray", None)
        if icon is not None:
            try:
                icon.hotkey = spec
                icon._register_hotkey()
            except Exception as exc:
                if self.logger:
                    self.logger.warning("hotkey rebind failed: %s", exc)
        self._hint(f"已保存：全局快捷键 {spec or '不用'}")
        return True

    # ---------- 托盘 ----------

    def _save_topmost(self):
        enabled = bool(self.topmost_var.get())
        self.host.set_topmost(enabled)
        if self._save_preference("shell.topmost", "1" if enabled else "0"):
            self._stay_on_top()
            self._hint_settings(f"已保存：窗口置顶 {'开启' if enabled else '关闭'}")

    # ---------- 聊天 ----------

    def _save_proactive_number(self, key, entry, low, high):
        """保存冷却时间或每日上限。只接受范围内的整数，0 表示不加限制。"""
        raw = str(entry.get()).strip()
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            self._hint("请填一个整数（0 表示不加限制）。")
            return None
        value = max(low, min(high, value))
        entry.delete(0, tk.END)
        entry.insert(0, str(value))
        if not self._save_preference(key, str(value)):
            return None
        self._hint(f"已保存：{value}")
        return value

    # ---------- V4：主题 ----------

    def _save_sidebar_state(self, shown):
        return self._save_preference("chat.sidebar", "1" if shown else "0")

    def _load_history(self, session_id, limit=40):
        if self.memory is None:
            return 0
        try:
            rows = self.memory.repository.recent_messages(session_id, limit=limit)
        except Exception as exc:
            self._warn("history load failed: %s", exc)
            return 0
        count = 0
        for row in rows or []:
            role = str(row.get("role") or "")
            if role == "user":
                self._append_message("你", str(row.get("content") or ""), "me")
            else:
                self._append_message("桌宠", str(row.get("content") or ""), "pet")
            count += 1
        return count

    def _save_proactive(self):
        enabled = "1" if self.proactive_var.get() else "0"
        if self._save_preference("proactive_enabled", enabled):
            self._hint_settings(
                f"已保存：主动提醒 {'开启' if enabled == '1' else '关闭'}"
            )

    def _save_privacy(self, key):
        privacy = getattr(self, "privacy", None)
        var = getattr(self, "_privacy_vars", {}).get(key)
        if privacy is None or var is None:
            return
        if privacy.set(key, bool(var.get())):
            self._hint_settings(f"已保存：{key} {'开启' if var.get() else '关闭'}")
            # 「能不能主动说话」和设置里的「允许主动提醒」是同一个开关，顺手同步。
            if key == "proactive" and getattr(self, "proactive_var", None) is not None:
                self.proactive_var.set(bool(var.get()))
        else:
            self._hint_settings("保存失败：记忆不可用")

    def _save_quiet(self):
        enabled = bool(self.quiet_var.get())
        if self.quiet is not None:
            try:
                self.quiet.set(enabled)
            except Exception as exc:
                self._hint_settings(f"保存失败：{exc}")
                return False
        elif not self._save_preference("quiet_mode", "1" if enabled else "0"):
            return False
        if enabled and self.speech is not None:
            try:
                self.speech.stop()
            except Exception:
                pass
        self._hint_settings(f"已保存：静默模式 {'开启' if enabled else '关闭'}")
        self._refresh_status()
        return enabled

    def _save_quiet_hours(self):
        raw = self.quiet_entry.get().strip()
        if not raw:
            return
        if self._save_preference("quiet_hours", raw):
            self._hint_settings(f"已保存：免打扰 {raw}")

    def _save_preference(self, key, value):
        """返回是否真的保存成功，失败时不显示成功提示。"""
        if self.memory is None:
            self._hint_settings("记忆不可用，未能保存")
            return False
        try:
            self.memory.set_preference(key, value)
            return True
        except Exception as exc:
            self._hint_settings(f"保存失败：{exc}")
            return False
