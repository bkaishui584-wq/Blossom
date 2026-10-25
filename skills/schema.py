"""Skill 元数据与 SKILL.md 解析。

Skill 只有说明和元数据，没有任何执行能力。解析失败一律如实报错，不猜。
"""

import re
from dataclasses import dataclass, field

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,40}$")

# 这些字样说明文件想绕过程序限制或窃取数据。命中的 Skill 一律标记为不可信。
UNTRUSTED_PATTERNS = (
    "忽略权限", "绕过权限", "跳过确认", "跳过权限", "不用确认", "无需确认",
    "ignore previous", "ignore all previous", "bypass permission",
    "bypass the permission", "skip confirmation", "disable permission",
    "disable safety", "print api key", "print the api key", "泄露密钥",
    "导出密钥", "上传密钥", "steal credential", "rm -rf /", "format c:",
    "disable the permission", "no confirmation needed",
)

_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)

_SECTION_KEYS = {
    "触发条件": "triggers",
    "触发": "triggers",
    "输入": "inputs",
    "步骤": "steps",
    "允许工具": "tools",
    "工具": "tools",
    "权限": "permission",
    "输出": "output",
    "输出格式": "output",
    "验证": "verify",
    "验收标准": "verify",
    "失败处理": "failure",
}


def _clean(value):
    return str(value or "").strip()


def _split_list(value):
    """把 "a, b, c" 或 "[a, b]" 拆成元组。空值返回空元组。"""
    text = _clean(value)
    if not text:
        return ()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    parts = [item.strip().strip('"').strip("'") for item in text.split(",")]
    return tuple(item for item in parts if item)


def _split_front_matter(text):
    """返回 (front_matter, body)。没有 front matter 时 front 为空串。"""
    text = str(text or "").lstrip("\ufeff")
    if not text.startswith("---"):
        return "", text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return "", text
    for index in range(1, len(lines)):
        if lines[index].strip() in ("---", "..."):
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1 :])
    return "", text


def _unquote(value):
    """front matter 里的值常带一层引号，脱掉它，免得说明里露出多余的引号。"""
    text = str(value or "").strip()
    for quote in ('"', "'"):
        if len(text) >= 2 and text.startswith(quote) and text.endswith(quote):
            return text[1:-1].strip()
    return text


def _parse_front(front):
    fields = {}
    pending_key = ""
    for raw in str(front or "").splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.lstrip().startswith("- ") and pending_key:
            extra = fields.get(pending_key, "")
            fields[pending_key] = (extra + "," + line.lstrip()[2:].strip()).strip(",")
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = _unquote(value.strip())
        fields[key] = value
        pending_key = key if not value else ""
    return fields


def _split_sections(body):
    sections = {}
    matches = list(_SECTION_RE.finditer(str(body or "")))
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        title = match.group(1).strip()
        sections[title] = body[start:end].strip()
    return sections


def _bullet_lines(text):
    items = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"^\d+[.、)]\s*", "", line)
        line = re.sub(r"^[-*+]\s*", "", line)
        line = line.strip()
        if line:
            items.append(line)
    return items


def _step_from_line(line):
    """一行步骤： "读项目结构 | tool=list_dir"。"""
    parts = [part.strip() for part in str(line).split("|")]
    title = parts[0].strip()
    if not title:
        return None
    step = SkillStep(title=title)
    for extra in parts[1:]:
        if "=" not in extra:
            continue
        key, _, value = extra.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if key == "tool":
            step.tool = value
        elif key == "permission":
            step.permission = value.upper()
        elif key == "impl":
            step.impl = value
        elif key == "note":
            step.note = value
    return step


@dataclass
class SkillStep:
    """计划里的一个步骤。tool 非空表示这一步靠工具完成。"""

    title: str
    tool: str = ""
    permission: str = ""
    note: str = ""
    impl: str = ""

    def to_dict(self):
        return {
            "title": self.title,
            "tool": self.tool,
            "permission": self.permission,
            "note": self.note,
            "impl": self.impl,
        }


@dataclass
class SkillMeta:
    name: str
    version: str = "0.1.0"
    description: str = ""
    triggers: tuple = ()
    inputs: tuple = ()
    steps: tuple = ()
    tools: tuple = ()
    permission: str = "READ"
    output: str = ""
    verify: tuple = ()
    failure: str = ""
    entry: str = ""
    timeout: float = 0.0
    source: str = "builtin"
    path: str = ""
    enabled: bool = True
    untrusted: tuple = ()
    raw: str = ""

    def matches(self, text):
        lowered = str(text or "").lower()
        return any(str(word).lower() in lowered for word in self.triggers)

    def step_list(self):
        return list(self.steps)

    def to_dict(self):
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "triggers": list(self.triggers),
            "inputs": list(self.inputs),
            "steps": [step.to_dict() for step in self.steps],
            "tools": list(self.tools),
            "permission": self.permission,
            "output": self.output,
            "verify": list(self.verify),
            "failure": self.failure,
            "entry": self.entry,
            "timeout": float(self.timeout or 0),
            "source": self.source,
            "path": self.path,
            "enabled": bool(self.enabled),
            "untrusted": list(self.untrusted),
        }

    def to_row(self):
        """给界面和开发者面板看的紧凑视图。"""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "permission": self.permission,
            "tools": len(self.tools),
            "steps": len(self.steps),
            "enabled": bool(self.enabled),
            "source": self.source,
            "untrusted": bool(self.untrusted),
        }


def scan_untrusted(text):
    """找出文件里要求绕过限制或泄露数据的字样。命中就返回命中的词。"""
    lowered = str(text or "").lower()
    hits = []
    for token in UNTRUSTED_PATTERNS:
        if token.lower() in lowered:
            hits.append(token)
    return tuple(hits)


def parse_skill_text(text, name_hint=""):
    """把 SKILL.md 文本解析成 SkillMeta。返回 (meta, errors)。"""
    errors = []
    front, body = _split_front_matter(text)
    fields = _parse_front(front)
    sections = _split_sections(body)

    def field_or_section(key, section_title):
        value = fields.get(key)
        if value:
            return value
        return sections.get(section_title, "")

    name = (fields.get("name") or name_hint or "").strip()
    meta = SkillMeta(
        name=name,
        version=(fields.get("version") or "0.1.0").strip(),
        description=(fields.get("description") or "").strip(),
        permission=(fields.get("permission") or "READ").strip().upper(),
        output=field_or_section("output", "输出格式") or field_or_section("output", "输出"),
        failure=field_or_section("failure", "失败处理"),
        entry=(fields.get("entry") or "").strip(),
        source=(fields.get("source") or "builtin").strip(),
        path=(fields.get("path") or "").strip(),
        raw=str(text or ""),
    )
    if not meta.description:
        intro = (sections.get("说明", "") or "").strip()
        if intro:
            meta.description = intro.splitlines()[0].strip()

    triggers = _split_list(fields.get("triggers"))
    if not triggers:
        triggers = tuple(_bullet_lines(sections.get("触发条件", "")))
    meta.triggers = triggers

    inputs = _split_list(fields.get("inputs"))
    if not inputs:
        inputs = tuple(_bullet_lines(sections.get("输入", "")))
    meta.inputs = inputs

    tools = _split_list(fields.get("tools"))
    if not tools:
        tools = tuple(
            item.split("=")[0].strip()
            for item in _bullet_lines(sections.get("允许工具", "") or sections.get("工具", ""))
        )
    meta.tools = tuple(tool for tool in tools if tool)

    steps = []
    raw_steps = _bullet_lines(sections.get("步骤", ""))
    for line in raw_steps:
        step = _step_from_line(line)
        if step is not None:
            steps.append(step)
    if not steps and fields.get("steps"):
        for line in _split_list(fields.get("steps")):
            step = _step_from_line(line)
            if step is not None:
                steps.append(step)
    meta.steps = tuple(steps)

    verify = _split_list(fields.get("verify"))
    if not verify:
        verify = tuple(_bullet_lines(sections.get("验收标准", "") or sections.get("验证", "")))
    meta.verify = verify

    if fields.get("timeout"):
        try:
            meta.timeout = float(fields["timeout"])
        except ValueError:
            errors.append("timeout 不是数字")

    meta.untrusted = scan_untrusted(text)
    if not meta.name:
        errors.append("缺少 name")
    if not meta.description:
        errors.append("缺少 description")
    return meta, errors
