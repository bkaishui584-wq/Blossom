"""本地知识库门面：加目录、扫目录、存索引、查片段。

只读用户显式加进来的目录；不自动全盘导入，不联网，不上传。
读正文复用 tools.workspace 的 extract_document（MarkItDown 三档降级）。
"""

import os

from knowledge.index import DocumentIndex, split_chunks
from tools import workspace

# 纯文本类后缀：这些直接按文本读，不走文档解析。
TEXT_EXTRA = (".md", ".markdown", ".rst", ".log", ".csv", ".tsv")
# 扫描时要认识的后缀合集。
SUFFIXES = tuple(
    set(workspace.TEXT_SUFFIXES)
    | set(workspace.DOC_SUFFIXES)
    | set(workspace.EXTRA_DOC_SUFFIXES)
    | set(TEXT_EXTRA)
)
# 单份文件超过这个大小不读，避免一份超大日志把内存和索引撑爆。
MAX_FILE_BYTES = 8 * 1024 * 1024
# 单份文件最多留这么多字进索引。
MAX_DOC_CHARS = 200000
# 扫描时要绕开的目录（工具那边已经有一份，这里再补几个）。
SKIP_DIRS = set(workspace.SKIP_DIRS) | {".svn", ".hg", "site-packages", "vendor"}


class KnowledgeError(Exception):
    """知识库操作失败。由调用方转成真实错误，绝不伪造成功。"""


class KnowledgeService:
    """知识库：管目录清单、扫文件、存片段、查片段。"""

    def __init__(self, database=None, config=None, logger=None, authorized=None):
        self.db = database
        self.config = config
        self.logger = logger
        self.authorized = authorized
        self.index = DocumentIndex(database, logger=logger)

    # ---------- 目录授权 ----------
    def _granted_roots(self):
        """权限中心放行的目录。取不到就当没有，不瞎猜。"""
        if not callable(self.authorized):
            return []
        try:
            value = self.authorized()
        except Exception:
            return []
        items = value if isinstance(value, (list, tuple, set)) else [value]
        roots = []
        for item in items:
            text = str(item or "").strip()
            if text:
                roots.append(os.path.realpath(text))
        return roots

    def authorized_roots(self):
        """可扫的目录 = 用户加进知识库的目录 + 权限中心放行的目录。"""
        roots = []
        for row in self.index.sources():
            root = str(row.get("root") or "").strip()
            if root and root not in roots:
                roots.append(root)
        for root in self._granted_roots():
            if root not in roots:
                roots.append(root)
        return roots

    def is_authorized(self, target):
        want = os.path.realpath(str(target or ""))
        if not want:
            return False
        for root in self.authorized_roots():
            if want == root or want.startswith(root + os.sep):
                return True
        return False

    def add_root(self, target, note=""):
        """把一个目录加进知识库。没授权过一律拒。返回归一的绝对路径。"""
        raw = str(target or "").strip()
        if not raw:
            raise KnowledgeError("要给我一个目录。")
        resolved = os.path.realpath(raw)
        if not os.path.isdir(resolved):
            raise KnowledgeError("这个路径不是目录：%s" % raw)
        if not self.is_authorized(resolved):
            raise KnowledgeError(
                "这个目录还没有授权。先在「本地能力与权限」里放行它，再加进知识库。"
            )
        self.index.add_source(resolved, note=note)
        return resolved

    def remove_root(self, target):
        """把一个目录移出知识库，连同它索引过的片段一起删掉。返回统计。"""
        resolved = os.path.realpath(str(target or ""))
        if not resolved:
            raise KnowledgeError("要给我一个目录。")
        docs = self.index.documents_under(resolved)
        self.index.drop_source(resolved)
        return {"root": resolved, "documents": len(docs)}

    def roots(self):
        return self.authorized_roots()

    # ---------- 扫描 ----------
    def scan_root(self, target, on_progress=None):
        """把一个目录里支持的文件读进索引。返回统计，出错也不中断整轮。"""
        resolved = os.path.realpath(str(target or ""))
        if not os.path.isdir(resolved):
            raise KnowledgeError("这个目录不存在：%s" % target)
        if not self.is_authorized(resolved):
            raise KnowledgeError("这个目录还没授权，不能扫。")
        found = indexed = skipped = failed = 0
        errors = []
        for item in self._walk(resolved):
            found += 1
            if callable(on_progress):
                try:
                    on_progress(found, item)
                except Exception:
                    pass
            try:
                ok = self.index_document(item, root=resolved)
            except Exception as exc:
                failed += 1
                errors.append((item, str(exc)))
                continue
            if ok:
                indexed += 1
            else:
                skipped += 1
        self.index.touch_source(resolved, docs=indexed)
        return {
            "root": resolved,
            "found": found,
            "indexed": indexed,
            "skipped": skipped,
            "failed": failed,
            "errors": errors[:20],
        }

    def _walk(self, root):
        """一层层扫，绕开缓存和版本库目录。只产出后缀认识的普通文件。"""
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                entries = sorted(os.scandir(current), key=lambda item: item.name)
            except OSError:
                continue
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name in SKIP_DIRS or entry.name.startswith("."):
                            continue
                        stack.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        if self.supports(entry.name):
                            yield entry.path
                except OSError:
                    continue

    @staticmethod
    def supports(name):
        suffix = os.path.splitext(str(name or ""))[1].lower()
        return suffix in SUFFIXES

    def index_document(self, target, root=""):
        """读一份文件、切段落、写进索引。读不到正文就返回 False。"""
        resolved = os.path.realpath(str(target or ""))
        if not os.path.isfile(resolved):
            return False
        try:
            stat = os.stat(resolved)
        except OSError:
            return False
        if stat.st_size > MAX_FILE_BYTES:
            return False
        suffix = os.path.splitext(resolved)[1].lower()
        text, engine = self._read_text(resolved, suffix)
        text = str(text or "").strip()
        if not text:
            return False
        text = text[:MAX_DOC_CHARS]
        chunks = split_chunks(text)
        if not chunks:
            return False
        self.index.store(
            resolved,
            name=os.path.basename(resolved),
            fmt=suffix.lstrip("."),
            size=stat.st_size,
            mtime=stat.st_mtime,
            engine=engine,
            chunks=chunks,
            chars=len(text),
            root=root,
        )
        return True

    @staticmethod
    def _read_text(resolved, suffix):
        """容器格式走 workspace 的解析；纯文本直接读。返回 (正文, 引擎名)。"""
        if suffix in workspace.DOC_SUFFIXES or suffix in workspace.EXTRA_DOC_SUFFIXES:
            result = workspace.extract_document(resolved, max_chars=MAX_DOC_CHARS)
            return result.get("text") or "", result.get("engine") or ""
        try:
            with open(resolved, "r", encoding="utf-8", errors="replace") as handle:
                return handle.read(MAX_DOC_CHARS), "plain"
        except OSError:
            return "", ""

    # ---------- 查 ----------
    def search(self, query, limit=6):
        """按关键词找片段。返回的文件都带相对路径，方便回原文。"""
        text = str(query or "").strip()
        if not text:
            return []
        rows = self.index.search(text, limit=limit)
        for row in rows:
            row["relative"] = self.relative(row.get("path"), row.get("root"))
        return rows

    @staticmethod
    def relative(value, root):
        try:
            if root:
                return os.path.relpath(str(value), str(root))
        except Exception:
            pass
        return os.path.basename(str(value or ""))

    def documents(self, limit=500):
        return self.index.documents(limit=limit)

    # ---------- 自述 ----------
    def describe(self):
        state = self.index.describe()
        return {
            "ready": state["ready"],
            "tokenizer": state["tokenizer"],
            "documents": state["documents"],
            "chunks": state["chunks"],
            "indexed": state["indexed"],
            "roots": [str(row.get("root") or "") for row in self.index.sources()],
            "reason": state["reason"],
        }

    def self_test(self):
        """真在库上建一次索引，确认打包后还能用。"""
        state = self.index.describe()
        if not state["ready"]:
            return {"ok": False, "detail": state["reason"] or "全文索引不可用"}
        return {
            "ok": True,
            "detail": "索引可用：%s 分词，%d 份文档、%d 个片段"
            % (state["tokenizer"] or "默认", state["documents"], state["indexed"]),
        }
