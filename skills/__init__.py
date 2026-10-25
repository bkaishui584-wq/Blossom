"""统一 Skill 系统。

分层：Skill 描述「怎么做」，Tool 执行「一个动作」，Agent 判断目标并选 Skill，
PermissionManager 决定能不能做，Validator 校验结果。Skill 不能绕过任何一层。
"""

from skills.registry import SkillRegistry
from skills.schema import SkillMeta, SkillStep

__all__ = ["SkillMeta", "SkillRegistry", "SkillStep"]


def build_skills(config=None, tools=None, memory=None, logger=None, database=None):
    """装配默认 Skill 系统。任何一步失败都降级为空系统，不拖垮启动。"""
    from skills.manager import SkillManager

    return SkillManager.build(
        config=config, tools=tools, memory=memory, logger=logger, database=database
    )
