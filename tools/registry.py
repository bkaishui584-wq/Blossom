"""工具注册与执行。模型只能请求工具，执行权始终在程序。"""

import json
import os
import time
from dataclasses import dataclass, field

from tools.permission import SAFE_LEVELS, normalize_level


class ToolError(Exception):
    """工具执行失败。由执行层转成真实错误返回，绝不伪造成功。"""


# 任务模式的"向用户提问"工具名。程序认这个名字来暂停任务。
ASK_USER_TOOL = "ask_user"


@dataclass
class Tool:
    name: str
    description: str
    schema: dict = field(default_factory=dict)
    permission: str = "READ"
    risk: str = ""
    handler: object = None
    keywords: tuple = ()
    # 把结果写成人话。只有带上它的只读工具才会被程序直接执行（见 agent/safe_tools.py）。
    summarize: object = None
    # --- V3（阶段 E）：执行策略。默认值就是最保守的那套，不改老工具的行为。
    readonly: bool = False      # 只读工具不会改本地状态，也不需要确认
    cancelable: bool = True     # 任务取消时这一步能不能中断
    retryable: bool = True      # 失败后允不允许重试
    idempotent: bool = True     # 重跑一次会不会产生重复副作用
    timeout: float = 0.0        # 超过这个秒数只记一条警告，不硬杀线程
    verify: object = None       # 执行后校验：接收结果，返回一行说明
    # V5：这项能力依赖什么。none 本地就能用，network 要联网，model 要模型。
    requires: str = "none"

    @property
    def is_readonly(self):
        return bool(self.readonly or normalize_level(self.permission) == "READ")

    def to_model_schema(self):
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.schema or {"type": "object", "properties": {}},
            },
        }

    def to_dict(self):
        """给开发者面板和审计看。不发 schema，避免把参数说明塞进上下文。"""
        return {
            "name": self.name,
            "description": self.description,
            "permission": normalize_level(self.permission),
            "readonly": self.is_readonly,
            "cancelable": bool(self.cancelable),
            "retryable": bool(self.retryable),
            "idempotent": bool(self.idempotent),
            "timeout": float(self.timeout or 0),
            "has_verify": callable(self.verify),
            "requires": str(self.requires or "none"),
        }

    def search_text(self):
        return " ".join([self.name, self.description, *self.keywords]).lower()


class ToolRegistry:
    def __init__(self):
        self._tools = {}

    def register(self, tool):
        self._tools[tool.name] = tool
        return tool

    def get(self, name):
        return self._tools.get(name)

    def names(self):
        return sorted(self._tools)

    def all(self):
        return [self._tools[name] for name in self.names()]

    def index(self):
        """紧凑目录：只有名字和一行说明，不含完整 schema。"""
        return "\n".join(f"- {tool.name}: {tool.description}" for tool in self.all())

    def schemas_for(self, names):
        """只加载被点名的工具 schema，避免把全部 schema 发给模型。"""
        return [
            self._tools[name].to_model_schema() for name in names if name in self._tools
        ]

    def relevant(self, query, limit=3, levels=None):
        """按工具自带关键词挑选相关工具。挑不中就不加载任何 schema。

        levels 限定权限等级：陪伴模型只拿安全工具，高风险工具只在任务模式出现。
        """
        text = str(query or "").lower()
        if not text:
            return []
        wanted = None if levels is None else {normalize_level(level) for level in levels}
        scored = []
        for tool in self.all():
            if wanted is not None and normalize_level(tool.permission) not in wanted:
                continue
            hits = sum(1 for keyword in tool.keywords if str(keyword).lower() in text)
            if hits:
                scored.append((hits, tool.name))
        scored.sort(reverse=True)
        return [name for _, name in scored[:limit]]

    def for_intent(self, intent, query, limit=3):
        """按意图挑工具。陪伴只拿安全工具，任务模式才放开高风险工具。"""
        kind = str(intent or "")
        if kind in ("info", "chat", "consult", "local"):
            return self.relevant(query, limit=limit, levels=SAFE_LEVELS)
        if kind == "task":
            return self.relevant(query, limit=limit)
        return self.relevant(query, limit=limit, levels=SAFE_LEVELS)

    def readonly_names(self):
        return [tool.name for tool in self.all() if tool.is_readonly]

    def catalog(self):
        """给能力中心用：可用工具、依赖什么、要什么权限，一眼看全。"""
        return [tool.to_dict() for tool in self.all()]


# JSON Schema 里那几种基础类型。工具自己声明什么就按什么收，没声明就不管。
_JSON_TYPES = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
}


def _type_ok(kind, value):
    """按声明的类型判一下。布尔不算数字，免得 True 被当成 1 用。"""
    if not kind or value is None:
        return True
    if str(kind) == "string" and isinstance(value, os.PathLike):
        # 程序内部传路径对象是常事；对模型来说 schema 里那份仍然是字符串。
        return True
    expected = _JSON_TYPES.get(str(kind))
    if expected is None:
        return True
    if isinstance(value, bool) and str(kind) in ("integer", "number"):
        return False
    return isinstance(value, expected)


def validate_arguments(tool, arguments):
    """按 schema 做最小校验。返回值是错误描述，None 表示通过。"""
    schema = tool.schema or {}
    arguments = arguments if isinstance(arguments, dict) else {}
    for key in schema.get("required", []):
        if key not in arguments:
            return f"缺少必填参数 {key}"
    properties = schema.get("properties", {})
    if properties and schema.get("additionalProperties") is False:
        unknown = [key for key in arguments if key not in properties]
        if unknown:
            return f"出现未声明参数 {', '.join(sorted(unknown))}"
    for key, value in arguments.items():
        spec = properties.get(key)
        if not isinstance(spec, dict):
            continue
        kind = spec.get("type")
        if not _type_ok(kind, value):
            return f"参数 {key} 应该是 {kind}"
    return None


@dataclass
class ToolOutcome:
    name: str
    ok: bool
    allowed: bool = True
    needs_confirmation: bool = False
    level: str = ""
    result: object = None
    error: str = ""
    truncated: bool = False
    # V3：执行事实。ok 只说明程序跑完了，checks 才说明结果对不对。
    readonly: bool = False
    idempotent: bool = True
    verified: bool = False
    checks: list = field(default_factory=list)
    seconds: float = 0.0
    slow: bool = False

    def for_model(self):
        """给模型看的精简结果，长度受限。"""
        payload = {"ok": self.ok}
        if self.ok:
            payload["result"] = self.result
        else:
            payload["error"] = self.error
        if self.truncated:
            payload["note"] = "结果过长已截断"
        return json.dumps(payload, ensure_ascii=False)

    def to_dict(self):
        return {
            "name": self.name,
            "ok": self.ok,
            "allowed": self.allowed,
            "needs_confirmation": self.needs_confirmation,
            "level": self.level,
            "error": self.error,
            "readonly": self.readonly,
            "idempotent": self.idempotent,
            "verified": self.verified,
            "checks": list(self.checks),
            "seconds": round(float(self.seconds), 3),
            "slow": self.slow,
        }



def _string_paths(node, prefix=()):
    """找出所有字符串叶子的路径，用来裁剪长字段。"""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _string_paths(value, prefix + (key,))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _string_paths(value, prefix + (index,))
    elif isinstance(node, str) and prefix:
        yield prefix


def _read_path(node, path):
    target = node
    for key in path:
        target = target[key]
    return target


def _write_path(node, path, value):
    target = node
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


def shrink_json(value, limit):
    """结构化结果过长时按字段裁，不把 JSON 本身切坏。

    切成半截 JSON 会让调用方解析失败，宁可少给内容也不给坏数据。
    """
    text = json.dumps(value, ensure_ascii=False, default=str)
    for _ in range(200):
        if len(text) <= limit:
            break
        paths = list(_string_paths(value))
        if not paths:
            break
        longest = max(paths, key=lambda item: len(_read_path(value, item)))
        current = _read_path(value, longest)
        if len(current) <= 32:
            break
        over = len(text) - limit
        keep = max(24, len(current) - over - 24)
        _write_path(value, longest, current[:keep] + "…（已截断）")
        text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > limit:
        head = text[: max(0, limit - 90)]
        text = json.dumps({"truncated": True, "preview": head}, ensure_ascii=False)
    return text


# 这些字段留在程序里给人看，不发给模型：太长，而且模型用不上。
MODEL_HIDDEN_KEYS = ("output_raw",)


class ToolExecutor:
    """唯一执行入口：查表 → 权限 → 校验 → 执行 → 审计。"""

    MAX_RESULT_CHARS = 2000

    def __init__(self, registry, permissions, audit=None, logger=None):
        self.registry = registry
        self.permissions = permissions
        self.audit = audit
        self.logger = logger

    def execute(self, name, arguments=None):
        tool = self.registry.get(name)
        if tool is None:
            return ToolOutcome(name=str(name), ok=False, allowed=False, error="未知工具")

        decision = self.permissions.check(tool.name, tool.permission, tool.risk, arguments)
        if not decision.allowed:
            self._audit("tool_blocked", tool, decision.reason)
            return ToolOutcome(
                name=tool.name,
                ok=False,
                allowed=False,
                needs_confirmation=decision.needs_confirmation,
                level=decision.level,
                error=f"权限未通过：{decision.reason}",
            )

        invalid = validate_arguments(tool, arguments)
        if invalid:
            self._audit("tool_invalid", tool, invalid)
            return ToolOutcome(
                name=tool.name, ok=False, level=decision.level, error=f"参数不合法：{invalid}"
            )

        started = time.monotonic()
        try:
            result = tool.handler(**(arguments or {}))
        except Exception as exc:
            self._audit("tool_error", tool, str(exc))
            return ToolOutcome(
                name=tool.name, ok=False, level=decision.level, error=f"执行失败：{exc}"
            )

        seconds = time.monotonic() - started
        # 有些字段是给人看的（比如完整命令输出），不塞给模型，省下的位置留给结论。
        visible = result
        if isinstance(result, dict) and any(key in result for key in MODEL_HIDDEN_KEYS):
            visible = {key: value for key, value in result.items() if key not in MODEL_HIDDEN_KEYS}
        text = visible if isinstance(visible, str) else json.dumps(visible, ensure_ascii=False, default=str)
        truncated = len(text) > self.MAX_RESULT_CHARS
        if truncated:
            # 结构化结果按字段裁，保住 JSON；纯文本按长度截断。
            if isinstance(result, str):
                text = text[: self.MAX_RESULT_CHARS]
            else:
                text = shrink_json(result, self.MAX_RESULT_CHARS)
        # 结果是否可信由工具的校验钳决定。没有校验器就如实说没验证过。
        checks = []
        if callable(tool.verify):
            try:
                note = tool.verify(result)
                if note:
                    checks.append(str(note))
            except Exception as exc:
                checks.append(f"校验没跑成：{exc}")
        slow = bool(tool.timeout) and seconds > float(tool.timeout)
        if slow:
            self._audit("tool_slow", tool, f"{seconds:.2f}s > {tool.timeout}s")
        self._audit("tool_ok", tool, text[:200])
        return ToolOutcome(
            name=tool.name,
            ok=True,
            level=decision.level,
            result=text,
            truncated=truncated,
            readonly=tool.is_readonly,
            idempotent=bool(tool.idempotent),
            verified=bool(checks),
            checks=checks,
            seconds=seconds,
            slow=slow,
        )

    def _audit(self, kind, tool, detail):
        if self.audit:
            self.audit.record(kind=kind, name=tool.name, level=tool.permission, decision=kind, detail=detail)
        if self.logger:
            self.logger.info("%s %s: %s", kind, tool.name, detail)
