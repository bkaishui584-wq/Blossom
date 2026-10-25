"""桌宠角色（Live2D）的接线：加载、缩放、显隐、状态、角色包信息。

从 ui/desktop.py 整块搬过来的。渲染进程由 avatar/ 里的实现负责，这里只管接线。"""

import time

import tkinter as tk


class AvatarGlue:
    """混入 DesktopPet。行为与搬之前完全一样。"""

    def _avatar_provider(self):
        avatar = getattr(self.agent, "avatar", None)
        return avatar if getattr(avatar, "name", "") == "live2d" else None

    def _live2d_give_up(self, reason):
        """配了模型却接不上：写清原因，只保留透明拖动区域。"""
        self._live2d_problem = str(reason or "")
        if self.logger:
            self.logger.warning("live2d not shown: %s", self._live2d_problem)
        self.pet.set_blank(True)
        self.host.clear_hit_mask()
        self.host.set_fallback_hit()
        self._append(
            "err",
            "角色画面没起来，桌面只保留透明拖动区域。原因：%s"
            % (self._live2d_problem or "未知"),
        )
        return None

    def _live2d_ready_check(self, tries=1):
        """角色画面一直不就绪时催一次；催了还不行就如实说明。"""
        if self._live2d_ready or self.live2d is None or self.host.hidden:
            return True
        if tries > 0:
            self._warn("live2d still not ready, asking it to reload")
            try:
                self.live2d.load_model()
            except Exception as exc:
                self._warn("live2d reload failed: %s", exc)
            return self._arm_live2d_ready_check(0)
        # 画面可能已经在显示了，只是那句话没送到。这里不动它，只留个记录。
        self._live2d_problem = "角色画面加载超时"
        self._warn("live2d never reported ready in time")
        self._append(
            "hint",
            "角色画面加载比平时慢。要是桌面上一直看不到角色，把日志发我看一眼。",
        )
        return False

    def _live2d_still_rendering(self):
        """渲染进程是不是还在正常画。它自己报就绪，就不算失败。"""
        if self.live2d is None:
            return False
        try:
            # 进程都没了就别嘴硬；进程还在、自己又报就绪，才算误会一场。
            if not self.live2d.running():
                return False
            status = self.live2d.refresh_status() or {}
        except Exception as exc:
            self._warn("live2d status check failed: %s", exc)
            return False
        return str(status.get("state") or "") == "ready"

    def _live2d_react(self, name):
        """让角色对用户动作当场有点反应；渲染窗口不在就当没这回事。"""
        react = getattr(self.live2d, "react", None)
        if react is None:
            return None
        try:
            result = react(name)
        except Exception as exc:
            self._warn("live2d reaction failed: %s", exc)
            return None
        # 点了没反应时，日志里要能看出是哪一层不肯动；普通用户界面不显示这些。
        if self.logger:
            if isinstance(result, dict) and not result.get("ok"):
                self.logger.info(
                    "live2d reaction skipped: %s (%s)", name, result.get("error") or ""
                )
            else:
                self.logger.info("live2d reaction: %s", name)
        return result

    def _character_rows(self):
        """角色包相关的只读行。全部来自真实扫描，没装角色包就如实说没有。"""
        manager = getattr(self.agent, "characters", None)
        if manager is None:
            return []
        try:
            return [row for row in manager.rows() if row[0] != "正在用的模型"]
        except Exception as exc:
            self._warn("character rows failed: %s", exc)
            return [("角色包", f"读取失败：{exc}")]

    def live2d_reload(self):
        """重新扫资源再装载。资源变了就换新的，没变也要能把画面重装一次。"""
        provider = self._avatar_provider()
        if provider is None:
            self._hint("没有接入 Live2D。")
            return False
        ok, note, changed = self._rescan_characters()
        if not ok:
            return False
        if self.live2d is None:
            if self._attach_live2d() is None:
                self._hint("角色窗口起不来，桌面保持透明拖动区域。")
                return False
        elif not changed:
            # 用户点这个按钮常常是为了把画面救回来，资源没变也重装一次。
            try:
                provider.load_model()
            except Exception as exc:
                self._hint(f"重新加载失败：{exc}")
                return False
        self._hint(f"正在重新加载角色{note}")
        return True

    def _rescan_characters(self):
        """重扫角色资源，真有变化才交给形象接口。返回 (能不能用, 补充说明, 有没有变化)。"""
        manager = getattr(self.agent, "characters", None)
        if manager is None:
            return True, "", False
        try:
            result = manager.reload() or {}
        except Exception as exc:
            self._warn("character reload failed: %s", exc)
            return True, "", False
        if not result.get("ok", True):
            self._hint(f"角色资源没通过检查：{result.get('error') or '资源不齐'}")
            return False, "", False
        if not result.get("changed"):
            return True, "", False
        provider = self._avatar_provider()
        if provider is not None and hasattr(provider, "refresh"):
            try:
                provider.refresh(manager.model(), manager.action_map())
            except Exception as exc:
                self._warn("character refresh failed: %s", exc)
        return True, "，动作或文件有变化", True

    def live2d_unload(self):
        if self.live2d is None:
            self._hint("角色窗口没有在运行。")
            return False
        try:
            self.live2d.unload_model()
        except Exception as exc:
            self._hint(f"卸载失败：{exc}")
            return False
        self.pet.set_blank(True)
        self.host.clear_hit_mask()
        self.host.set_fallback_hit()
        self._hint("角色已卸载，当前只保留透明拖动区域。")
        return True

    def live2d_toggle_visible(self):
        self.host.toggle_hidden()
        return not self.host.hidden

    def live2d_scale(self, delta):
        """缩放只改窗口大小，模型始终按自身比例铺满，不会拉伸。"""
        value = max(0.4, min(3.0, self._live2d_scale_value + float(delta)))
        self._live2d_scale_value = value
        if self.live2d is not None:
            try:
                status = self.live2d.get_status()
                self._fit_live2d_body({"boxSize": status.get("boxSize")})
            except Exception as exc:
                self._warn("live2d scale failed: %s", exc)
        self._remember("scale", f"{value:.3f}")
        self._hint(f"角色缩放：{value:.2f} 倍")
        return value
