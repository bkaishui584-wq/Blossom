"""Skill 校验：结构、依赖、权限、路径和不可信内容。

导入别人的 Skill 时，只信它说的「怎么做」，不信它要求「跳过什么」。
"""

import os

from skills.schema import NAME_RE, scan_untrusted
from tools.permission import PermissionLevel, normalize_level

SAFE_SUFFIXES = (".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".rst")
SCRIPT_SUFFIXES = (
    ".py", ".pyw", ".ps1", ".psm1", ".bat", ".cmd", ".sh",
    ".js", ".mjs", ".cjs", ".ts", ".dll", ".exe", ".jar", ".vbs",
)


def tool_names(source):
    """从工具注册表或执行器里取工具名。取不到返回 None，表示这项不校验。"""
    if source is None:
        return None
    names = getattr(source, "names", None)
    if callable(names):
        try:
            return set(names())
        except Exception:
            return None
    inner = getattr(source, "registry", None)
    if inner is not None:
        return tool_names(inner)
    return None


class ValidationReport:
    """一次校验的结论。ok 才是能加载的 Skill。"""

    def __init__(self, skill=""):
        self.skill = str(skill or "")
        self.errors = []
        self.warnings = []
        self.blocked = ""

    @property
    def ok(self):
        return not self.errors and not self.blocked

    def error(self, text):
        self.errors.append(str(text))
        return self

    def warn(self, text):
        self.warnings.append(str(text))
        return self

    def block(self, text):
        self.blocked = str(text)
        return self

    def to_dict(self):
        return {
            "skill": self.skill,
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "blocked": self.blocked,
        }


def inside(root, target):
    """target 是否在 root 目录里。防止 Skill 用相对路径跑出自己目录。"""
    if not root or not target:
        return False
    try:
        root_path = os.path.realpath(str(root))
        target_path = os.path.realpath(str(target))
    except OSError:
        return False
    if target_path == root_path:
        return True
    return target_path.startswith(root_path + os.sep)


def classify_reference(path):
    """导入时给参考文件分类。脚本一律不自动导入，等用户单独授权。"""
    suffix = os.path.splitext(str(path))[1].lower()
    if suffix in SCRIPT_SUFFIXES:
        return "script"
    if suffix in SAFE_SUFFIXES:
        return "text"
    return "unknown"


class SkillValidator:
    """结构校验 + 安全扫描。没接工具注册表时跳过「工具是否存在」这项。"""

    def __init__(self, tools=None, roots=None):
        self.tools = tools
        self.roots = list(roots or ())

    def validate(self, meta):
        report = ValidationReport(getattr(meta, "name", ""))
        if meta is None:
            report.error("读不到 Skill 定义")
            return report

        name = str(getattr(meta, "name", "") or "")
        if not NAME_RE.match(name):
            report.error("名称只能是小写字母开头，允许小写字母、数字、下划线和短横线")

        try:
            level = normalize_level(getattr(meta, "permission", "READ"))
            PermissionLevel(level)
        except ValueError:
            report.error("权限等级无法识别：%s" % getattr(meta, "permission", ""))
            level = "READ"

        steps = list(getattr(meta, "steps", ()) or ())
        tools = list(getattr(meta, "tools", ()) or ())
        if not steps:
            report.warn("没有声明步骤，只能当说明文档用")

        for step in steps:
            step_tool = getattr(step, "tool", "")
            if step_tool and tools and step_tool not in tools:
                report.error("步骤用了没在「允许工具」里声明的工具：%s" % step_tool)

        known = tool_names(self.tools)
        if known is not None and tools:
            unknown = [tool for tool in tools if tool not in known]
            if unknown:
                report.error("声明的工具不存在：%s" % ", ".join(sorted(unknown)))

        entry = str(getattr(meta, "entry", "") or "")
        if entry:
            path = str(getattr(meta, "path", "") or "")
            if path:
                target = os.path.join(path, entry)
                if not inside(path, target):
                    report.error("实现文件跑出 Skill 目录了")
                elif not os.path.isfile(target):
                    report.error("找不到实现文件：%s" % entry)

        hits = tuple(getattr(meta, "untrusted", ()) or ())
        if hits:
            report.block(
                "内容里出现要求绕过权限或泄露数据的字样：%s" % ", ".join(sorted(set(hits)))
            )
        return report
