"""模型路由：陪伴与任务两个角色的选择集中在这里。

Agent Core 只问「这个角色用哪个 provider」，不关心具体厂商。
缺 Task 不影响 Companion；两个都没配置时上层退回本地能力。
"""

from model.base import ModelRole, ModelSlot


class ModelRouter:
    """把 Companion / Task 两个 ModelSlot 放在一起，按角色取用。"""

    def __init__(self, companion=None, task=None):
        self.companion = self._as_slot(companion, ModelRole.COMPANION)
        self.task = self._as_slot(task, ModelRole.TASK)

    @staticmethod
    def _as_slot(value, role):
        if isinstance(value, ModelSlot):
            return value
        return ModelSlot(role.value, value, configured=value is not None)

    def slot(self, role):
        """按角色取 slot；不认识的角色返回 None，不抛异常。"""
        name = str(getattr(role, "value", role))
        if name == ModelRole.COMPANION.value:
            return self.companion
        if name == ModelRole.TASK.value:
            return self.task
        return None

    def provider(self, role):
        slot = self.slot(role)
        return slot.provider if slot else None

    def available(self, role):
        slot = self.slot(role)
        return bool(slot and slot.available())

    def any_available(self):
        return self.available(ModelRole.COMPANION) or self.available(ModelRole.TASK)

    def status(self):
        return {
            ModelRole.COMPANION.value: self.companion.status(),
            ModelRole.TASK.value: self.task.status(),
        }

    def to_dict(self):
        return {
            "companion": self.companion.to_dict(),
            "task": self.task.to_dict(),
        }
