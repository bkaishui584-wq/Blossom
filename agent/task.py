"""任务数据结构。任务状态用枚举表达，而不是一堆散落布尔变量。"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class TaskState(str, Enum):
    NEW = "new"
    PLANNING = "planning"
    PENDING_CONFIRMATION = "pending_confirmation"
    RUNNING = "running"
    WAITING_TOOL = "waiting_tool"
    WAITING_USER = "waiting_user"
    WAITING_CONFIRMATION = "waiting_confirmation"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


TASK_STATE_LABELS = {
    TaskState.NEW.value: "新建",
    TaskState.PLANNING.value: "规划中",
    TaskState.PENDING_CONFIRMATION.value: "等待确认",
    TaskState.RUNNING.value: "执行中",
    TaskState.WAITING_TOOL.value: "等待工具",
    TaskState.WAITING_USER.value: "等待用户",
    TaskState.WAITING_CONFIRMATION.value: "等待确认",
    TaskState.VERIFYING.value: "验证中",
    TaskState.COMPLETED.value: "已完成",
    TaskState.FAILED.value: "失败",
    TaskState.TIMEOUT.value: "超时",
    TaskState.CANCELLED.value: "已取消",
}

TERMINAL_STATES = (
    TaskState.COMPLETED.value,
    TaskState.FAILED.value,
    TaskState.TIMEOUT.value,
    TaskState.CANCELLED.value,
)


# 模型调用失败的原因分类。不同原因给用户不同的话，也决定要不要重试。
# 超时不算"执行失败"：它没失败，它只是没在时限内回来。
ERROR_TIMEOUT = "timeout"
ERROR_RATE_LIMIT = "rate_limit"
ERROR_NETWORK = "network"
ERROR_AUTH = "auth"
ERROR_OTHER = "other"

ERROR_KIND_LABELS = {
    ERROR_TIMEOUT: "模型没有在时限内回应",
    ERROR_RATE_LIMIT: "请求太频繁，被限流了",
    ERROR_NETWORK: "网络连不上",
    ERROR_AUTH: "密钥或权限不对",
    ERROR_OTHER: "模型调用失败",
}

# 这几种重试有意义：等一会儿或换个时刻再问就可能成。
RETRYABLE_KINDS = (ERROR_TIMEOUT, ERROR_RATE_LIMIT, ERROR_NETWORK)

_TIMEOUT_HINTS = ("timeout", "timed out", "deadline", "超时")
_RATE_HINTS = ("rate limit", "rate_limit", "429", "too many requests", "限流")
_AUTH_HINTS = ("401", "403", "unauthorized", "invalid api key", "authentication", "密钥")
_NETWORK_HINTS = (
    "connection", "unreachable", "getaddrinfo", "network", "ssl", "proxy", "断网", "连接",
)


def classify_error(text):
    """把模型报的错收成一个类别。认不出来就是 other，不硬猜。"""
    low = str(text or "").lower()
    for needle in _TIMEOUT_HINTS:
        if needle in low:
            return ERROR_TIMEOUT
    for needle in _RATE_HINTS:
        if needle in low:
            return ERROR_RATE_LIMIT
    for needle in _AUTH_HINTS:
        if needle in low:
            return ERROR_AUTH
    for needle in _NETWORK_HINTS:
        if needle in low:
            return ERROR_NETWORK
    return ERROR_OTHER


def error_label(kind):
    return ERROR_KIND_LABELS.get(str(kind or ""), ERROR_KIND_LABELS[ERROR_OTHER])


def retryable(kind):
    return str(kind or "") in RETRYABLE_KINDS


def state_for_error(kind):
    """超时归超时，别的归失败。不把超时说成执行失败。"""
    return TaskState.TIMEOUT.value if str(kind) == ERROR_TIMEOUT else TaskState.FAILED.value


@dataclass
class TaskResult:
    """任务完成后返回给程序与陪伴层的结构化结果。"""

    ok: bool
    request: str = ""
    summary: str = ""
    steps: list = field(default_factory=list)
    changes: list = field(default_factory=list)
    needs_user: bool = False
    error: str = ""
    state: str = TaskState.COMPLETED.value

    @property
    def state_label(self):
        return TASK_STATE_LABELS.get(self.state, self.state)

    def to_dict(self):
        return {
            "ok": self.ok,
            "request": self.request,
            "summary": self.summary,
            "steps": list(self.steps),
            "changes": list(self.changes),
            "needs_user": self.needs_user,
            "error": self.error,
            "state": self.state,
        }


# ============================================================================
# V3（阶段 C/D）：有待办结构的任务计划、边界限制与可持久化记录。
# 简单任务不需要计划，复杂任务才生成步骤表。
# ============================================================================


@dataclass
class TaskLimits:
    """单轮任务的天花板。到顶就暂停或问用户，不许无限循环。"""

    max_rounds: int = 3
    max_tool_calls: int = 8
    max_seconds: int = 180
    max_retries: int = 1

    def to_dict(self):
        return {
            "max_rounds": self.max_rounds,
            "max_tool_calls": self.max_tool_calls,
            "max_seconds": self.max_seconds,
            "max_retries": self.max_retries,
        }


@dataclass
class TaskStep:
    """计划里的一步。tool 为空表示这一步只是推理或等用户回答。"""

    index: int = 0
    title: str = ""
    tool: str = ""
    status: str = "pending"  # pending/running/done/failed/skipped
    attempts: int = 0
    result: str = ""
    error: str = ""
    verify: str = ""

    def to_dict(self):
        return {
            "index": self.index,
            "title": self.title,
            "tool": self.tool,
            "status": self.status,
            "attempts": self.attempts,
            "result": self.result,
            "error": self.error,
            "verify": self.verify,
        }

    @classmethod
    def from_dict(cls, row):
        row = row or {}
        return cls(
            index=int(row.get("index") or 0),
            title=str(row.get("title") or ""),
            tool=str(row.get("tool") or ""),
            status=str(row.get("status") or "pending"),
            attempts=int(row.get("attempts") or 0),
            result=str(row.get("result") or ""),
            error=str(row.get("error") or ""),
            verify=str(row.get("verify") or ""),
        )


@dataclass
class TaskPlan:
    """任务计划：目标、步骤、允许的工具和限制。"""

    goal: str = ""
    steps: list = field(default_factory=list)
    tools: list = field(default_factory=list)
    limits: TaskLimits = field(default_factory=TaskLimits)

    @property
    def current(self):
        for step in self.steps:
            if step.status in ("pending", "running"):
                return step
        return None

    def to_dict(self):
        return {
            "goal": self.goal,
            "steps": [step.to_dict() for step in self.steps],
            "tools": list(self.tools),
            "limits": self.limits.to_dict(),
        }


def new_task_id():
    return "task-" + uuid.uuid4().hex[:12]


def now_stamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class TaskRecord:
    """一条可持久化的任务记录。重启后靠它恢复待续任务。"""

    id: str = field(default_factory=new_task_id)
    goal: str = ""
    state: str = TaskState.NEW.value
    session: str = ""
    project: str = ""
    current_step: int = 0
    pending_question: str = ""
    pending_confirmation: str = ""
    error: str = ""
    attempts: int = 0
    plan: list = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    @property
    def state_label(self):
        return TASK_STATE_LABELS.get(self.state, self.state)

    @property
    def finished(self):
        return self.state in TERMINAL_STATES

    def to_dict(self):
        return {
            "id": self.id,
            "goal": self.goal,
            "state": self.state,
            "state_label": self.state_label,
            "session": self.session,
            "project": self.project,
            "current_step": self.current_step,
            "pending_question": self.pending_question,
            "pending_confirmation": self.pending_confirmation,
            "error": self.error,
            "attempts": self.attempts,
            "plan": list(self.plan),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row):
        import json

        row = row or {}
        try:
            plan = json.loads(row.get("plan") or "[]")
        except (TypeError, ValueError):
            plan = []
        return cls(
            id=str(row.get("id") or ""),
            goal=str(row.get("goal") or ""),
            state=str(row.get("state") or TaskState.NEW.value),
            session=str(row.get("session") or ""),
            project=str(row.get("project") or ""),
            current_step=int(row.get("current_step") or 0),
            pending_question=str(row.get("pending_question") or ""),
            pending_confirmation=str(row.get("pending_confirmation") or ""),
            error=str(row.get("error") or ""),
            attempts=int(row.get("attempts") or 0),
            plan=plan if isinstance(plan, list) else [],
            created_at=str(row.get("created_at") or ""),
            updated_at=str(row.get("updated_at") or ""),
        )
