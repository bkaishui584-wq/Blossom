"""权限系统：所有工具调用必须先过这里，人格无法绕过。

等级对应的人话：
    READ     读取
    LOW      普通操作
    MEDIUM   涉及个人数据
    HIGH     高风险操作
    CRITICAL 系统级操作（默认直接拒绝，授权也解不开）

除了等级，还认「授权」：拒绝、单次、每次确认、持久。持久授权必须带范围
（哪个工具、哪条路径、哪条命令），不是一个总开关。判断结果写进审计。
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path


class PermissionLevel(str, Enum):
    READ = "READ"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


LEVEL_ORDER = (
    PermissionLevel.READ,
    PermissionLevel.LOW,
    PermissionLevel.MEDIUM,
    PermissionLevel.HIGH,
    PermissionLevel.CRITICAL,
)


# 核心模块只允许在这个范围内的工具，高风险工具只能走确认模式。
SAFE_LEVELS = (PermissionLevel.READ.value, PermissionLevel.LOW.value)


LEVEL_LABELS = {
    "READ": "读取",
    "LOW": "普通操作",
    "MEDIUM": "涉及个人数据",
    "HIGH": "高风险操作",
    "CRITICAL": "系统级操作",
}


class GrantMode(str, Enum):
    DENY = "deny"
    ONCE = "once"
    EACH_TIME = "each"
    PERSISTENT = "always"


MODE_LABELS = {
    "deny": "已拒绝",
    "once": "只允许这一次",
    "each": "每次都要确认",
    "always": "已持久授权",
}


def normalize_level(level):
    """统一成等级名。注意 str(PermissionLevel.LOW) 带类名前缀，必须取 value。"""
    return str(getattr(level, "value", level)).upper()


def level_rank(level):
    """未知等级按最高风险处理，宁严不松。"""
    try:
        return LEVEL_ORDER.index(PermissionLevel(normalize_level(level)))
    except ValueError:
        return len(LEVEL_ORDER)


def normalize_mode(mode):
    return str(getattr(mode, "value", mode) or "").strip().lower()


_PATH_KEYS = ("path", "root", "cwd", "file", "dir", "folder", "target", "workspace")
_COMMAND_KEYS = ("command", "cmd", "script", "shell")


def request_scope(arguments):
    """从参数里取出这次操作真正碰的东西，返回 "path:..." / "command:..." 或 ""。

    只认第一个命中的参数，取不到就不带范围。
    """
    if not isinstance(arguments, dict):
        return ""
    for key, value in arguments.items():
        name = str(key).strip().lower()
        if not isinstance(value, str) or not value.strip():
            continue
        text = value.strip()
        if name in _PATH_KEYS or name.endswith("_path") or name.endswith("_dir"):
            return "path:" + text
        if name in _COMMAND_KEYS:
            return "command:" + text
    return ""


def _inside(granted, target):
    """target 是否在 granted 目录里面。只按路径层级比，不做字符串前缀。"""
    left = os.path.normcase(os.path.normpath(granted))
    right = os.path.normcase(os.path.normpath(target))
    if left == right:
        return True
    return right.startswith(left.rstrip("\\/") + os.sep)


def _escapes(scope):
    """路径里带 .. 或明显想跳出目录，一律按越权处理。"""
    kind, _, value = str(scope).partition(":")
    if kind != "path":
        return False
    parts = [part for part in Path(value.replace("\\", "/")).parts if part not in ("", ".")]
    return ".." in parts


@dataclass
class Grant:
    """一条授权：哪个工具、什么范围、哪种模式。范围为空表示该工具整体。"""

    tool: str
    mode: str
    scope: str = ""
    note: str = ""
    created_at: str = ""
    uses: int = 0

    def to_dict(self):
        return {
            "tool": self.tool,
            "mode": self.mode,
            "scope": self.scope,
            "note": self.note,
            "created_at": self.created_at,
            "uses": self.uses,
        }

    @classmethod
    def from_dict(cls, raw):
        if not isinstance(raw, dict):
            return None
        tool = str(raw.get("tool") or "").strip()
        if not tool:
            return None
        return cls(
            tool=tool,
            mode=normalize_mode(raw.get("mode")),
            scope=str(raw.get("scope") or "").strip(),
            note=str(raw.get("note") or ""),
            created_at=str(raw.get("created_at") or ""),
            uses=int(raw.get("uses") or 0),
        )

    def matches(self, tool, scope=""):
        if self.tool != tool:
            return False
        if not self.scope:
            return True
        if not scope:
            return False
        kind, _, value = self.scope.partition(":")
        other_kind, _, other = str(scope).partition(":")
        if kind != other_kind:
            return False
        if kind == "command":
            return other.split(" ")[0].lower() == value.split(" ")[0].lower()
        return _inside(value, other)


class GrantStore:
    """授权表。拒绝和持久授权落盘，单次授权只活在内存里。"""

    def __init__(self, path=None, logger=None):
        self.path = Path(path) if path else None
        self.logger = logger
        # key: (tool, scope)；同一工具的范围互不覆盖，撤销时精确移除。
        self._grants = {}
        self.load()

    # ---------- 读写授权 ----------
    def set(self, tool, mode, scope="", note=""):
        mode = normalize_mode(mode)
        if mode not in {item.value for item in GrantMode}:
            return None
        tool = str(tool or "").strip()
        if not tool:
            return None
        grant = Grant(
            tool=tool,
            mode=mode,
            scope=str(scope or "").strip(),
            note=str(note or ""),
            created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        self._grants[(grant.tool, grant.scope)] = grant
        self.save()
        return grant

    def match(self, tool, scope=""):
        """范围最具体的那条优先，其次才是该工具的整体授权。"""
        best = None
        for grant in self._grants.values():
            if not grant.matches(tool, scope):
                continue
            if best is None or len(grant.scope) > len(best.scope):
                best = grant
        return best

    def consume_once(self, grant):
        grant.uses += 1
        self._grants.pop((grant.tool, grant.scope), None)
        return grant

    def revoke(self, tool, scope=None):
        """撤销。不给范围就撤该工具的全部授权，撤销立刻生效。"""
        tool = str(tool or "").strip()
        removed = 0
        for key in list(self._grants):
            if key[0] != tool:
                continue
            if scope is not None and key[1] != str(scope).strip():
                continue
            self._grants.pop(key, None)
            removed += 1
        if removed:
            self.save()
        return removed

    def all(self):
        return [grant.to_dict() for grant in sorted(self._grants.values(), key=lambda g: (g.tool, g.scope))]

    # ---------- 落盘 ----------
    def load(self):
        if self.path is None or not self.path.exists():
            return 0
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8") or "[]")
        except Exception as exc:
            self._warn("grant load failed: %s", exc)
            return 0
        for item in raw if isinstance(raw, list) else []:
            grant = Grant.from_dict(item)
            if grant is None or grant.mode not in (GrantMode.DENY.value, GrantMode.PERSISTENT.value):
                continue
            self._grants[(grant.tool, grant.scope)] = grant
        return len(self._grants)

    def save(self):
        if self.path is None:
            return False
        # 单次授权和「每次确认」不落盘：重启之后不该留下临时让步。
        rows = [
            grant.to_dict()
            for grant in self._grants.values()
            if grant.mode in (GrantMode.DENY.value, GrantMode.PERSISTENT.value)
        ]
        # 一条持久授权都没有、也还没建过文件时，不留空文件。
        if not rows and not self.path.exists():
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            self._warn("grant save failed: %s", exc)
            return False
        return True

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)


@dataclass
class PermissionDecision:
    allowed: bool
    level: str
    reason: str = ""
    needs_confirmation: bool = False
    auto: bool = False

    def to_dict(self):
        return {
            "allowed": self.allowed,
            "level": self.level,
            "reason": self.reason,
            "needs_confirmation": self.needs_confirmation,
            "auto": self.auto,
        }


class PermissionManager:
    """默认策略：低风险自动放行，中高风险先确认，危险操作直接禁止。

    没有确认通道时高风险不会「一放就过」，而是 fail closed。
    """

    def __init__(
        self,
        auto_allow_max=PermissionLevel.LOW,
        confirm_max=PermissionLevel.HIGH,
        deny_from=PermissionLevel.CRITICAL,
        confirm_callback=None,
        audit=None,
        logger=None,
        grants=None,
    ):
        self.auto_allow_max = level_rank(auto_allow_max)
        self.confirm_max = level_rank(confirm_max)
        self.deny_from = level_rank(deny_from)
        self.confirm_callback = confirm_callback
        self.audit = audit
        self.logger = logger
        self.grants = grants

    def check(self, name, level, reason="", arguments=None):
        rank = level_rank(level)
        level_name = normalize_level(level)
        scope = request_scope(arguments)

        if scope and _escapes(scope):
            return self._decide(
                False, level_name, "路径越权，已拒绝", False, False, name, f"{reason} | {scope}"
            )

        if rank >= self.deny_from:
            return self._decide(
                False, level_name, f"{name} 属于禁止等级，不允许执行", False, False, name, reason
            )

        grant = self.grants.match(name, scope) if self.grants else None
        if grant is not None:
            mode = normalize_mode(grant.mode)
            where = grant.scope or "该工具"
            if mode == GrantMode.DENY.value:
                return self._decide(
                    False, level_name, f"你之前拒绝了 {where}", False, False, name, reason
                )
            if mode == GrantMode.ONCE.value:
                self.grants.consume_once(grant)
                return self._decide(
                    True, level_name, f"你批准了这一次 {where}", False, True, name, reason
                )
            if mode == GrantMode.PERSISTENT.value:
                grant.uses += 1
                self.grants.save()
                return self._decide(
                    True, level_name, f"已授权 {where}", False, True, name, reason
                )
            if mode == GrantMode.EACH_TIME.value:
                # 每次确认：把等级抬到确认区，低风险也要问。
                rank = max(rank, level_rank(PermissionLevel.MEDIUM))

        if rank <= self.auto_allow_max:
            return self._decide(
                True, level_name, "低风险操作，自动放行", False, True, name, reason
            )

        if rank <= self.confirm_max:
            if self.confirm_callback is None:
                return self._decide(
                    False,
                    level_name,
                    "需要用户确认，但当前没有确认通道",
                    True,
                    False,
                    name,
                    reason,
                )
            approved = bool(self.confirm_callback(name, level_name, reason))
            return self._decide(
                approved,
                level_name,
                "用户已批准" if approved else "用户已拒绝",
                not approved,
                False,
                name,
                reason,
            )

        return self._decide(
            False, level_name, "超出确认范围，按拒绝处理", False, False, name, reason
        )

    def _decide(self, allowed, level, reason, needs_confirmation, auto, name, detail):
        decision = PermissionDecision(
            allowed=allowed,
            level=level,
            reason=reason,
            needs_confirmation=needs_confirmation,
            auto=auto,
        )
        if self.audit:
            self.audit.record(
                kind="permission",
                name=name,
                level=level,
                decision="allow" if allowed else "deny",
                detail=f"{reason} | {detail}",
            )
        if self.logger:
            self.logger.info("permission %s %s: %s", name, level, reason)
        return decision
