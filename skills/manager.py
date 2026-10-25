"""Skill 管理器：把注册表、加载器、校验器、路由器、执行器和统计装成一套。

Agent 只跟这里打交道，UI 也只读这里。任何一步不可用都降级，不拖垮主程序。
"""

from skills.executor import SkillExecutor
from skills.learning import SkillLearning
from skills.loader import SkillLoader
from skills.metrics import Timings
from skills.registry import SkillRegistry
from skills.router import SkillRouter
from skills.store import SkillRunStore
from skills.validator import SkillValidator

USER_SKILL_DIR = "skills"


class SkillManager:
    def __init__(
        self,
        registry=None,
        loader=None,
        validator=None,
        router=None,
        executor=None,
        store=None,
        learning=None,
        metrics=None,
        logger=None,
        roots=(),
        tools=None,
    ):
        self.registry = registry or SkillRegistry()
        self.loader = loader
        self.validator = validator or SkillValidator(tools=tools, roots=roots)
        self.store = store
        self.metrics = metrics or Timings()
        self.learning = learning or SkillLearning(logger=logger)
        self.router = router or SkillRouter(
            self.registry, loader=self.loader, learning=self.learning,
            stats=self.stats, logger=logger,
        )
        self.executor = executor or SkillExecutor(
            self.registry, loader=self.loader, store=self.store, metrics=self.metrics,
            validator=self.validator, logger=logger,
        )
        self.logger = logger

    # ---------- 装配 ----------
    @classmethod
    def build(cls, config=None, tools=None, memory=None, logger=None, database=None):
        registry = SkillRegistry()
        roots = []
        user_dir = None
        if config is not None:
            user_dir = getattr(config, "skills_dir", None)
            if user_dir:
                user_dir = str(user_dir)
                roots.append(user_dir)
        loader = SkillLoader(roots=roots, registry=registry, logger=logger, tools=tools)
        validator = SkillValidator(tools=tools, roots=loader.roots)
        loader.validator = validator
        store = SkillRunStore(database, logger=logger) if database is not None else None
        learning = SkillLearning(memory=memory, store=store, logger=logger)
        router = SkillRouter(
            registry, loader=loader, memory=memory, learning=learning, logger=logger
        )
        router.stats = store.stats if store is not None else (lambda skill=None: {})
        executor = SkillExecutor(
            registry, loader=loader, tools=tools, memory=memory, config=config,
            store=store, validator=validator, logger=logger,
        )
        manager = cls(
            registry=registry, loader=loader, validator=validator, router=router,
            executor=executor, store=store, learning=learning, metrics=executor.metrics,
            logger=logger, roots=loader.roots,
        )
        if user_dir:
            try:
                import os

                os.makedirs(user_dir, exist_ok=True)
            except OSError as exc:
                if logger:
                    logger.warning("skills dir unavailable: %s", exc)
        try:
            manager.reload()
        except Exception as exc:
            if logger:
                logger.warning("skill discovery failed: %s", exc)
        return manager

    # ---------- 读 ----------
    def reload(self):
        self.registry._skills.clear()
        self.registry._order.clear()
        if self.loader is not None:
            self.loader.invalidate()
        loaded = self.loader.load_all() if self.loader is not None else []
        return loaded

    def rows(self):
        return [meta.to_row() for meta in self.registry.all()]

    def names(self):
        return self.registry.names()

    def get(self, name):
        return self.registry.get(name)

    def recommend(self, text, intent="", limit=3):
        if self.learning is not None and not self.learning.auto_match_enabled():
            return []
        return self.router.recommend(text, intent=intent, limit=limit)

    def plan(self, name, request="", inputs=None):
        return self.router.plan(name, request=request, inputs=inputs)

    def explicit(self, text):
        return self.router.explicit(text)

    def stats(self, skill=None):
        if self.store is None:
            return {}
        return self.store.stats(skill)

    def runs(self, limit=50, skill=None):
        if self.store is None:
            return []
        return self.store.recent(limit=limit, skill=skill)

    def report_of(self, name):
        if self.loader is None:
            return None
        return self.loader.report_of(name)

    def instruction(self, name, reference=""):
        if self.loader is None:
            return ""
        return self.loader.instruction(name, reference)

    def references(self, name):
        if self.loader is None:
            return []
        return self.loader.references(name)

    # ---------- 写 ----------
    def enable(self, name, flag=True):
        return self.registry.enable(name, flag)

    def disable(self, name):
        return self.registry.disable(name)

    def import_skill(self, source_dir):
        if self.loader is None:
            return {"ok": False, "error": "没有可用的加载器"}
        return self.loader.import_from(source_dir, target_root=self.loader.roots[0])

    def run(self, name, request="", inputs=None, emit=None, ask=None, model=None):
        return self.executor.run(
            name, request=request, inputs=inputs, emit=emit, ask=ask, model=model
        )

    def cancel(self):
        return self.executor.cancel()

    @property
    def cancelled(self):
        return self.executor.cancelled

    def favorite(self, name, flag=True):
        return self.learning.favorite(name, flag)

    def set_auto_match(self, flag):
        return self.learning.set_auto_match(flag)

    def remember_workflow(self, skill, steps, note=""):
        return self.learning.remember_workflow(skill, steps, note=note)

    # ---------- 视图 ----------
    def status_text(self):
        enabled = len(self.registry.enabled())
        total = self.registry.count()
        if total == 0:
            return "还没有可用的 Skill。"
        return "已装 %d 个 Skill，其中 %d 个启用。" % (total, enabled)

    def developer_rows(self):
        rows = []
        for meta in self.registry.all():
            report = self.report_of(meta.name)
            blocked = getattr(report, "blocked", "") if report is not None else ""
            rows.append(
                {
                    "name": meta.name,
                    "version": meta.version,
                    "permission": meta.permission,
                    "tools": ", ".join(meta.tools) or "无",
                    "steps": len(meta.steps),
                    "enabled": meta.enabled,
                    "blocked": blocked,
                }
            )
        return rows
