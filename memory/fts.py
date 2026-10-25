"""记忆的全文检索索引（SQLite FTS5）。

索引是派生数据：随时能重建，删掉也不影响记忆本体；记忆只存在 memories 表里。
所以这一层只做三件事：建索引、按关键词找 id、报告自己的状态。

分词选 trigram：中文没有词边界，unicode61 会把一整句当成一个词，等于搜不到。
trigram 按三字子串命中，中英文都能用；SQLite 太老没有 trigram 就退回 unicode61，
再不行整层停用，检索仍走原来的关键词打分。

删除和修改靠触发器同步：记忆一删，索引里那一条也跟着没。
"""

import re

TABLE = "memories_fts"
# 索引跟着这些列：正文与关键词。
_COLUMNS = ("content", "keywords")
# SQLite 自带的 FTS5 用哪个都能试；trigram 优先。
TOKENIZERS = ("trigram", "unicode61")
# trigram 至少三字起才命中；更短的词交给关键词打分，别指望索引。
MIN_TERM = 3
# 一条查询最多拼几个词，别把超长句灌进 MATCH。
MAX_TERMS = 8
_STAMP = (15, "memory_fts")
_TOKENIZE_RE = re.compile(r"tokenize\s*=\s*'([^']+)'", re.I)


def _statements(tokenizer):
    """建表与三个触发器。触发器保证索引跟着 memories 一起增删改。"""
    return (
        "CREATE VIRTUAL TABLE IF NOT EXISTS %s USING fts5("
        "content, keywords, content='memories', content_rowid='id', tokenize='%s')"
        % (TABLE, tokenizer),
        "CREATE TRIGGER IF NOT EXISTS %s_ai AFTER INSERT ON memories BEGIN"
        " INSERT INTO %s(rowid, content, keywords)"
        " VALUES (new.id, new.content, new.keywords); END" % (TABLE, TABLE),
        "CREATE TRIGGER IF NOT EXISTS %s_ad AFTER DELETE ON memories BEGIN"
        " INSERT INTO %s(%s, rowid, content, keywords)"
        " VALUES ('delete', old.id, old.content, old.keywords); END" % (TABLE, TABLE, TABLE),
        "CREATE TRIGGER IF NOT EXISTS %s_au AFTER UPDATE ON memories BEGIN"
        " INSERT INTO %s(%s, rowid, content, keywords)"
        " VALUES ('delete', old.id, old.content, old.keywords);"
        " INSERT INTO %s(rowid, content, keywords)"
        " VALUES (new.id, new.content, new.keywords); END"
        % (TABLE, TABLE, TABLE, TABLE),
    )


def _rebuild_sql():
    return "INSERT INTO %s(%s) VALUES('rebuild')" % (TABLE, TABLE)


class MemoryFts:
    """记忆全文索引。哪个方法都不抛异常，读不到就给空或 False。"""

    def __init__(self, database, logger=None):
        self.db = database
        self.logger = logger
        self.tokenizer = ""
        self.error = ""
        self._ready = None

    # ---------- 建索引 ----------
    def ensure(self, force=False):
        """确保索引在。返回能不能用。建不出来不算错，只是退回关键词打分。"""
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
        self._warn("memory fts unavailable: %s", self.error)
        return False

    def _supported(self, tokenizer):
        """真在库里建一张临时虚表试试，不收版本号当证据。"""
        try:
            self.db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS temp.fts_support_probe USING fts5("
                "body, tokenize='%s')" % tokenizer
            )
            self.db.execute("DROP TABLE IF EXISTS temp.fts_support_probe")
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
            self._warn("memory fts stamp failed: %s", exc)

    def rebuild(self):
        """按 memories 表重灌索引，返回灌进去的条数。"""
        try:
            self.db.execute(_rebuild_sql())
        except Exception as exc:
            self.error = str(exc)
            self._warn("memory fts rebuild failed: %s", exc)
            return 0
        return self.count()

    # ---------- 查 ----------
    def search_ids(self, terms, kinds=None, pet_id=None, limit=200):
        """按关键词找记忆 id，按相关度排。索引用不了就给 None，调用方自己退回。"""
        if not self.ensure():
            return None
        query = match_query(terms, self.tokenizer)
        if not query:
            return None
        sql = (
            "SELECT f.rowid AS id FROM %s f JOIN memories m ON m.id = f.rowid"
            " WHERE %s MATCH ?" % (TABLE, TABLE)
        )
        params = [query]
        kinds = tuple(kinds or ())
        if kinds:
            sql += " AND m.kind IN (%s)" % ", ".join("?" * len(kinds))
            params.extend(kinds)
        if pet_id is not None:
            sql += " AND (m.pet_id = ? OR m.pet_id = '')"
            params.append(str(pet_id))
        # 被取代的、归档的旧记录不参与检索，和原来的候选集口径一致。
        sql += " AND m.superseded_by = 0 AND m.archived = 0 ORDER BY rank LIMIT ?"
        params.append(max(1, int(limit or 200)))
        try:
            rows = self.db.query(sql, tuple(params))
        except Exception as exc:
            self.error = str(exc)
            self._warn("memory fts query failed: %s", exc)
            return None
        return [int(row["id"]) for row in rows]

    def count(self):
        try:
            row = self.db.query_one("SELECT COUNT(*) AS n FROM %s" % TABLE)
        except Exception:
            return 0
        return int((row or {}).get("n") or 0)

    # ---------- 自述 ----------
    def describe(self):
        """给设置页和诊断用：建没建、什么分词、索引里多少条、为什么没建。"""
        ready = bool(self.ensure())
        return {
            "ready": ready,
            "table": TABLE,
            "tokenizer": self.tokenizer,
            "indexed": self.count() if ready else 0,
            "reason": "" if ready else (self.error or "当前 SQLite 不支持 FTS5"),
        }

    def _existing_sql(self):
        try:
            row = self.db.query_one(
                "SELECT sql FROM sqlite_master WHERE name = ?", (TABLE,)
            )
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


def match_query(terms, tokenizer="trigram"):
    """把关键词拼成一条 FTS 查询。拼不出来给空串。"""
    usable = []
    for term in sorted({str(item or "").strip().lower() for item in terms or ()}):
        if len(term) < MIN_TERM:
            continue
        usable.append(term)
        if len(usable) >= MAX_TERMS:
            break
    if not usable:
        return ""
    return " OR ".join('"%s"' % term.replace('"', '""') for term in usable)


def describe(database, logger=None):
    """一句话状态，给自检和诊断用。"""
    return MemoryFts(database, logger=logger).describe()
