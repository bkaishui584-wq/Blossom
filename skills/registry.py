"""Skill 注册表：登记、查找、启用、禁用和版本管理。

只存元数据，不存执行逻辑，也不常驻加载任何说明文件。
"""


class SkillRegistry:
    def __init__(self):
        self._skills = {}
        self._order = []

    def register(self, meta):
        name = str(getattr(meta, "name", "") or "")
        if not name:
            raise ValueError("Skill 必须有名字")
        if name not in self._skills:
            self._order.append(name)
        self._skills[name] = meta
        return meta

    def get(self, name):
        return self._skills.get(str(name or ""))

    def names(self):
        return list(self._order)

    def all(self):
        return [self._skills[name] for name in self._order]

    def enabled(self):
        return [meta for meta in self.all() if getattr(meta, "enabled", True)]

    def count(self):
        return len(self._skills)

    def version_of(self, name):
        meta = self.get(name)
        return getattr(meta, "version", "") if meta is not None else ""

    def enable(self, name, flag=True):
        meta = self.get(name)
        if meta is None:
            return False
        meta.enabled = bool(flag)
        return True

    def disable(self, name):
        return self.enable(name, False)

    def uninstall(self, name):
        name = str(name or "")
        if name not in self._skills:
            return False
        del self._skills[name]
        self._order.remove(name)
        return True

    def search(self, query, limit=5):
        """按触发词和说明找 Skill。命不中就不返回，不硬凑。"""
        text = str(query or "").lower().strip()
        if not text:
            return []
        scored = []
        for meta in self.enabled():
            score = 0
            for word in meta.triggers:
                if str(word).lower() in text:
                    score += 3
            if meta.name and meta.name.replace("-", " ").replace("_", " ") in text:
                score += 4
            for token in str(meta.description or "").lower().split():
                if len(token) >= 2 and token in text:
                    score += 1
            if score:
                scored.append((score, meta.name))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [name for _, name in scored[:limit]]

    def rows(self):
        return [meta.to_row() for meta in self.all()]

    def to_dict(self):
        return {meta.name: meta.to_dict() for meta in self.all()}
