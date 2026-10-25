"""Skill 路由与组合计划。

规则判断在前，不调用模型。低置信度、目标冲突或信息不足时先问，不硬猜。
"""

import re
from dataclasses import dataclass, field

from skills.schema import SkillStep
from tools.permission import normalize_level

# 意图 -> 更擅长的 Skill。命中只加分，不封死。
INTENT_AFFINITY = {
    "task": ("coding-agent", "git-review", "office-agent"),
    "info": ("research-agent", "file-knowledge", "personal-workflow", "knowledge-base"),
    "consult": ("study-agent", "research-agent", "coding-agent"),
    "memory": ("file-knowledge", "personal-workflow", "knowledge-base"),
    "local": ("personal-workflow",),
}

# 关键词 -> 技能名片段。用户导入的技能一般只写 description，没有中文触发词，
# 这里按名字片段兜底，避免新导入的技能永远得不到推荐。
NAME_HINTS = (
    (("界面", "配色", "视觉", "样式", "排版", "布局", "主题", "ui", "ux"), ("ui", "ux", "design", "frontend")),
    (("踩坑", "坑", "复盘", "纠正", "反思", "教训", "错误", "改进", "记录"), ("improve", "retro", "self")),
    (("资料", "检索", "研究", "调研", "来源", "引用"), ("research", "knowledge")),
    (("代码", "项目", "重构", "调试", "报错"), ("coding", "code", "dev")),
    (("文档", "表格", "幻灯片", "演示"), ("office", "doc")),
    (("学习", "讲解", "题目", "复习"), ("study", "learn")),
    (("日程", "待办", "提醒", "计时"), ("workflow", "remind", "schedule")),
)

_EXPLICIT = (
    re.compile(r"/skill\s+([a-z0-9_-]{2,40})"),
    re.compile(r"用\s*([a-z0-9_-]{2,40})\s*(?:技能|skill)"),
    re.compile(r"([a-z0-9_-]{2,40})\s*技能"),
)


def hint_names(registry, text):
    """按关键词和技能名片段兜底匹配。只认真正注册过的技能。"""
    lowered = str(text or "").lower()
    if not lowered:
        return set()
    hits = set()
    for keywords, fragments in NAME_HINTS:
        if not any(word in lowered for word in keywords):
            continue
        for meta in registry.enabled():
            name = str(getattr(meta, "name", "") or "").lower()
            if any(fragment in name for fragment in fragments):
                hits.add(meta.name)
    return hits


@dataclass
class SkillCandidate:
    name: str
    score: float
    reason: str = ""
    confidence: float = 0.5
    permissions: tuple = ()
    tools: tuple = ()
    description: str = ""

    def to_dict(self):
        return {
            "name": self.name,
            "score": round(float(self.score), 2),
            "reason": self.reason,
            "confidence": round(float(self.confidence), 2),
            "permissions": list(self.permissions),
            "tools": list(self.tools),
            "description": self.description,
        }


@dataclass
class PlanStep:
    index: int
    title: str
    tool: str = ""
    permission: str = ""
    timeout: float = 30.0
    retries: int = 0
    verify: str = ""

    def to_dict(self):
        return {
            "index": self.index,
            "title": self.title,
            "tool": self.tool,
            "permission": self.permission,
            "timeout": self.timeout,
            "retries": self.retries,
            "verify": self.verify,
        }


@dataclass
class SkillPlan:
    skill: str
    request: str = ""
    steps: list = field(default_factory=list)
    timeout: float = 120.0
    confidence: float = 0.5
    reason: str = ""
    needs_input: str = ""

    def to_dict(self):
        return {
            "skill": self.skill,
            "request": self.request[:200],
            "steps": [step.to_dict() for step in self.steps],
            "timeout": self.timeout,
            "confidence": round(float(self.confidence), 2),
            "reason": self.reason,
            "needs_input": self.needs_input,
        }

    @property
    def tools(self):
        return [step.tool for step in self.steps if step.tool]


class SkillRouter:
    """按意图、上下文和能力挑候选 Skill，并给出有限执行计划。"""

    MIN_CONFIDENCE = 0.4

    def __init__(self, registry, loader=None, memory=None, stats=None, logger=None, learning=None):
        self.registry = registry
        self.loader = loader
        self.memory = memory
        self.learning = learning
        self.stats = stats or (lambda skill=None: {})
        self.logger = logger
        self.last_candidates = []

    # ---------- 明确点名 ----------
    def explicit(self, text):
        lowered = str(text or "").lower()
        if not lowered:
            return ""
        known = self.registry.names()
        for pattern in _EXPLICIT:
            match = pattern.search(lowered)
            if not match:
                continue
            name = match.group(1)
            if name in known:
                return name
        return ""

    # ---------- 推荐 ----------
    def recommend(self, text, intent="", limit=3, context=""):
        """返回按分数排的候选。分数太低就返回空列表，宁可不推荐。"""
        text = str(text or "")
        if not text.strip():
            return []
        candidates = []
        seen = set()
        base = {name: 3.0 for name in self.registry.search(text, limit=limit + 3)}
        for name in base:
            seen.add(name)
        affinity = INTENT_AFFINITY.get(str(intent or ""), ())
        for name in affinity:
            if self.registry.get(name) is not None:
                base.setdefault(name, 0.0)
                seen.add(name)
        hints = hint_names(self.registry, text)
        for name in hints:
            if self.registry.get(name) is not None:
                base.setdefault(name, 0.0)
                seen.add(name)
        stats = self.stats() or {}
        for name in seen:
            meta = self.registry.get(name)
            if meta is None or not meta.enabled:
                continue
            score = base.get(name, 0.0)
            reasons = []
            if score:
                reasons.append("命中了触发词")
            if name in affinity:
                score += 2.0
                reasons.append("适合当前意图")
            if name in hints and name not in affinity:
                score += 2.0
                reasons.append("名字和需求对得上")
            bucket = stats.get(name) or {}
            rate = bucket.get("success_rate")
            if rate is not None:
                score += float(rate) * 1.5
                reasons.append("历史成功率 %.0f%%" % (float(rate) * 100))
            if self.learning is not None:
                boost, why = self.learning.rank_boost(name)
                score += boost
                if why:
                    reasons.append(why)
            if context and str(context).lower() in str(meta.description).lower():
                score += 0.5
            if score < 1.0:
                continue
            candidates.append(
                SkillCandidate(
                    name=name,
                    score=score,
                    reason="、".join(reasons) or "规则命中",
                    confidence=min(0.95, 0.35 + score / 10.0),
                    permissions=self.permissions_of(meta),
                    tools=tuple(meta.tools),
                    description=meta.description,
                )
            )
        candidates.sort(key=lambda item: (-item.score, item.name))
        self.last_candidates = candidates[:limit]
        return list(self.last_candidates)

    def permissions_of(self, meta):
        levels = [normalize_level(getattr(meta, "permission", "READ"))]
        for step in getattr(meta, "steps", ()) or ():
            if getattr(step, "permission", ""):
                levels.append(normalize_level(step.permission))
        return tuple(sorted(set(levels)))

    # ---------- 计划 ----------
    def plan(self, name, request="", inputs=None):
        meta = self.registry.get(name)
        if meta is None:
            return SkillPlan(skill=str(name or ""), request=request, reason="没有这个 Skill")
        steps = []
        declared_tools = set(str(tool) for tool in (meta.tools or ()))
        for index, step in enumerate(meta.steps or (), start=1):
            tool = getattr(step, "tool", "")
            permission = normalize_level(getattr(step, "permission", "") or meta.permission)
            retryable = permission in ("READ", "LOW")
            steps.append(
                PlanStep(
                    index=index,
                    title=getattr(step, "title", ""),
                    tool=tool,
                    permission=permission,
                    timeout=60.0 if permission in ("HIGH", "CRITICAL") else 30.0,
                    retries=1 if retryable and tool else 0,
                    verify="工具自带校验" if tool else "由 Skill 报告",
                )
            )
        missing = []
        for tool in declared_tools:
            if not any(step.tool == tool for step in steps):
                missing.append(tool)
        plan = SkillPlan(
            skill=meta.name,
            request=str(request or ""),
            steps=steps,
            timeout=float(meta.timeout or 120.0),
            confidence=0.7,
            reason="按 Skill 声明的步骤生成",
        )
        if not meta.steps:
            plan.needs_input = "这个 Skill 没有可执行步骤，只能当说明文档看"
        if missing:
            plan.reason += "；声明了但计划里没出现的工具：%s" % ", ".join(sorted(missing))
        return plan

    def verify_plan(self, plan, results):
        """按计划检查每一步的真实结果。没验证过就不报完成。"""
        checks = []
        for step in plan.steps:
            score = None
            for item in results:
                if getattr(item, "tool", "") == step.tool and step.tool:
                    score = item
            if score is None:
                checks.append({"step": step.index, "verified": False, "note": "没有对应执行记录"})
            else:
                checks.append(
                    {
                        "step": step.index,
                        "verified": bool(getattr(score, "verified", False)),
                        "note": "已校验" if getattr(score, "verified", False) else "未校验",
                    }
                )
        return checks
