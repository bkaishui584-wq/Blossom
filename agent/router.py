"""意图与模式路由。只做轻量规则判断，绝不调用模型。

模型负责理解，程序负责决定现在该走哪条路。
分析 != 执行；"告诉我怎么改" 不是 "替我改"。
"""

import re
from dataclasses import dataclass
from enum import Enum


class Mode(str, Enum):
    """桌宠当前所处的模式。"""

    LOCAL = "local"
    COMPANION = "companion"
    TASK_PENDING = "task_pending"
    TASK = "task"


class Intent(str, Enum):
    CHAT = "chat"
    MEMORY = "memory"
    SAFE_TOOL = "safe_tool"
    TASK = "task"


MODE_LABELS = {
    Mode.LOCAL.value: "本地模式",
    Mode.COMPANION.value: "陪伴",
    Mode.TASK_PENDING.value: "任务确认",
    Mode.TASK.value: "任务执行",
}

INTENT_LABELS = {
    Intent.CHAT.value: "闲聊",
    Intent.MEMORY.value: "记忆",
    Intent.SAFE_TOOL.value: "查信息",
    Intent.TASK.value: "任务",
}

# 明确表示"替我动手做"的说法
_TASK_PHRASES = (
    "帮我改", "帮我修改", "帮我做", "帮我弄", "帮我建", "帮我创建",
    "帮我删", "帮我跑", "帮我运行", "帮我执行", "帮我装", "帮我安装",
    "帮我提交", "帮我部署", "帮我重构", "帮我修复", "帮我加",
    "直接帮我", "替我改", "替我运行", "替我执行", "替我删",
)

# "帮我写…"是创作请求：让我写一段程序、一段代码，模型直接在对话里给就行，
# 不等于去改用户的文件（见 V2 验收 Case 3）。只有落到具体文件，或者顺手
# 还要改 / 跑 / 装 / 删东西时，才算任务。
_WRITE_PHRASES = ("帮我写", "替我写")

_MUTATING = (
    "改", "修改", "重写", "重构", "修复", "删除", "删掉", "运行", "执行",
    "跑起来", "安装", "部署", "提交", "打包", "编译", "迁移",
)

_TASK_VERBS = (
    "修改", "改成", "改一下", "重写", "重构", "修复", "删除", "删掉", "创建",
    "新建", "生成", "运行", "执行", "跑起来", "安装", "部署", "提交", "commit",
    "push", "迁移", "打包", "编译",
)

_TASK_TARGETS = (
    "文件", "代码", "脚本", "程序", "项目", "仓库", "目录", "函数", "模块",
    "报错", "错误", "命令", "依赖", "数据库", "服务",
)

# 只有"讨论/分析"的说法，不算任务
_DISCUSS = (
    "怎么改", "应该怎么", "如何修改", "告诉我怎么", "为什么", "什么原因",
    "分析一下", "解释一下", "讲讲", "什么思路", "怎么实现", "怎么理解",
)

_CAPABILITY = (
    "能不能", "可不可以", "可以吗", "会不会", "会吗", "有办法吗", "能帮我吗",
)

_MEMORY = ("记住", "记一下", "记下来", "别忘了", "别忘", "记着")

_SAFE = (
    "天气", "气温", "温度", "下雨", "冷不冷", "热不热", "新闻", "资讯",
    "头条", "热点", "日程", "安排", "待办", "设备", "舵机", "传感器",
)

_STOP = ("停", "取消", "别做了", "不要了", "算了", "停下", "不用了", "stop", "cancel")
_CONFIRM = ("开始", "好", "可以", "行", "嗯", "确认", "执行吧", "继续", "是的", "yes", "ok", "go")

_PATH_RE = re.compile(r"[\w\-./\\]+\.(py|js|ts|tsx|json|md|txt|html|css|yml|yaml|toml|cfg|ini|bat|sh|sql)")


def _has(text, words):
    return any(word in text for word in words)


def _is_capability(text):
    if _has(text, _CAPABILITY):
        return True
    # "你能帮我改代码吗？" 是能力询问，不是让我立刻动手
    return "帮我" in text and text.rstrip().endswith(("吗", "?", "？"))


def _is_write_request(text):
    if not _has(text, _WRITE_PHRASES):
        return False
    if _PATH_RE.search(text):
        return False
    return not _has(text, _MUTATING)


def _is_task(text):
    if _has(text, _TASK_PHRASES):
        return True
    if _has(text, _WRITE_PHRASES) and _PATH_RE.search(text):
        # 指名要写到某个文件，就是文件操作，不是单纯让我写一段内容。
        return True
    if _has(text, _TASK_VERBS) and (_has(text, _TASK_TARGETS) or bool(_PATH_RE.search(text))):
        return True
    return False


def classify(text):
    """把用户输入分成 CHAT / MEMORY / SAFE_TOOL / TASK。"""
    lowered = str(text or "").strip().lower()
    if not lowered:
        return Intent.CHAT
    if _is_capability(lowered) or _has(lowered, _DISCUSS) or _is_write_request(lowered):
        return Intent.CHAT
    if _is_task(lowered):
        return Intent.TASK
    if _has(lowered, _SAFE):
        return Intent.SAFE_TOOL
    if _has(lowered, _MEMORY):
        return Intent.MEMORY
    return Intent.CHAT


def is_stop(text):
    return _has(str(text or "").strip().lower(), _STOP)


def is_confirm(text):
    return _has(str(text or "").strip().lower(), _CONFIRM)


class IntentRouter:
    """默认关闭的薄封装，便于以后替换成更聪明的路由而不改 AgentCore。"""

    def classify(self, text):
        return classify(text)

    def is_stop(self, text):
        return is_stop(text)

    def is_confirm(self, text):
        return is_confirm(text)


    # ---------- V3：分层路由 ----------
    def decide(self, text, awaiting="", capability=None, legacy=None):
        """按层次给出结构化结论。程序据此决定走哪条路，模型只负责理解。

        层次：停/取消/确认 → 确定性本地能力 → 闲聊/咨询/查信息/任务 → 模糊就问。
        capability 传入 True 或可调用对象，表示本地能力已经能处理。
        """
        raw = str(text or "")
        lowered = raw.strip().lower()
        if not lowered:
            return RouteDecision(
                intent=IntentKind.UNKNOWN.value, confidence=0.2, reason="空输入"
            )

        # 第一层：只认状态。取消和确认优先于任何意图判断。
        if is_stop(lowered):
            return RouteDecision(
                intent=IntentKind.CANCEL.value,
                confidence=0.95,
                requested_action="取消",
                session_relation="continue" if awaiting else "",
                reason="命中停止/取消说法",
                legacy=Intent.CHAT.value,
            )
        if awaiting == "confirm" and is_confirm(lowered):
            return RouteDecision(
                intent=IntentKind.CONFIRM.value,
                confidence=0.9,
                requested_action="确认",
                session_relation="continue",
                reason="在等确认，用户答应了",
            )
        if awaiting == "user":
            return RouteDecision(
                intent=IntentKind.SUPPLEMENT.value,
                confidence=0.85,
                target=_target(raw),
                session_relation="continue",
                reason="任务在等用户补充信息",
            )

        # 第二层：确定性本地能力。能算出来的不问模型。
        matched = capability() if callable(capability) else bool(capability)
        if matched:
            return RouteDecision(
                intent=IntentKind.LOCAL.value,
                confidence=0.9,
                target=_target(raw),
                requested_action=_action(raw),
                requires_tools=True,
                reason="本地能力可直接处理",
            )

        # 第三层：闲聊 / 咨询 / 查信息 / 任务。
        kind = _kind_of(classify(raw) if legacy is None else legacy, lowered)
        action = _action(raw)
        target = _target(raw)
        decision = RouteDecision(
            intent=kind.value,
            confidence=0.7,
            target=target,
            requested_action=action,
            legacy=(classify(raw) if legacy is None else legacy).value,
            reason="规则匹配",
        )

        if kind == IntentKind.TASK:
            decision.requires_tools = True
            decision.requires_confirmation = True
            # 第四层：说不清要对谁动手就先问，别猜。
            if not target and _has(lowered, _VAGUE):
                decision.confidence = 0.35
                decision.missing_information = "要处理的对象（文件、项目或内容）"
                decision.reason = "任务意图明确但目标含糊"
            else:
                decision.confidence = 0.8
            return decision

        if kind == IntentKind.INFO:
            decision.requires_tools = True
            decision.requires_confirmation = False
            return decision

        if kind == IntentKind.MEMORY:
            decision.confidence = 0.85
            return decision

        decision.requires_confirmation = False
        return decision
# ============================================================================
# V3（阶段 B）：结构化路由。
# 规则照旧只做轻量判断，不调用模型；模型不可用时这套兜底照样能用。
# 旧接口 classify / is_stop / is_confirm 保持不变，新增 decide() 给 Agent 用。
# ============================================================================


class IntentKind(str, Enum):
    """结构化意图。比旧的 Intent 更细，旧枚举继续保留给老调用方。"""

    CHAT = "chat"
    CONSULT = "consult"          # 告诉我怎么做，不等于替我动手
    MEMORY = "memory"
    INFO = "info"                # 信息查询：天气、新闻、日程
    TASK = "task"
    SUPPLEMENT = "supplement"    # 补充信息：回答任务的追问
    CONFIRM = "confirm"
    CANCEL = "cancel"
    FEEDBACK = "feedback"        # 当面评价桌宠的说法
    LOCAL = "local"              # 确定性本地能力，用不着模型
    UNKNOWN = "unknown"


INTENT_KIND_LABELS = {
    IntentKind.CHAT.value: "闲聊",
    IntentKind.CONSULT.value: "咨询",
    IntentKind.MEMORY.value: "记忆",
    IntentKind.INFO.value: "查信息",
    IntentKind.TASK.value: "任务",
    IntentKind.SUPPLEMENT.value: "补充信息",
    IntentKind.CONFIRM.value: "确认",
    IntentKind.CANCEL.value: "取消",
    IntentKind.FEEDBACK.value: "反馈",
    IntentKind.LOCAL.value: "本地能力",
    IntentKind.UNKNOWN.value: "不确定",
}

# 中文里指代不明的说法。光靠这些词判断不出要对哪个对象动手，就该问。
_VAGUE = ("那个", "这个", "它", "东西", "一下", "搞一下", "弄一下", "帮我弄", "帮我搞")

# 这些词说明用户要的是解释和方法，不是让我动手改东西。
_CONSULT_EXTRA = (
    "分析", "看看", "检查", "排查", "解释", "建议", "评估", "review", "说明一下",
    "帮我看看", "帮我分析", "告诉我", "怎么做",
)

_QUOTED_RE = re.compile(r"[「『“\"']([^」』”\"']{1,40})[」』”\"']")
_ACTION_RE = re.compile(
    r"(修改|改一下|改成|重写|重构|修复|删除|删掉|创建|新建|生成|运行|执行|"
    r"安装|部署|提交|打包|编译|迁移|分析|看看|检查|排查)"
)


def _target(text):
    """从话里抠出要动手的对象。抠不出来就返回空串，交给上层去问。"""
    match = _PATH_RE.search(text)
    if match:
        return match.group(0)
    match = _QUOTED_RE.search(text)
    if match:
        return match.group(1).strip()
    return ""


def _action(text):
    match = _ACTION_RE.search(text)
    return match.group(1) if match else ""


def _kind_of(legacy, lowered):
    """把旧的四分类映射成结构化意图。"""
    if legacy == Intent.TASK:
        return IntentKind.TASK
    if legacy == Intent.MEMORY:
        return IntentKind.MEMORY
    if legacy == Intent.SAFE_TOOL:
        return IntentKind.INFO
    if (
        _has(lowered, _DISCUSS)
        or _has(lowered, _CAPABILITY)
        or _has(lowered, _CONSULT_EXTRA)
        or _is_write_request(lowered)
    ):
        return IntentKind.CONSULT
    return IntentKind.CHAT


@dataclass
class RouteDecision:
    """本轮怎么走的可审计结论。只给程序和开发者面板看，不发给模型。"""

    intent: str = IntentKind.CHAT.value
    confidence: float = 0.5
    target: str = ""
    requested_action: str = ""
    requires_tools: bool = False
    requires_confirmation: bool = False
    missing_information: str = ""
    session_relation: str = ""
    reason: str = ""
    legacy: str = Intent.CHAT.value

    @property
    def label(self):
        return INTENT_KIND_LABELS.get(self.intent, self.intent)

    def to_dict(self):
        return {
            "intent": self.intent,
            "intent_label": self.label,
            "confidence": round(float(self.confidence), 2),
            "target": self.target,
            "requested_action": self.requested_action,
            "requires_tools": self.requires_tools,
            "requires_confirmation": self.requires_confirmation,
            "missing_information": self.missing_information,
            "session_relation": self.session_relation,
            "reason": self.reason,
            "legacy_intent": self.legacy,
        }
