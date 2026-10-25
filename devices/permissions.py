"""设备动作授权。模型和设备都绕不过这一层。

等级由动作注册表决定，不由模型说。舵机这类会动的东西默认归到「涉及个人数据」
以上，必须过用户确认；已经给过的持久授权也只在「这一台设备的这一个动作」上生效，
不是把设备控制整块打开。
"""

from tools.permission import GrantMode, PermissionLevel, level_rank, normalize_level

CONNECT_TOOL = "device.connect"
ACTION_PREFIX = "device.action"
# 需要确认的动作至少按这个等级走，低等级也拦得住。
CONFIRM_FLOOR = PermissionLevel.MEDIUM


def action_key(device, action):
    """授权键：一台设备 + 一个动作。范围小到刚好够用。"""
    return "%s:%s:%s" % (ACTION_PREFIX, str(device or "").strip(), str(action or "").strip())


class DevicePermissionService:
    """设备动作与串口连接的授权入口。没有权限中心时一律按拒绝处理。"""

    def __init__(self, permissions=None, logger=None):
        self.permissions = permissions
        self.logger = logger

    # ---------- 动作 ----------
    def check_action(self, spec, device, reason=""):
        """动作 + 设备 → 判断。返回 (allowed, needs_confirmation, level, 人话原因)。"""
        if self.permissions is None:
            return False, False, normalize_level(getattr(spec, "permission", "READ")), "没有权限中心，设备动作一律不执行"
        level = self._level_of(spec)
        key = action_key(device, getattr(spec, "name", ""))
        why = reason or "%s 执行 %s" % (device, getattr(spec, "name", ""))
        decision = self.permissions.check(key, level, why, {"device": str(device), "action": str(getattr(spec, "name", ""))})
        return (
            bool(decision.allowed),
            bool(decision.needs_confirmation),
            str(decision.level or level),
            str(decision.reason or ""),
        )

    def _level_of(self, spec):
        level = normalize_level(getattr(spec, "permission", "READ"))
        if getattr(spec, "needs_confirmation", False):
            if level_rank(level) < level_rank(CONFIRM_FLOOR):
                level = CONFIRM_FLOOR.value
        return level

    def remember(self, device, action, mode=GrantMode.PERSISTENT.value, note=""):
        """记住一次授权。只记住这一台设备的这一个动作。"""
        store = getattr(self.permissions, "grants", None)
        if store is None:
            return {"ok": False, "error": "当前没有可用的授权存储"}
        grant = store.set(action_key(device, action), mode, scope="", note=note)
        if grant is None:
            return {"ok": False, "error": "这个模式不认识：%s" % mode}
        return {"ok": True, "grant": grant.to_dict()}

    def revoke(self, device=None, action=None):
        """撤销。不给动作就撤这台设备的全部动作授权；撤销立刻生效。"""
        store = getattr(self.permissions, "grants", None)
        if store is None:
            return 0
        if device is None:
            removed = 0
            for grant in store.all():
                if str(grant.get("tool", "")).startswith(ACTION_PREFIX + ":"):
                    removed += store.revoke(grant["tool"], grant.get("scope"))
            return removed
        prefix = "%s:%s:" % (ACTION_PREFIX, str(device).strip())
        if action:
            return store.revoke(action_key(device, action))
        removed = 0
        for grant in store.all():
            if str(grant.get("tool", "")).startswith(prefix):
                removed += store.revoke(grant["tool"], grant.get("scope"))
        return removed

    def action_grants(self):
        store = getattr(self.permissions, "grants", None)
        if store is None:
            return []
        return [grant for grant in store.all() if str(grant.get("tool", "")).startswith(ACTION_PREFIX + ":")]

    # ---------- 连接串口 ----------
    def check_connect(self, port, device=""):
        """第一次连一个串口要用户点头。同一端口授权过就照旧。"""
        if self.permissions is None:
            return False, True, normalize_level(PermissionLevel.MEDIUM), "没有权限中心，串口不连接"
        name = str(port or "").strip()
        if not name:
            return False, False, normalize_level(PermissionLevel.MEDIUM), "没有指定串口，没什么可连的"
        decision = self.permissions.check(
            CONNECT_TOOL,
            PermissionLevel.MEDIUM,
            "连接串口设备 %s" % name,
            {"path": name},
        )
        return (
            bool(decision.allowed),
            bool(decision.needs_confirmation),
            str(decision.level),
            str(decision.reason or ""),
        )

    def connect_grants(self):
        store = getattr(self.permissions, "grants", None)
        if store is None:
            return []
        return [grant for grant in store.all() if grant.get("tool") == CONNECT_TOOL]