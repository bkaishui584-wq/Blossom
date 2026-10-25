"""Skill 加载器：按需读取 SKILL.md，参考资料也只在要的时候读。

默认只解析说明文件。脚本和依赖安装必须由用户单独授权，导入时不搬、不跑。
"""

import os
import shutil

from skills.schema import SkillMeta, parse_skill_text
from skills.validator import SkillValidator, classify_reference, inside

SKILL_FILE = "SKILL.md"
REFERENCE_DIR = "reference"
BUILTIN_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "library")


def skill_root_of(path):
    """一个 Skill 目录里应该有一个 SKILL.md。"""
    return os.path.isfile(os.path.join(str(path), SKILL_FILE))


class SkillLoader:
    """roots 是按优先级排的搜索目录，前面的先被找到。"""

    def __init__(self, roots=(), registry=None, validator=None, logger=None, tools=None):
        self.roots = [str(root) for root in roots if root]
        if BUILTIN_ROOT not in self.roots:
            self.roots.append(BUILTIN_ROOT)
        self.registry = registry
        self.tools = tools
        self.validator = validator or SkillValidator(tools=tools, roots=self.roots)
        self.logger = logger
        self._paths = {}
        self._cache = {}
        self._reports = {}

    # ---------- 发现 ----------
    def discover(self):
        """扫描所有 root，返回 skill 名到目录的映射。同名以先找到的为准。"""
        found = {}
        for root in self.roots:
            if not os.path.isdir(root):
                continue
            try:
                entries = sorted(os.listdir(root))
            except OSError as exc:
                self._warn("skill root %s unreadable: %s", root, exc)
                continue
            for entry in entries:
                path = os.path.join(root, entry)
                if os.path.isdir(path) and skill_root_of(path) and entry not in found:
                    found[entry] = path
        self._paths = found
        return dict(found)

    def path_of(self, name):
        if name not in self._paths:
            self.discover()
        return self._paths.get(str(name or ""))

    # ---------- 加载 ----------
    def load(self, name):
        """读一个 Skill 的说明文件。读不到或解析失败都返回 None，不猜。"""
        name = str(name or "")
        if name in self._cache:
            return self._cache[name]
        path = self.path_of(name)
        if not path:
            return None
        try:
            with open(os.path.join(path, SKILL_FILE), "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            self._warn("skill %s unreadable: %s", name, exc)
            return None
        meta, errors = parse_skill_text(text, name_hint=name)
        if errors:
            self._warn("skill %s parse issues: %s", name, "; ".join(errors))
        meta.path = path
        meta.source = "builtin" if inside(BUILTIN_ROOT, path) else "user"
        report = self.validator.validate(meta)
        self._reports[name] = report
        if not report.ok:
            meta.enabled = False
            self._warn(
                "skill %s not loaded: %s",
                name,
                report.blocked or "; ".join(report.errors),
            )
        self._cache[name] = meta
        if self.registry is not None:
            self.registry.register(meta)
        return meta

    def load_all(self):
        loaded = []
        for name in self.discover():
            meta = self.load(name)
            if meta is not None:
                loaded.append(meta)
        return loaded

    def report_of(self, name):
        if name not in self._reports:
            self.load(name)
        return self._reports.get(str(name or ""))

    def invalidate(self, name=None):
        if name is None:
            self._cache.clear()
            self._reports.clear()
        else:
            self._cache.pop(str(name), None)
            self._reports.pop(str(name), None)

    # ---------- 参考资料 ----------
    def references(self, name):
        """只列文件名，不读内容。用到某一份时再 instruction()。"""
        path = self.path_of(name)
        if not path:
            return []
        ref_dir = os.path.join(path, REFERENCE_DIR)
        if not os.path.isdir(ref_dir):
            return []
        try:
            return sorted(
                entry for entry in os.listdir(ref_dir)
                if os.path.isfile(os.path.join(ref_dir, entry))
            )
        except OSError:
            return []

    def instruction(self, name, reference=""):
        """读正文或某一份参考资料。按需加载，不常驻上下文。"""
        path = self.path_of(name)
        if not path:
            return ""
        target = os.path.join(path, SKILL_FILE) if not reference else os.path.join(
            path, REFERENCE_DIR, str(reference)
        )
        if not inside(path, target) or not os.path.isfile(target):
            return ""
        try:
            with open(target, "r", encoding="utf-8") as handle:
                return handle.read()
        except OSError as exc:
            self._warn("skill %s reference unreadable: %s", name, exc)
            return ""

    def entry_path(self, name):
        meta = self._cache.get(str(name or "")) or self.load(name)
        if meta is None or not meta.entry:
            return ""
        path = os.path.join(meta.path, meta.entry)
        return path if inside(meta.path, path) and os.path.isfile(path) else ""

    # ---------- 导入 ----------
    def import_from(self, source_dir, target_root=None, name=""):
        """导入用户自己的 Skill：只搬说明和文本参考，脚本一律跳过等授权。"""
        source_dir = str(source_dir or "")
        if not os.path.isdir(source_dir) or not skill_root_of(source_dir):
            return {"ok": False, "error": "这个目录里没有 SKILL.md"}
        meta, _ = parse_skill_text(
            self.instruction_from(source_dir), name_hint=name or os.path.basename(source_dir)
        )
        if not meta.name or not isValidName(meta.name):
            return {"ok": False, "error": "SKILL.md 里的 name 不合法"}
        root = str(target_root or (self.roots[0] if self.roots else BUILTIN_ROOT))
        target = os.path.join(root, meta.name)
        if os.path.exists(target):
            return {"ok": False, "error": "同名 Skill 已经存在：%s" % meta.name}
        copied, skipped = [], []
        try:
            os.makedirs(target, exist_ok=True)
            shutil.copy2(os.path.join(source_dir, SKILL_FILE), os.path.join(target, SKILL_FILE))
            copied.append(SKILL_FILE)
            ref_dir = os.path.join(source_dir, REFERENCE_DIR)
            if os.path.isdir(ref_dir):
                os.makedirs(os.path.join(target, REFERENCE_DIR), exist_ok=True)
                for entry in sorted(os.listdir(ref_dir)):
                    src = os.path.join(ref_dir, entry)
                    if not os.path.isfile(src):
                        skipped.append(entry + "（不是文件）")
                        continue
                    kind = classify_reference(entry)
                    if kind == "script":
                        skipped.append(entry + "（脚本，需要单独授权）")
                        continue
                    if kind == "unknown":
                        skipped.append(entry + "（类型不支持）")
                        continue
                    shutil.copy2(src, os.path.join(target, REFERENCE_DIR, entry))
                    copied.append(os.path.join(REFERENCE_DIR, entry))
        except OSError as exc:
            return {"ok": False, "error": "导入失败：%s" % exc}
        self.invalidate(meta.name)
        self.discover()
        return {
            "ok": True,
            "name": meta.name,
            "path": target,
            "copied": copied,
            "skipped": skipped,
            "note": "只导入了说明与文本参考；脚本和依赖需要单独审查后授权。",
        }

    def instruction_from(self, source_dir):
        for entry in os.listdir(source_dir):
            if entry.lower() == SKILL_FILE.lower():
                try:
                    with open(os.path.join(source_dir, entry), "r", encoding="utf-8") as handle:
                        return handle.read()
                except OSError:
                    return ""
        return ""

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)


def isValidName(name):
    from skills.schema import NAME_RE

    return bool(NAME_RE.match(str(name or "")))
