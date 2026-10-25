"""Skill 学习与个性化。

只把用户认可的做事方式记成偏好，不碰权限：临时授权不会因此变成永久权限。
"""

import json

WORKFLOW_PREFIX = "skill_workflow_"
PROJECT_KEY = "skill_project_hints"
FAVORITE_KEY = "skill_favorites"
DISABLED_AUTO_KEY = "skill_auto_match"


class SkillLearning:
    """读写用户偏好，给路由提供「这个人喜欢怎么做」的分数。"""

    def __init__(self, memory=None, store=None, logger=None):
        self.memory = memory
        self.store = store
        self.logger = logger

    # ---------- 偏好 ----------
    def _get(self, key):
        if self.memory is None:
            return None
        try:
            return self.memory.get_preference(key)
        except Exception:
            return None

    def _set(self, key, value):
        if self.memory is None:
            return False
        try:
            self.memory.set_preference(key, value)
            return True
        except Exception as exc:
            self._warn("skill preference write failed: %s", exc)
            return False

    def _json(self, key, default):
        raw = self._get(key)
        if not raw:
            return default
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return default
        return data if isinstance(data, type(default)) else default

    # ---------- 工作流模板 ----------
    def remember_workflow(self, skill, steps, note=""):
        """把用户认可的步骤顺序存成模板。只存步骤标题，不存任何权限。"""
        name = str(skill or "").strip()
        if not name:
            return False
        clean = [str(step).strip() for step in (steps or []) if str(step).strip()]
        if not clean:
            return False
        payload = {"steps": clean, "note": str(note or "")[:200]}
        return self._set(WORKFLOW_PREFIX + name, json.dumps(payload, ensure_ascii=False))

    def workflow(self, skill):
        data = self._json(WORKFLOW_PREFIX + str(skill or ""), {})
        return list(data.get("steps") or [])

    def workflows(self, names=()):
        found = {}
        for name in list(names) or self._favorite_names():
            steps = self.workflow(name)
            if steps:
                found[str(name)] = steps
        return found

    def forget_workflow(self, skill):
        return self._set(WORKFLOW_PREFIX + str(skill or ""), "")

    # ---------- 项目与偏好 ----------
    def note_project(self, path, language="", rules=""):
        data = self._json(PROJECT_KEY, {})
        key = str(path or "").strip()
        if not key:
            return False
        data[key] = {"language": str(language or ""), "rules": str(rules or "")[:300]}
        return self._set(PROJECT_KEY, json.dumps(data, ensure_ascii=False))

    def project_hints(self):
        return self._json(PROJECT_KEY, {})

    def favorite(self, skill, flag=True):
        names = list(self._json(FAVORITE_KEY, []))
        name = str(skill or "")
        if not name:
            return False
        if flag and name not in names:
            names.append(name)
        if not flag and name in names:
            names.remove(name)
        return self._set(FAVORITE_KEY, json.dumps(names, ensure_ascii=False))

    def _favorite_names(self):
        return [str(name) for name in self._json(FAVORITE_KEY, [])]

    def favorites(self):
        return self._favorite_names()

    def auto_match_enabled(self):
        value = self._get(DISABLED_AUTO_KEY)
        if value is None:
            return True
        return str(value).strip().lower() not in {"0", "false", "no", "off", ""}

    def set_auto_match(self, flag):
        return self._set(DISABLED_AUTO_KEY, "1" if flag else "0")

    # ---------- 排序加成 ----------
    def rank_boost(self, skill):
        """返回 (加分, 原因)。加分只影响推荐顺序，不影响权限。"""
        name = str(skill or "")
        boost = 0.0
        reasons = []
        if name in self._favorite_names():
            boost += 1.5
            reasons.append("你标记过常用")
        if self._json(WORKFLOW_PREFIX + name, {}).get("steps"):
            boost += 0.5
            reasons.append("有你保存的工作流")
        if self.store is not None:
            bucket = (self.store.stats(name) or {}).get(name) or {}
            failures = bucket.get("failures") or {}
            if failures and bucket.get("runs"):
                failure_ratio = sum(failures.values()) / max(1, int(bucket["runs"]))
                if failure_ratio > 0.5:
                    boost -= 0.5
                    reasons.append("最近失败偏多")
        return boost, "、".join(reasons)

    def summary(self, names=()):
        stats = self.store.stats() if self.store is not None else {}
        return {
            "favorites": self._favorite_names(),
            "auto_match": self.auto_match_enabled(),
            "workflows": sorted(self.workflows(names)),
            "stats": stats,
        }

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)
