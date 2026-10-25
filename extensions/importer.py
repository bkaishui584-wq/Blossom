"""技能导入器：先看清，再决定要不要。

按 V10 的十步走：记来源 → 落到临时目录 → 查目录结构与路径穿越 → 解析 SKILL.md
→ 核对引用的文件是否在范围内 → 扫脚本入口与外部地址 → 核许可证与依赖
→ 说明要什么权限和联网 → 固定版本与来源 → 无副作用地给出结论。

两条死规矩：
1. 导入、更新、启用，任何时候都不执行来源里的脚本。
2. SKILL.md 也当成不可信输入：它说要读什么、连哪里，不代表就有权限。

脚本一律进 scripts 清单隔离，不复制进技能目录，等用户单独授权。
"""

import json
import os
import re
import shutil
import tempfile
import zipfile

from skills.schema import parse_skill_text, scan_untrusted
from skills.validator import SCRIPT_SUFFIXES, classify_reference, inside

SKILL_FILE = "SKILL.md"
REFERENCE_DIR = "reference"

IMPORT_LABELS = {
    "importable": "可导入",
    "review": "待核验",
    "blocked": "拒绝导入",
}

URL_RE = re.compile(r"https?://[^\s\")'>]+", re.I)
CITED_RE = re.compile(r"\`([^\`]+\.[A-Za-z0-9]{1,8})\`")
# 这几样本机不允许「照做」的写法，命中就只记下来交给人看，不自动执行，也不当指令。
RISKY_SNIPPETS = (
    ("powershell -executionpolicy bypass", "要求用绕过执行策略的方式跑 PowerShell"),
    ("invoke-expression", "要求动态执行字符串代码"),
    ("curl ", "要求联网抓取"),
    ("wget ", "要求联网抓取"),
    ("npm install", "要求装 npm 依赖"),
    ("pip install", "要求装 Python 依赖"),
    ("rm -rf", "要求递归删除"),
    ("del /f", "要求强制删除"),
    ("reg add", "要求改注册表"),
    ("schtasks", "要求建计划任务"),
)


class ImportReport:
    """一次导入检查的结论。ok 才对得上「能安全落地」。"""

    def __init__(self, source="", ident="", name=""):
        self.source = str(source or "")
        self.id = str(ident or "")
        self.name = str(name or "")
        self.version = ""
        self.description = ""
        self.verdict = "review"
        self.stage_dir = ""
        self.files = []
        self.text_files = []
        self.scripts = []
        self.unknown_files = []
        self.suspicious = []
        self.unlisted_refs = []
        self.external_urls = []
        self.tools = []
        self.permission = "READ"
        self.requires_network = False
        self.license = ""
        self.license_status = "review-needed"
        self.dependencies = []
        self.notes = []
        self.blocked_reason = ""

    @property
    def ok(self):
        return self.verdict == "importable" and not self.blocked_reason

    def verdict_label(self):
        return IMPORT_LABELS.get(self.verdict, self.verdict)

    def to_dict(self):
        return {
            "source": self.source,
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "verdict": self.verdict,
            "verdict_label": self.verdict_label(),
            "ok": self.ok,
            "blocked_reason": self.blocked_reason,
            "stage_dir": self.stage_dir,
            "files": list(self.files),
            "text_files": list(self.text_files),
            "scripts": list(self.scripts),
            "unknown_files": list(self.unknown_files),
            "suspicious": [list(item) for item in self.suspicious],
            "unlisted_refs": list(self.unlisted_refs),
            "external_urls": list(self.external_urls),
            "tools": list(self.tools),
            "permission": self.permission,
            "requires_network": bool(self.requires_network),
            "license": self.license,
            "license_status": self.license_status,
            "dependencies": list(self.dependencies),
            "notes": list(self.notes),
        }


def _tree_dirs(paths):
    """列出这批文件涉及的所有目录前缀。用来判断某个引用是不是随包该带的。"""
    dirs = set()
    for rel in paths or ():
        parts = str(rel).replace("\\", "/").split("/")[:-1]
        for index in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:index]))
    return dirs


def _walk_files(root):
    """列出目录里的每个文件，跳过 __pycache__ 这类噪音。"""
    found = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [item for item in dirs if item not in ("__pycache__", ".git")]
        for entry in sorted(files):
            found.append(os.path.join(base, entry))
    return found


def _read_text(path, limit=400000):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return ""


# 随包元数据里写了这几样，才算「许可证是清楚的」。其余一律待审。
PERMISSIVE = ("mit", "apache-2.0", "bsd-2-clause", "bsd-3-clause", "isc", "mpl-2.0", "cc0-1.0")


def _license_of(root):
    """从随包的元数据里找许可证。找不到就说没声明，不猜。"""
    for name in ("_meta.json", "meta.json", "info.json", "manifest.json"):
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            continue
        if isinstance(data, dict):
            value = data.get("license") or data.get("licence")
            if isinstance(value, str) and value.strip():
                text = value.strip()
                status = "approved" if text.lower() in PERMISSIVE else "review-needed"
                return text, status, [str(name)]
    return "", "review-needed", []


def safe_extract(zip_path, dest):
    """解压到指定目录。越界、绝对路径、符号链接一律拒绝，返回问题清单。"""
    problems = []
    os.makedirs(dest, exist_ok=True)
    try:
        archive = zipfile.ZipFile(zip_path)
    except Exception as exc:
        return ["压缩包打不开：%s" % exc]
    with archive:
        for info in archive.infolist():
            name = info.filename
            target = os.path.join(dest, name)
            if name.startswith("/") or name.startswith("\\") or ":" in name.split("/")[0]:
                problems.append("压缩包里用了绝对路径：%s" % name)
                continue
            if ".." in name.replace("\\", "/").split("/"):
                problems.append("压缩包里有路径穿越：%s" % name)
                continue
            if not inside(dest, target):
                problems.append("解压后会跑出目录：%s" % name)
                continue
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                problems.append("压缩包里有符号链接：%s" % name)
                continue
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target) or dest, exist_ok=True)
            with archive.open(info) as source, open(target, "wb") as handle:
                shutil.copyfileobj(source, handle)
    return problems


def inspect_directory(root, source="", ident="", name=""):
    """看一个技能目录。返回 ImportReport，不改动任何东西。"""
    report = ImportReport(source=source, ident=ident, name=name)
    root = str(root or "")
    if not os.path.isdir(root):
        report.verdict = "blocked"
        report.blocked_reason = "这个目录不存在"
        return report
    report.stage_dir = root

    problems = []
    for path in _walk_files(root):
        rel = os.path.relpath(path, root).replace("\\", "/")
        if os.path.islink(path):
            problems.append("有符号链接：%s" % rel)
            continue
        if not inside(root, path):
            problems.append("文件跑出了技能目录：%s" % rel)
            continue
        report.files.append(rel)

    skill_path = os.path.join(root, SKILL_FILE)
    text = _read_text(skill_path)
    if not text:
        report.verdict = "blocked"
        report.blocked_reason = "没有找到可读的 SKILL.md"
        return report

    meta, errors = parse_skill_text(text, name_hint=name or os.path.basename(os.path.normpath(root)))
    report.name = meta.name or report.name
    report.version = meta.version
    report.description = meta.description
    report.tools = list(meta.tools)
    report.permission = meta.permission
    if errors:
        report.notes.extend("说明文件的问题：%s" % item for item in errors)

    hits = tuple(meta.untrusted or scan_untrusted(text))
    if hits:
        problems.append("说明里出现要求绕过权限或泄露数据的字样：%s" % "、".join(sorted(set(hits))))

    lowered = text.lower()
    for token, why in RISKY_SNIPPETS:
        if token in lowered:
            report.suspicious.append([token.strip(), why])
    report.external_urls = sorted(set(URL_RE.findall(text)))
    if report.external_urls:
        report.requires_network = True
    # 说明里让装依赖或 clone 仓库的，也算要联网：这是推断，会在备注里写明。
    for token, _why in RISKY_SNIPPETS:
        if token in ("pip install", "npm install", "curl ", "wget "):
            if token in lowered:
                report.requires_network = True
    if "git clone" in lowered or "gitcode.com/" in lowered or "github.com/" in lowered:
        report.requires_network = True

    for rel in report.files:
        kind = classify_reference(rel)
        suffix = os.path.splitext(rel)[1].lower()
        if suffix in SCRIPT_SUFFIXES:
            report.scripts.append(rel)
            kind = "script"
        if kind == "script":
            if rel not in report.scripts:
                report.scripts.append(rel)
        elif kind == "text":
            report.text_files.append(rel)
        else:
            report.unknown_files.append(rel)

    # 说明里点名的文件得真在目录里。只查「随包确实有这一类目录」的引用：
    # 技能运行时自己建的路径（比如 .learnings/）不算缺文件。
    tree = _tree_dirs(report.files)
    for cited in sorted(set(CITED_RE.findall(text))):
        clean = cited.replace("\\", "/").strip()
        parent = clean.rsplit("/", 1)[0] if "/" in clean else ""
        if not parent or parent not in tree:
            continue
        cited_path = os.path.join(root, clean)
        if os.path.exists(cited_path):
            continue
        if clean not in report.unlisted_refs:
            report.unlisted_refs.append(clean)

    report.license, report.license_status, notes = _license_of(root)
    if notes:
        report.notes.extend("许可证来自 %s" % item for item in notes)
    for name_only in ("requirements.txt", "package.json"):
        if os.path.isfile(os.path.join(root, name_only)):
            report.dependencies.append(name_only)

    if report.scripts:
        report.notes.append("含脚本 %d 个，一律隔离，不进技能目录，等单独授权" % len(report.scripts))
    if report.requires_network:
        if report.external_urls:
            report.notes.append("说明里带了外部地址 %d 条，用之前先确认联网范围" % len(report.external_urls))
        else:
            report.notes.append("说明里要求联网取依赖或拉仓库，用之前先确认联网范围")

    # 结论只看「内容能不能安全落地」；许可证是另一条线，单独展示，不混进这里。
    if problems:
        report.verdict = "blocked"
        report.blocked_reason = "；".join(problems)
    elif report.scripts or report.suspicious or report.requires_network or report.unlisted_refs:
        report.verdict = "review"
    else:
        report.verdict = "importable"
    if report.license_status != "approved":
        report.notes.append("许可证没有随包声明或不是常见宽松许可，导入前要人工确认")
    return report


class SkillImporter:
    """导入器本体。默认把东西摊在临时目录里看，用户点头之后才落到技能目录。"""

    def __init__(self, client=None, logger=None, temp_root=None):
        self.client = client
        self.logger = logger
        self.temp_root = temp_root

    def _temp_dir(self, prefix="blossom-import-"):
        return tempfile.mkdtemp(prefix=prefix, dir=self.temp_root)

    def inspect_directory(self, path):
        return inspect_directory(path, source=str(path or ""))

    def inspect_zip(self, path):
        path = str(path or "")
        report = ImportReport(source=path, name=os.path.splitext(os.path.basename(path))[0])
        if not os.path.isfile(path):
            report.verdict = "blocked"
            report.blocked_reason = "没有这个压缩包"
            return report
        dest = self._temp_dir()
        problems = safe_extract(path, dest)
        if problems:
            report.verdict = "blocked"
            report.blocked_reason = "；".join(problems)
            report.stage_dir = dest
            return report
        inner = _single_skill_root(dest)
        inner_report = inspect_directory(inner, source=path, name=report.name)
        inner_report.stage_dir = inner
        return inner_report

    def inspect_qoder(self, url_or_id, client=None, license_hint="", license_status_hint=""):
        """从 Qoder 市场取一个技能的公开内容来看。只取说明，不下载可执行包。"""
        from extensions import qoder as qoder_module

        client = client or self.client or qoder_module
        ident = qoder_module.skill_id_from_url(url_or_id) or str(url_or_id or "").strip()
        report = ImportReport(source=str(url_or_id or ""), ident=ident)
        if not ident:
            report.verdict = "blocked"
            report.blocked_reason = "从链接里读不出技能 id"
            return report
        try:
            payload = client.detail(ident)
        except Exception as exc:
            report.verdict = "review"
            report.blocked_reason = "详情取不到：%s" % exc
            report.notes.append("先记为待核验，等能取到内容再导入")
            return report
        info = qoder_module.normalize_detail(payload)
        report.name = info.get("name") or ident
        report.version = info.get("version") or ""
        report.description = info.get("description") or ""
        report.license = ""
        report.license_status = "review-needed"
        if info.get("github_repo"):
            report.notes.append("上游仓库：%s（许可证要去仓库里核）" % info["github_repo"])
        else:
            report.notes.append("接口不提供许可证字段，导入前要人工确认授权")

        try:
            text = qoder_module.file_text(client.skill_file(ident, "/SKILL.md"))
        except Exception as exc:
            report.verdict = "review"
            report.blocked_reason = "说明正文取不到：%s" % exc
            return report
        if not text:
            report.verdict = "review"
            report.blocked_reason = "接口没给出 SKILL.md 正文"
            return report

        dest = self._temp_dir()
        os.makedirs(os.path.join(dest, REFERENCE_DIR), exist_ok=True)
        with open(os.path.join(dest, SKILL_FILE), "w", encoding="utf-8") as handle:
            handle.write(text)
        for label in qoder_module.tree_paths(info.get("file_tree")):
            if not label or label == SKILL_FILE:
                continue
            report.files.append(str(label))
        staged = inspect_directory(dest, source=str(url_or_id or ""), ident=ident, name=report.name)
        staged.version = report.version
        staged.description = report.description or staged.description
        staged.notes = report.notes + staged.notes
        for entry in report.files:
            if entry not in staged.files:
                staged.files.append(entry)
                suffix = os.path.splitext(entry)[1].lower()
                if suffix in SCRIPT_SUFFIXES:
                    if entry not in staged.scripts:
                        staged.scripts.append(entry)
        if staged.scripts and staged.verdict == "importable":
            staged.verdict = "review"
        # 上游许可证如果已经核过，就用核过的结论；没核过就照旧标待审阅。
        if license_hint:
            staged.license = str(license_hint)
            staged.license_status = str(license_status_hint or "review-needed")
            staged.notes.append("许可证来自清单里已核实的记录：%s" % license_hint)
        return staged

    def discard(self, report):
        """用户不导入时把临时目录收掉，别在 %TEMP% 里越堆越多。"""
        if report is None:
            return False
        stage = str(getattr(report, "stage_dir", "") or "")
        if not stage or not os.path.isdir(stage):
            return False
        root = os.path.realpath(self.temp_root or tempfile.gettempdir())
        target = os.path.realpath(stage)
        # 只删自己建的临时目录，删不到就别删。
        if not inside(root, target) or target == root:
            return False
        if not os.path.basename(target).startswith("blossom-import-"):
            return False
        try:
            shutil.rmtree(target, ignore_errors=True)
            return True
        except OSError:
            return False

    def apply(self, report, target_root, override_name=""):
        """把检查通过的技能落进技能目录。只搬说明和文本参考，脚本一律不搬。"""
        if report is None or report.verdict == "blocked":
            return {"ok": False, "error": (report.blocked_reason if report else "没有可导入的内容")}
        name = str(override_name or report.name or "").strip()
        if not name:
            return {"ok": False, "error": "这个技能没有可用的名字"}
        root = str(target_root or "")
        if not root:
            return {"ok": False, "error": "没有指定技能目录"}
        target = os.path.join(root, name)
        if os.path.exists(target):
            return {"ok": False, "error": "同名技能已经存在：%s" % name}
        stage = report.stage_dir
        skill_path = os.path.join(stage, SKILL_FILE)
        if not os.path.isfile(skill_path):
            return {"ok": False, "error": "临时目录里没有 SKILL.md"}
        copied, skipped = [], []
        try:
            os.makedirs(target, exist_ok=True)
            shutil.copy2(skill_path, os.path.join(target, SKILL_FILE))
            copied.append(SKILL_FILE)
            for rel in report.text_files:
                if rel == SKILL_FILE:
                    continue
                source = os.path.join(stage, rel)
                if not os.path.isfile(source) or not inside(stage, source):
                    continue
                kind = classify_reference(rel)
                if kind != "text":
                    skipped.append(rel + "（不是文本）")
                    continue
                dest = os.path.join(target, rel)
                os.makedirs(os.path.dirname(dest) or target, exist_ok=True)
                shutil.copy2(source, dest)
                copied.append(rel)
        except OSError as exc:
            return {"ok": False, "error": "复制失败：%s" % exc}
        for rel in report.scripts:
            skipped.append(rel + "（脚本，需要单独授权）")
        for rel in report.unknown_files:
            skipped.append(rel + "（类型不支持）")
        self.discard(report)
        return {
            "ok": True,
            "name": name,
            "path": target,
            "copied": copied,
            "skipped": skipped,
            "verdict": report.verdict,
            "note": "只搬了说明和文本参考；脚本和依赖要单独审查后授权。",
        }


def _single_skill_root(dest):
    """压缩包常常多一层同名文件夹，往下走一层找 SKILL.md。"""
    if os.path.isfile(os.path.join(dest, SKILL_FILE)):
        return dest
    try:
        entries = sorted(os.listdir(dest))
    except OSError:
        return dest
    for entry in entries:
        candidate = os.path.join(dest, entry)
        if os.path.isdir(candidate) and os.path.isfile(os.path.join(candidate, SKILL_FILE)):
            return candidate
    return dest
