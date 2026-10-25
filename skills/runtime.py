"""Skill 运行时的数据契约：上下文、结果和异常。

Skill 实现只依赖这个文件，不直接碰权限、数据库或模型。
"""

import time
from dataclasses import dataclass, field


class SkillError(Exception):
    """Skill 执行不下去。由执行层如实转成错误，绝不伪造成功。"""


class SkillCancelled(SkillError):
    """用户取消了任务。后续步骤一律不再执行。"""


@dataclass
class StepResult:
    title: str
    tool: str = ""
    ok: bool = False
    allowed: bool = True
    detail: str = ""
    error: str = ""
    seconds: float = 0.0
    verified: bool = False

    def to_dict(self):
        return {
            "title": self.title,
            "tool": self.tool,
            "ok": bool(self.ok),
            "allowed": bool(self.allowed),
            "detail": str(self.detail)[:400],
            "error": self.error,
            "seconds": round(float(self.seconds), 3),
            "verified": bool(self.verified),
        }


@dataclass
class SkillResult:
    skill: str
    ok: bool
    summary: str = ""
    detail: str = ""
    error: str = ""
    steps: list = field(default_factory=list)
    tools: list = field(default_factory=list)
    seconds: float = 0.0
    tokens: int = 0
    verified: bool = False
    artifacts: list = field(default_factory=list)
    cancelled: bool = False
    reason: str = ""

    def to_dict(self):
        return {
            "skill": self.skill,
            "ok": bool(self.ok),
            "summary": self.summary,
            "detail": self.detail,
            "error": self.error,
            "steps": [step.to_dict() if hasattr(step, "to_dict") else step for step in self.steps],
            "tools": list(self.tools),
            "seconds": round(float(self.seconds), 3),
            "tokens": int(self.tokens),
            "verified": bool(self.verified),
            "artifacts": list(self.artifacts),
            "cancelled": bool(self.cancelled),
            "reason": self.reason,
        }

    @property
    def model_text(self):
        """给模型看的紧凑结果，长度受限。"""
        head = self.summary or self.error or "没有结果"
        return str(head)[:600]


class SkillContext:
    """Skill 跑一次拿到的全部东西。所有动作都要经过 tool()。"""

    def __init__(
        self,
        meta,
        request="",
        inputs=None,
        tool_call=None,
        cancelled=None,
        emit=None,
        ask=None,
        memory=None,
        config=None,
        workspace="",
        logger=None,
        model=None,
        max_tool_calls=20,
    ):
        self.meta = meta
        self.request = str(request or "")
        self.inputs = dict(inputs or {})
        self.workspace = str(workspace or "")
        self.memory = memory
        self.config = config
        self.logger = logger
        self.model = model  # 可选：向陪伴模型要一段文字，没有就返回 None
        self.steps = []
        self.tool_calls = 0
        self.tokens = 0
        self.max_tool_calls = int(max_tool_calls or 20)
        self._tool_call = tool_call
        self._cancelled = cancelled or (lambda: False)
        self._emit = emit
        self._ask = ask
        self.started_at = time.monotonic()

    # ---------- 状态 ----------
    @property
    def cancelled(self):
        try:
            return bool(self._cancelled())
        except Exception:
            return False

    @property
    def name(self):
        return getattr(self.meta, "name", "")

    def elapsed(self):
        return time.monotonic() - self.started_at

    def emit(self, text):
        if self._emit is not None:
            try:
                self._emit(str(text))
            except Exception:
                pass
        return text

    def ask(self, question):
        """向用户提问。没有可提问的界面时返回 None，由 Skill 自己降级。"""
        if self._ask is None:
            return None
        try:
            return self._ask(str(question))
        except Exception:
            return None

    def note(self, title, detail="", ok=True, tool="", error="", seconds=0.0, verified=False):
        step = StepResult(
            title=str(title),
            tool=str(tool),
            ok=bool(ok),
            detail=str(detail),
            error=str(error),
            seconds=float(seconds),
            verified=bool(verified),
        )
        self.steps.append(step)
        self.emit(step.title)
        return step

    # ---------- 工具 ----------
    def tool(self, name, **arguments):
        """唯一动作入口。声明白名单 -> 执行层做权限判断 -> 记录。"""
        if self.cancelled:
            raise SkillCancelled("用户已取消")
        name = str(name or "")
        declared = [str(item) for item in (getattr(self.meta, "tools", ()) or ())]
        if declared and name not in declared:
            raise SkillError("Skill %s 没有声明工具 %s" % (self.name, name))
        if self.tool_calls >= self.max_tool_calls:
            raise SkillError("工具调用次数到上限了（%d）" % self.max_tool_calls)
        self.tool_calls += 1
        started = time.monotonic()
        if self._tool_call is None:
            raise SkillError("当前没有接上工具执行层")
        outcome = self._tool_call(name, arguments)
        seconds = time.monotonic() - started
        ok = bool(getattr(outcome, "ok", False))
        self.note(
            title="调用工具 %s" % name,
            tool=name,
            ok=ok,
            detail=str(getattr(outcome, "result", "") or "")[:200],
            error=str(getattr(outcome, "error", "") or ""),
            seconds=seconds,
            verified=bool(getattr(outcome, "verified", False)),
        )
        return outcome

    def data(self, name, **arguments):
        """要文本结果的小工具。失败时抛 SkillError，不返回半成品。"""
        outcome = self.tool(name, **arguments)
        if not getattr(outcome, "ok", False):
            raise SkillError(str(getattr(outcome, "error", "") or "工具失败"))
        return getattr(outcome, "result", "")

    def snapshot(self):
        return {
            "skill": self.name,
            "request": self.request[:200],
            "steps": [step.to_dict() for step in self.steps],
            "tool_calls": self.tool_calls,
            "seconds": round(self.elapsed(), 3),
        }
