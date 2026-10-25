"""扩展层：注册表、清单、用户级安装状态和许可证审查记录。

和 Skill 分开：Skill 描述「怎么做一件事」，扩展描述「换掉或补上一个后端」。
这一层只登记事实，不安装、不下载、不执行第三方代码。
"""

from extensions.catalog import builtin_catalog
from extensions.manifest import (
    AUDIENCE_LABELS,
    KIND_LABELS,
    LICENSE_LABELS,
    PERMISSION_LABELS,
    SOURCE_LABELS,
    VERDICT_LABELS,
    ExtensionManifest,
)
from extensions.registry import ExtensionRegistry
from extensions.store import ExtensionStore

__all__ = [
    "AUDIENCE_LABELS",
    "KIND_LABELS",
    "LICENSE_LABELS",
    "PERMISSION_LABELS",
    "SOURCE_LABELS",
    "VERDICT_LABELS",
    "ExtensionManifest",
    "ExtensionRegistry",
    "ExtensionStore",
    "builtin_catalog",
]
