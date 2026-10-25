"""本地知识库的索引：把正文切成段落，用 SQLite FTS5 做全文检索。

索引是派生数据：删掉随时能重建，正文永远留在用户自己的文件里。
这一层只管三件事：切段落、建索引、按关键词找片段。

分词选 trigram：中文没有词边界，unicode61 会把一整句当一个词，等于搜不到。
SQLite 太老没有 trigram 就退回 unicode61，再不行整层停用，检索走 LIKE 兜底。
"""

import os
import re

TABLE = "doc_chunks_fts"
# 一片大概多少字。太碎丢上下文，太长命中后定位不到原句。
CHUNK_CHARS = 900
# 超长段落硬切时，前后两片搭多少字，避免断在句中。
LONG_PARA_OVERLAP = 120
# SQLite 自带的 FTS5 用哪个都能试；trigram 优先。
TOKENIZERS = ("trigram", "unicode61")
# trigram 至少三字起才命中；更短的词交给 LIKE，别指望索引。
MIN_TERM = 3
# 一条查询最多拼几个词，别把超长句灌进 MATCH。
MAX_TERMS = 8
_STAMP = (17, "knowledge_fts")
_TOKENIZE_RE = re.compile(r"tokenize\s*=\s*'([^']+)'", re.I)
# 把查询切成词：字母、数字、下划线、汉字各算一个词，其余当分隔。
_TERM_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")
_PARAGRAPH_RE = re.compile(r"\n\s*\n")
# 汉字单字也算一个词。
_CJK_RE = re.compile(r"^[\u4e00-\u9fff]+$")


def _norm(path):
    return os.path.realpath(str(path or ""))


def query_terms(text):
    """把一句查询切成词：长的排前面，去重，最多 MAX_TERMS 个。"""
    words = []
    for match in _TERM_RE.finditer(str(text or "")):
        word = match.group(0).lower()
        # 汉字单字也算一个词（LIKE 兜底用得上）；英文单字母太吵，丢掉。
        if word in words or (len(word) < 2 and not _CJK_RE.match(word)):
            continue
        words.append(word)
    words.sort(key=len, reverse=True)
    return words[:MAX_TERMS]


def match_query(terms):
    """把关键词拼成一条 FTS 查询。拼不出来给空串。"""
    usable = []
    for item in terms or ():
        term = str(item or "").strip().lower()
        if len(term) < MIN_TERM:
            continue
        usable.append(term)
        if len(usable) >= MAX_TERMS:
            break
    if not usable:
        return ""
    return " OR ".join('"%s"' % term.replace('"', '""') for term in usable)


def _locator(start, end, part=0):
    """位置说明：回原文能按第几段找。"""
    if start == end:
        label = "第 %d 段" % int(start)
    else:
        label = "第 %d-%d 段" % (int(start), int(end))
    if part:
        label += "（第 %d 节）" % int(part)
    return label


def _split_long(paragraph, size):
    """一段本身超过一片就带重叠硬切。"""
    step = max(1, int(size) - LONG_PARA_OVERLAP)
    pieces = []
    at = 0
    total = len(paragraph)
    while at < total:
        pieces.append(paragraph[at:at + size])
        if at + size >= total:
            break
        at += step
    return pieces


def split_chunks(text, size=CHUNK_CHARS):
    """把正文切成 (位置说明, 片段) 的列表。

    先按空行分段，再把小段并到一片里；一段本身超过一片就带重叠硬切。
    位置说明用段号，方便回原文定位。
    """
    limit = max(int(size or CHUNK_CHARS), LONG_PARA_OVERLAP * 2 + 1)
    paragraphs = []
    for number, raw in enumerate(_PARAGRAPH_RE.split(str(text or "")), start=1):
        part = raw.strip()
        if part:
            paragraphs.append((number, part))
    chunks = []
    buf = []
    length = 0
    for number, part in paragraphs:
        if len(part) > limit:
            if buf:
                chunks.append(_merge(buf))
                buf, length = [], 0
            pieces = _split_long(part, limit)
            for part_no, piece in enumerate(pieces, start=1):
                mark = part_no if len(pieces) > 1 else 0
                chunks.append((_locator(number, number, mark), piece))
            continue
        if buf and length + len(part) > limit:
            chunks.append(_merge(buf))
            buf, length = [], 0
        buf.append((number, part))
        length += len(part) + 2
    if buf:
        chunks.append(_merge(buf))
    return chunks


def _merge(buf):
    start = buf[0][0]
    end = buf[-1][0]
    body = "\n\n".join(part for _, part in buf)
    return (_locator(start, end), body)


def snippet(text, terms, width=160):
    """在片段里找第一个命中的词，前后各截一段当摘录。"""
    body = str(text or "").strip()
    if len(body) <= width:
        return body
    low = body.lower()
    at = -1
    for item in terms or ():
        found = low.find(str(item).lower())
        if found >= 0 and (at < 0 or found < at):
            at = found
    if at < 0:
        return body[:width].rstrip() + "…"
    start = max(0, at - width // 3)
    end = min(len(body), start + width)
    head = "…" if start > 0 else ""
    tail = "…" if end < len(body) else ""
    return head + body[start:end].strip() + tail


def _escape_like(value):
    return str(value or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _statements(tokenizer):
    """建表与三个触发器。触发器保证索引跟着 doc_chunks 一起增删改。"""
    return (
        "CREATE VIRTUAL TABLE IF NOT EXISTS %s USING fts5("
        "text, content='doc_chunks', content_rowid='id', tokenize='%s')"
        % (TABLE, tokenizer),
        "CREATE TRIGGER IF NOT EXISTS %s_ai AFTER INSERT ON doc_chunks BEGIN"
        " INSERT INTO %s(rowid, text) VALUES (new.id, new.text); END" % (TABLE, TABLE),
        "CREATE TRIGGER IF NOT EXISTS %s_ad AFTER DELETE ON doc_chunks BEGIN"
        " INSERT INTO %s(%s, rowid, text)"
        " VALUES ('delete', old.id, old.text); END" % (TABLE, TABLE, TABLE),
        "CREATE TRIGGER IF NOT EXISTS %s_au AFTER UPDATE ON doc_chunks BEGIN"
        " INSERT INTO %s(%s, rowid, text)"
        " VALUES ('delete', old.id, old.text);"
        " INSERT INTO %s(rowid, text) VALUES (new.id, new.text); END"
        % (TABLE, TABLE, TABLE, TABLE),
    )


def _rebuild_sql():
    return "INSERT INTO %s(%s) VALUES('rebuild')" % (TABLE, TABLE)


class DocumentIndex:
    """文档片段索引。哪个方法都不抛异常，读不到就给空或 False。"""

    def __init__(self, database, logger=None):
        self.db = database
        self.logger = logger
        self.tokenizer = ""
        self.error = ""
        self._ready = None

    # ---------- 建索引 ----------
    def ensure(self, force=False):
        """确保索引在。返回能不能用。建不出来不算错，只是退回 LIKE。"""
        if self._ready is not None and not force:
            return self._ready
        if self.db is None:
            self._ready = False
            self.error = "没有数据库连接"
            return False
        existing = self._existing_sql()
        if existing and not force:
            self.tokenizer = self._tokenizer_of(existing)
            self._ready = True
            return True
        for tokenizer in TOKENIZERS:
            if not self._supported(tokenizer):
                continue
            if self._create(tokenizer):
                self._ready = True
                return True
        self._ready = False
        if not self.error:
            self.error = "当前 SQLite 不支持 FTS5"
        self._warn("knowledge fts unavailable: %s", self.error)
        return False

    def _supported(self, tokenizer):
        """真在库里建一张临时虚表试试，不收版本号当证据。"""
        try:
            self.db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS temp.knowledge_probe USING fts5("
                "body, tokenize='%s')" % tokenizer
            )
            self.db.execute("DROP TABLE IF EXISTS temp.knowledge_probe")
            return True
        except Exception as exc:
            self.error = str(exc)
            return False

    def _create(self, tokenizer):
        try:
            for statement in _statements(tokenizer):
                self.db.execute(statement)
        except Exception as exc:
            self.error = str(exc)
            return False
        self.tokenizer = tokenizer
        self._stamp()
        self.rebuild()
        return True

    def _stamp(self):
        """在迁移表里留一条记录，方便看这个库建过索引没有。不算迁移失败。"""
        try:
            self.db.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (?, ?)",
                _STAMP,
            )
        except Exception as exc:
            self._warn("knowledge fts stamp failed: %s", exc)

    def rebuild(self):
        """按 doc_chunks 表重灌索引，返回灌进去的条数。"""
        try:
            self.db.execute(_rebuild_sql())
        except Exception as exc:
            self.error = str(exc)
            self._warn("knowledge fts rebuild failed: %s", exc)
            return 0
        return self.count_indexed()

    # ---------- 文档 ----------
    def document(self, value):
        try:
            return self.db.query_one("SELECT * FROM documents WHERE path = ?", (_norm(value),))
        except Exception as exc:
            self.error = str(exc)
            return None

    def documents(self, limit=500):
        try:
            return self.db.query(
                "SELECT * FROM documents ORDER BY updated_at DESC, id DESC LIMIT ?",
                (max(1, int(limit or 500)),),
            )
        except Exception as exc:
            self.error = str(exc)
            return []

    def documents_under(self, root):
        try:
            return self.db.query(
                "SELECT * FROM documents WHERE root = ? ORDER BY path", (_norm(root),)
            )
        except Exception as exc:
            self.error = str(exc)
            return []

    def store(self, value, name="", fmt="", size=0, mtime=0.0, engine="", chunks=(), chars=0, root=""):
        """把一份文档的片段写进索引。同路径先清旧的，再写新的。返回文档编号。"""
        key = _norm(value)
        if not key:
            return 0
        self.remove_path(key)
        try:
            doc_id = self.db.insert(
                "INSERT INTO documents"
                " (path, name, format, size, mtime, engine, chunks, chars, root)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key,
                    str(name or ""),
                    str(fmt or ""),
                    int(size or 0),
                    float(mtime or 0.0),
                    str(engine or ""),
                    len(tuple(chunks or ())),
                    int(chars or 0),
                    _norm(root) if root else "",
                ),
            )
        except Exception as exc:
            self.error = str(exc)
            self._warn("knowledge store document failed: %s", exc)
            return 0
        for order, item in enumerate(chunks or ()):
            locator, body = item
            try:
                self.db.insert(
                    "INSERT INTO doc_chunks (doc_id, ord, locator, text) VALUES (?, ?, ?, ?)",
                    (doc_id, order, str(locator or ""), str(body or "")),
                )
            except Exception as exc:
                self.error = str(exc)
                self._warn("knowledge store chunk failed: %s", exc)
        return int(doc_id or 0)

    def remove_path(self, value):
        row = self.document(value)
        if not row:
            return 0
        return self.remove_id(row["id"])

    def remove_id(self, doc_id):
        try:
            self.db.execute("DELETE FROM doc_chunks WHERE doc_id = ?", (int(doc_id),))
            removed = self.db.execute("DELETE FROM documents WHERE id = ?", (int(doc_id),))
        except Exception as exc:
            self.error = str(exc)
            return 0
        return int(removed or 0)

    # ---------- 目录 ----------
    def sources(self):
        try:
            return self.db.query("SELECT * FROM doc_sources ORDER BY added_at, root")
        except Exception as exc:
            self.error = str(exc)
            return []

    def add_source(self, root, note=""):
        key = _norm(root)
        if not key:
            return False
        try:
            self.db.execute(
                "INSERT OR IGNORE INTO doc_sources (root, note) VALUES (?, ?)",
                (key, str(note or "")),
            )
            return True
        except Exception as exc:
            self.error = str(exc)
            return False

    def touch_source(self, root, docs=0):
        try:
            self.db.execute(
                "UPDATE doc_sources SET docs = ?, last_scan = datetime('now') WHERE root = ?",
                (int(docs or 0), _norm(root)),
            )
            return True
        except Exception as exc:
            self.error = str(exc)
            return False

    def drop_source(self, root):
        key = _norm(root)
        removed = 0
        for row in self.documents_under(key):
            removed += self.remove_id(row["id"])
        try:
            self.db.execute("DELETE FROM doc_sources WHERE root = ?", (key,))
        except Exception as exc:
            self.error = str(exc)
        return removed

    # ---------- 计数 ----------
    def _one(self, sql, params=()):
        try:
            row = self.db.query_one(sql, params)
        except Exception:
            return 0
        return int((row or {}).get("n") or 0)

    def count_documents(self):
        return self._one("SELECT COUNT(*) AS n FROM documents")

    def count_chunks(self):
        return self._one("SELECT COUNT(*) AS n FROM doc_chunks")

    def count_indexed(self):
        return self._one("SELECT COUNT(*) AS n FROM %s" % TABLE)

    # ---------- 查 ----------
    def search(self, query, limit=6):
        """按关键词找片段。返回带文件名、位置和摘录的列表。"""
        terms = query_terms(query)
        if not terms:
            return []
        count = max(1, int(limit or 6))
        rows = []
        if self.ensure():
            rows = self._search_fts(terms, count)
        if not rows:
            rows = self._search_like(terms, count)
        return rows

    def _search_fts(self, terms, limit):
        query = match_query(terms)
        if not query:
            return []
        sql = (
            "SELECT c.doc_id AS doc_id, c.locator AS locator, c.text AS text,"
            " d.path AS path, d.name AS name, d.root AS root"
            " FROM %s f JOIN doc_chunks c ON c.id = f.rowid"
            " JOIN documents d ON d.id = c.doc_id"
            " WHERE %s MATCH ? ORDER BY rank LIMIT ?" % (TABLE, TABLE)
        )
        try:
            rows = self.db.query(sql, (query, int(limit)))
        except Exception as exc:
            self.error = str(exc)
            self._warn("knowledge fts query failed: %s", exc)
            return []
        return [self._hit(row, terms) for row in rows]

    def _search_like(self, terms, limit):
        usable = [term for term in terms if term]
        if not usable:
            return []
        clauses = " OR ".join("c.text LIKE ? ESCAPE '\\'" for _ in usable)
        params = ["%" + _escape_like(term) + "%" for term in usable]
        params.append(int(limit))
        sql = (
            "SELECT c.doc_id AS doc_id, c.locator AS locator, c.text AS text,"
            " d.path AS path, d.name AS name, d.root AS root"
            " FROM doc_chunks c JOIN documents d ON d.id = c.doc_id"
            " WHERE %s LIMIT ?" % clauses
        )
        try:
            rows = self.db.query(sql, tuple(params))
        except Exception as exc:
            self.error = str(exc)
            self._warn("knowledge like query failed: %s", exc)
            return []
        return [self._hit(row, terms) for row in rows]

    @staticmethod
    def _hit(row, terms):
        body = str(row.get("text") or "")
        return {
            "doc_id": int(row.get("doc_id") or 0),
            "name": str(row.get("name") or ""),
            "path": str(row.get("path") or ""),
            "root": str(row.get("root") or ""),
            "locator": str(row.get("locator") or ""),
            "text": body,
            "snippet": snippet(body, terms),
        }

    # ---------- 自述 ----------
    def describe(self):
        """给设置页和诊断用：建没建、什么分词、索引里多少条、为什么没建。"""
        ready = bool(self.ensure())
        return {
            "ready": ready,
            "table": TABLE,
            "tokenizer": self.tokenizer,
            "documents": self.count_documents(),
            "chunks": self.count_chunks(),
            "indexed": self.count_indexed() if ready else 0,
            "reason": "" if ready else (self.error or "当前 SQLite 不支持 FTS5"),
        }

    def _existing_sql(self):
        try:
            row = self.db.query_one("SELECT sql FROM sqlite_master WHERE name = ?", (TABLE,))
        except Exception as exc:
            self.error = str(exc)
            return ""
        return str((row or {}).get("sql") or "")

    @staticmethod
    def _tokenizer_of(sql):
        found = _TOKENIZE_RE.search(str(sql or ""))
        return found.group(1).lower() if found else ""

    def _warn(self, message, *args):
        if not self.logger:
            return
        try:
            self.logger.warning(message, *args)
        except Exception:
            pass


def describe(database, logger=None):
    """一句话状态，给自检和诊断用。"""
    return DocumentIndex(database, logger=logger).describe()
