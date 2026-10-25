"""Skill 执行器：跑一个 Skill，记录每一步，出错了如实说。

所有动作都要经过 ToolExecutor（它就是权限、校验和审计的唯一入口）。
执行器自己不做权限判断，也不伪造任何结果。
"""

import importlib.util
import os
import threading
import time

from skills.metrics import Timings
from skills.runtime import SkillCancelled, SkillContext, SkillError, SkillResult
from skills.validator import SkillValidator

DEFAULT_TIMEOUT = 120.0
MAX_TOOL_CALLS = 20


class SkillExecutor:
    def __init__(
        self,
        registry,
        loader=None,
        tools=None,
        memory=None,
        config=None,
        store=None,
        metrics=None,
        logger=None,
        audit=None,
        validator=None,
        tools_registry=None,
    ):
        self.registry = registry
        self.loader = loader
        self.tools = tools
        self.memory = memory
        self.config = config
        self.store = store
        self.metrics = metrics or Timings()
        self.logger = logger
        self.audit = audit
        # 校验工具名要用工具注册表，不是 Skill 注册表。
        self.validator = validator or SkillValidator(tools=tools_registry or tools)
        self._modules = {}
        self._cancel = threading.Event()
        self.last_result = None

    # ---------- 取消 ----------
    def cancel(self):
        self._cancel.set()
        return True

    def reset(self):
        self._cancel.clear()

    @property
    def cancelled(self):
        return self._cancel.is_set()

    # ---------- 执行 ----------
    def run(self, name, request="", inputs=None, emit=None, ask=None, workspace="", model=None):
        self.reset()
        name = str(name or "")
        started = time.monotonic()
        meta = self.registry.get(name)
        if meta is None and self.loader is not None:
            meta = self.loader.load(name)
        if meta is None:
            return self._finish(
                SkillResult(skill=name, ok=False, error="没有这个 Skill：%s" % name), request, started
            )
        if not getattr(meta, "enabled", True):
            report = self._report(meta)
            reason = report.blocked or "已禁用"
            return self._finish(
                SkillResult(skill=name, ok=False, error="这个 Skill 当前不可用：%s" % reason),
                request,
                started,
            )
        report = self._report(meta)
        if not report.ok:
            reason = report.blocked or "; ".join(report.errors)
            self._audit("skill_blocked", name, reason)
            return self._finish(
                SkillResult(skill=name, ok=False, error="校验没通过，不能执行：%s" % reason),
                request,
                started,
            )

        handler = self._handler(meta)
        if handler is None:
            return self._finish(
                SkillResult(
                    skill=name,
                    ok=False,
                    error="这个 Skill 只有说明，没有可执行的实现路径",
                    reason="说明型 Skill",
                ),
                request,
                started,
            )

        context = SkillContext(
            meta,
            request=request,
            inputs=inputs,
            tool_call=self._tool_call,
            cancelled=lambda: self.cancelled,
            emit=emit,
            ask=ask,
            memory=self.memory,
            config=self.config,
            workspace=workspace or self._workspace(),
            logger=self.logger,
            model=model,
            max_tool_calls=MAX_TOOL_CALLS,
        )
        self._audit("skill_start", name, str(request)[:120])
        try:
            with self.metrics.timed("skill.%s" % name):
                raw = handler(context)
        except SkillCancelled as exc:
            result = SkillResult(
                skill=name,
                ok=False,
                error=str(exc) or "已取消",
                steps=list(context.steps),
                cancelled=True,
            )
            result.tools = self._tools_used(context)
            return self._finish(result, request, started)
        except SkillError as exc:
            result = SkillResult(
                skill=name, ok=False, error=str(exc), steps=list(context.steps)
            )
            result.tools = self._tools_used(context)
            return self._finish(result, request, started)
        except Exception as exc:
            self._warn("skill %s raised: %s", name, exc)
            result = SkillResult(
                skill=name, ok=False, error="执行失败：%s" % exc, steps=list(context.steps)
            )
            result.tools = self._tools_used(context)
            return self._finish(result, request, started)

        result = self._normalize(name, raw, context)
        result.tokens = int(context.tokens or 0)
        self._audit("skill_done", name, "%s in %.2fs" % ("ok" if result.ok else "fail", result.seconds))
        return self._finish(result, request, started)

    def _normalize(self, name, raw, context):
        if isinstance(raw, SkillResult):
            raw.skill = raw.skill or name
            if not raw.steps:
                raw.steps = list(context.steps)
            raw.tools = list(raw.tools or self._tools_used(context))
            return raw
        data = raw if isinstance(raw, dict) else {}
        result = SkillResult(
            skill=name,
            ok=bool(data.get("ok", True)),
            summary=str(data.get("summary") or ""),
            detail=str(data.get("detail") or ""),
            error=str(data.get("error") or ""),
            verified=bool(data.get("verified", False)),
            artifacts=list(data.get("artifacts") or []),
            steps=list(context.steps),
            tools=self._tools_used(context),
        )
        return result

    def _tools_used(self, context):
        seen = []
        for step in context.steps:
            if step.tool and step.tool not in seen:
                seen.append(step.tool)
        return seen

    def _finish(self, result, request, started):
        result.seconds = time.monotonic() - started
        self.last_result = result
        if self.store is not None:
            self.store.save(result, request=request)
        return result

    # ---------- 工具 ----------
    def _tool_call(self, name, arguments):
        if self.tools is None:
            raise SkillError("当前没有接上工具执行层")
        if self.cancelled:
            raise SkillCancelled("用户已取消")
        outcome = self.tools.execute(name, arguments or {})
        if self.logger:
            self.logger.info(
                "skill tool %s: ok=%s level=%s", name, getattr(outcome, "ok", False),
                getattr(outcome, "level", ""),
            )
        return outcome

    # ---------- 实现模块 ----------
    def _handler(self, meta):
        entry = str(getattr(meta, "entry", "") or "")
        path = str(getattr(meta, "path", "") or "")
        if not entry or not path:
            return None
        target = os.path.join(path, entry)
        if not os.path.isfile(target):
            return None
        key = "%s:%s" % (meta.name, target)
        module = self._modules.get(key)
        if module is None:
            try:
                spec = importlib.util.spec_from_file_location("deskpet_skill_%s" % meta.name, target)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            except Exception as exc:
                self._warn("skill %s implementation failed to load: %s", meta.name, exc)
                self._audit("skill_entry_error", meta.name, str(exc))
                return None
            self._modules[key] = module
        return getattr(module, "run", None)

    def _report(self, meta):
        report = None
        if self.loader is not None:
            report = self.loader.report_of(getattr(meta, "name", ""))
        if report is None:
            report = self.validator.validate(meta)
        return report

    def _workspace(self):
        config = self.config
        if config is None:
            return os.getcwd()
        for attr in ("workspace_root", "project_root", "data_root", "program_dir"):
            value = getattr(config, attr, None)
            if value:
                return str(value)
        return os.getcwd()

    def _audit(self, kind, name, detail):
        if self.audit is not None:
            try:
                self.audit.record(kind=kind, name=name, level="", decision=kind, detail=detail)
            except Exception:
                pass

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)
