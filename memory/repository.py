"""记忆存储层。只做 SQL 读写，不含策略判断。"""


class MemoryRepository:
    def __init__(self, database):
        self.db = database

    # ---------- 对话（短期记忆） ----------
    def add_message(self, session_id, role, content):
        return self.db.insert(
            "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
            (session_id, role, content),
        )

    def sessions(self, limit=20):
        """有消息的会话列表，按最近活跃排。只读，不建新表。"""
        return self.db.query(
            "SELECT session_id, COUNT(*) AS total, MAX(id) AS last_id,"
            " MAX(created_at) AS last_at FROM messages"
            " GROUP BY session_id ORDER BY last_id DESC LIMIT ?",
            (int(limit),),
        )

    # ---------- 会话元数据（改名、置顶、归档、标签） ----------
    def session_meta(self, session_id):
        return self.db.query_one(
            "SELECT * FROM session_meta WHERE session_id = ?", (str(session_id),)
        )

    def ensure_session(self, session_id, title=""):
        """只补一行元数据，不动任何消息。"""
        return self.db.execute(
            "INSERT INTO session_meta (session_id, title) VALUES (?, ?)"
            " ON CONFLICT(session_id) DO NOTHING",
            (str(session_id), str(title or "")),
        )

    def update_session(self, session_id, title=None, pinned=None, archived=None, tags=None):
        sets, params = [], []
        if title is not None:
            sets.append("title = ?")
            params.append(str(title))
        if pinned is not None:
            sets.append("pinned = ?")
            params.append(int(bool(pinned)))
        if archived is not None:
            sets.append("archived = ?")
            params.append(int(bool(archived)))
        if tags is not None:
            sets.append("tags = ?")
            params.append(str(tags))
        if not sets:
            return 0
        sets.append("updated_at = datetime('now')")
        params.append(str(session_id))
        self.ensure_session(session_id)
        return self.db.execute(
            "UPDATE session_meta SET %s WHERE session_id = ?" % ", ".join(sets),
            tuple(params),
        )

    def session_overview(self, limit=200, include_archived=False):
        """会话列表：消息统计 + 元数据，最近活跃在前，置顶的在最前。"""
        sql = (
            "SELECT s.session_id, s.total, s.last_id, s.last_at,"
            " COALESCE(m.title, '') AS title,"
            " COALESCE(m.pinned, 0) AS pinned,"
            " COALESCE(m.archived, 0) AS archived,"
            " COALESCE(m.tags, '') AS tags"
            " FROM (SELECT session_id, COUNT(*) AS total, MAX(id) AS last_id,"
            " MAX(created_at) AS last_at FROM messages GROUP BY session_id) AS s"
            " LEFT JOIN session_meta AS m ON m.session_id = s.session_id"
        )
        if not include_archived:
            sql += " WHERE COALESCE(m.archived, 0) = 0"
        sql += " ORDER BY COALESCE(m.pinned, 0) DESC, s.last_id DESC LIMIT ?"
        return self.db.query(sql, (int(limit),))

    def delete_session(self, session_id):
        """删一个会话：消息、摘要、元数据一起走。只动这一条。"""
        session_id = str(session_id)
        removed = self.db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        self.db.execute("DELETE FROM summaries WHERE session_id = ?", (session_id,))
        self.db.execute("DELETE FROM session_meta WHERE session_id = ?", (session_id,))
        return removed

    def recent_messages(self, session_id, limit=12, unsummarized_only=False):
        sql = "SELECT id, role, content, summarized FROM messages WHERE session_id = ?"
        if unsummarized_only:
            sql += " AND summarized = 0"
        sql += " ORDER BY id DESC LIMIT ?"
        rows = self.db.query(sql, (session_id, limit))
        rows.reverse()
        return rows

    def count_unsummarized(self, session_id):
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM messages WHERE session_id = ? AND summarized = 0",
            (session_id,),
        )
        return row["n"] if row else 0

    def mark_summarized(self, ids):
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        return self.db.execute(
            f"UPDATE messages SET summarized = 1 WHERE id IN ({marks})", tuple(ids)
        )

    def add_summary(self, session_id, content, covers_until_message_id):
        return self.db.insert(
            "INSERT INTO summaries (session_id, content, covers_until_message_id)"
            " VALUES (?, ?, ?)",
            (session_id, content, covers_until_message_id),
        )

    def latest_summary(self, session_id):
        return self.db.query_one(
            "SELECT content FROM summaries WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (session_id,),
        )

    # ---------- 长期记忆 ----------
    # 一条记忆要能回答：说了什么、谁说的、多重要、可信吗、谁把它取代了、用过没。
    _COLUMNS = (
        "id, kind, content, keywords, importance, source, confidence,"
        " confirmed, locked, archived, created_at, last_used_at, pet_id,"
        " julianday('now') - julianday(created_at) AS age_days"
    )

    def add_memory(
        self,
        content,
        kind="long_term",
        keywords="",
        importance=1,
        source="USER_EXPLICIT",
        confidence=1.0,
        confirmed=None,
        locked=0,
        pet_id="",
    ):
        if confirmed is None:
            # 只有用户亲口说的才算已确认；观察和推断先当未确认。
            confirmed = 1 if source == "USER_EXPLICIT" else 0
        return self.db.insert(
            "INSERT INTO memories (kind, content, keywords, importance, source, confidence,"
            " confirmed, locked, pet_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                kind,
                content,
                keywords,
                importance,
                source,
                float(confidence),
                int(bool(confirmed)),
                int(bool(locked)),
                str(pet_id or ""),
            ),
        )

    @staticmethod
    def _owner_clause(pet_id):
        """归属过滤：宠物只看自己的和公共的，看不到别只宠物的。"""
        if pet_id is None:
            return "", []
        return " AND (pet_id = ? OR pet_id = '')", [str(pet_id)]

    @staticmethod
    def _own_clause(pet_id):
        """只动自己的记录，公共记录不碰。删除这类不可逆操作走这里。"""
        if pet_id is None:
            return "", []
        return " AND pet_id = ?", [str(pet_id)]

    def candidate_memories(self, kinds=("long_term", "project"), limit=200, pet_id=None):
        """可选记忆。被取代或归档的旧记录仍在库里，但不参与检索。"""
        marks = ",".join("?" * len(kinds))
        extra, params = self._owner_clause(pet_id)
        return self.db.query(
            f"SELECT {self._COLUMNS} FROM memories"
            f" WHERE kind IN ({marks}) AND superseded_by = 0 AND archived = 0"
            + extra
            + " ORDER BY importance DESC, id DESC LIMIT ?",
            (*kinds, *params, limit),
        )

    def memories_by_ids(self, ids, pet_id=None):
        """按 id 取记忆，返回顺序跟传入的 id 顺序一致（检索相关度顺序）。"""
        clean = [int(item) for item in (ids or [])]
        if not clean:
            return []
        marks = ",".join("?" * len(clean))
        extra, params = self._owner_clause(pet_id)
        rows = self.db.query(
            f"SELECT {self._COLUMNS} FROM memories"
            f" WHERE id IN ({marks}) AND superseded_by = 0 AND archived = 0" + extra,
            (*clean, *params),
        )
        order = {value: index for index, value in enumerate(clean)}
        rows.sort(key=lambda row: order.get(int(row["id"]), len(order)))
        return rows

    def memories_by_kind(self, kind, limit=20, pet_id=None):
        """按层取记忆。人格反馈这类要全部拿到的，就走这里。"""
        extra, params = self._owner_clause(pet_id)
        return self.db.query(
            f"SELECT {self._COLUMNS} FROM memories"
            " WHERE kind = ? AND superseded_by = 0 AND archived = 0"
            + extra
            + " ORDER BY importance DESC, id DESC LIMIT ?",
            (kind, *params, max(1, int(limit))),
        )

    def get_memory(self, memory_id, pet_id=None):
        """取一条记忆。带归属时只看得到自己的和公共的。"""
        extra, params = self._owner_clause(pet_id)
        return self.db.query_one(
            f"SELECT {self._COLUMNS}, superseded_by FROM memories WHERE id = ?" + extra,
            (int(memory_id), *params),
        )

    def find_active(self, kind, content, pet_id=None):
        """同一层里有没有一模一样的一条，用来去重。归属不同不算同一条。"""
        sql = (
            "SELECT id FROM memories WHERE kind = ? AND content = ?"
            " AND superseded_by = 0 AND archived = 0"
        )
        params = [str(kind), str(content)]
        if pet_id is not None:
            sql += " AND pet_id = ?"
            params.append(str(pet_id))
        return self.db.query_one(sql + " ORDER BY id LIMIT 1", tuple(params))

    def update_memory(
        self, memory_id, content=None, importance=None, keywords=None, pet_id=None
    ):
        """用户手动改一条记忆。只改传进来的字段。"""
        sets, params = [], []
        if content is not None:
            sets.append("content = ?")
            params.append(str(content))
        if keywords is not None:
            sets.append("keywords = ?")
            params.append(str(keywords))
        if importance is not None:
            sets.append("importance = ?")
            params.append(int(importance))
        if not sets:
            return 0
        params.append(int(memory_id))
        extra, own = self._owner_clause(pet_id)
        return self.db.execute(
            f"UPDATE memories SET {', '.join(sets)} WHERE id = ?" + extra,
            tuple(params + own),
        )

    def set_locked(self, memory_id, locked=True, pet_id=None):
        extra, params = self._owner_clause(pet_id)
        return self.db.execute(
            "UPDATE memories SET locked = ? WHERE id = ?" + extra,
            (int(bool(locked)), int(memory_id), *params),
        )

    def confirm_memory(self, memory_id, confirmed=True, pet_id=None):
        extra, params = self._owner_clause(pet_id)
        return self.db.execute(
            "UPDATE memories SET confirmed = ? WHERE id = ?" + extra,
            (int(bool(confirmed)), int(memory_id), *params),
        )

    def supersede(self, old_ids, new_id):
        """旧记忆不删除，只标记被谁取代，历史继续留在库里。"""
        if not old_ids:
            return 0
        marks = ",".join("?" * len(old_ids))
        return self.db.execute(
            f"UPDATE memories SET superseded_by = ? WHERE id IN ({marks})",
            (new_id, *old_ids),
        )

    def touch_memory(self, ids):
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        return self.db.execute(
            f"UPDATE memories SET last_used_at = datetime('now') WHERE id IN ({marks})",
            tuple(ids),
        )

    def count_memories(self, kind=None, include_archived=False):
        sql = "SELECT COUNT(*) AS n FROM memories WHERE superseded_by = 0"
        params = ()
        if not include_archived:
            sql += " AND archived = 0"
        if kind:
            sql += " AND kind = ?"
            params = (str(kind),)
        row = self.db.query_one(sql, params)
        return int(row["n"]) if row else 0

    def archived_count(self):
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM memories WHERE superseded_by = 0 AND archived = 1"
        )
        return int(row["n"]) if row else 0

    def kind_counts(self, include_archived=False):
        sql = "SELECT kind, COUNT(*) AS n FROM memories WHERE superseded_by = 0"
        if not include_archived:
            sql += " AND archived = 0"
        sql += " GROUP BY kind ORDER BY n DESC, kind"
        return {row["kind"]: int(row["n"]) for row in self.db.query(sql)}

    def browse(
        self,
        kind=None,
        query=None,
        include_archived=False,
        limit=200,
        offset=0,
        pet_id=None,
    ):
        """记忆管理界面用：按层、按关键词翻看。只读。"""
        sql = f"SELECT {self._COLUMNS}, superseded_by FROM memories WHERE superseded_by = 0"
        params = []
        owner_extra, owner_params = self._owner_clause(pet_id)
        if owner_extra:
            sql += owner_extra
            params.extend(owner_params)
        if not include_archived:
            sql += " AND archived = 0"
        if kind:
            sql += " AND kind = ?"
            params.append(str(kind))
        if query:
            sql += " AND (content LIKE ? OR keywords LIKE ?)"
            like = "%" + str(query) + "%"
            params.extend([like, like])
        sql += " ORDER BY importance DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([max(1, int(limit)), max(0, int(offset))])
        return self.db.query(sql, tuple(params))

    def archive_overflow(self, capacity):
        """超出容量的部分不删除，只归档：不再参与检索，仍能查看和恢复。"""
        capacity = max(1, int(capacity))
        total = self.count_memories()
        if total <= capacity:
            return 0
        overflow = total - capacity
        rows = self.db.query(
            "SELECT id FROM memories WHERE superseded_by = 0 AND archived = 0 AND locked = 0"
            " ORDER BY importance ASC, id ASC LIMIT ?",
            (overflow,),
        )
        ids = [row["id"] for row in rows]
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        return self.db.execute(
            f"UPDATE memories SET archived = 1 WHERE id IN ({marks})", tuple(ids)
        )

    def set_archived(self, memory_id, archived=True, pet_id=None):
        """归档或恢复一条记忆。归档只是不再参与检索，内容仍留在库里。"""
        extra, params = self._owner_clause(pet_id)
        return self.db.execute(
            "UPDATE memories SET archived = ? WHERE id = ?" + extra,
            (int(bool(archived)), int(memory_id), *params),
        )

    # ---------- 删除（只在用户明确确认后调用） ----------
    def delete_memory(self, memory_id, pet_id=None):
        extra, params = self._owner_clause(pet_id)
        return self.db.execute(
            "DELETE FROM memories WHERE id = ?" + extra, (int(memory_id), *params)
        )

    def delete_kind(self, kind, pet_id=None):
        extra, params = self._own_clause(pet_id)
        return self.db.execute(
            "DELETE FROM memories WHERE kind = ?" + extra, (str(kind), *params)
        )

    def delete_all_memories(self, pet_id=None):
        extra, params = self._own_clause(pet_id)
        return self.db.execute("DELETE FROM memories WHERE 1 = 1" + extra, tuple(params))

    # ---------- 用户偏好 ----------
    def set_preference(self, key, value):
        previous = self.get_preference(key)
        text = str(value)
        self.db.execute(
            "INSERT INTO preferences (key, value, updated_at) VALUES (?, ?, datetime('now'))"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at",
            (key, text),
        )
        if previous is not None and previous != text:
            # 旧值留一份历史，用户改了偏好也追得回来。
            try:
                self.db.insert(
                    "INSERT INTO preference_history (key, old_value, new_value)"
                    " VALUES (?, ?, ?)",
                    (key, previous, text),
                )
            except Exception:
                pass
        return True

    def get_preference(self, key):
        row = self.db.query_one("SELECT value FROM preferences WHERE key = ?", (key,))
        return row["value"] if row else None

    def all_preferences(self):
        return self.db.query("SELECT key, value FROM preferences ORDER BY key")

    def preference_history(self, key=None, limit=50):
        """某个偏好被改过的历史。key 为空就返回最近的若干条。"""
        sql = "SELECT key, old_value, new_value, changed_at FROM preference_history"
        params = ()
        if key:
            sql += " WHERE key = ?"
            params = (str(key),)
        sql += " ORDER BY id DESC LIMIT ?"
        return self.db.query(sql, (*params, max(1, int(limit))))

    # ---------- Token 使用 ----------
    def record_usage(self, session_id, model, prompt_tokens, completion_tokens):
        return self.db.insert(
            "INSERT INTO token_usage (session_id, model, prompt_tokens, completion_tokens)"
            " VALUES (?, ?, ?, ?)",
            (session_id, model, int(prompt_tokens or 0), int(completion_tokens or 0)),
        )

    def usage_totals(self, session_id=None):
        sql = (
            "SELECT COUNT(*) AS calls, COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,"
            " COALESCE(SUM(completion_tokens), 0) AS completion_tokens FROM token_usage"
        )
        params = ()
        if session_id:
            sql += " WHERE session_id = ?"
            params = (session_id,)
        row = self.db.query_one(sql, params) or {}
        row["total_tokens"] = (row.get("prompt_tokens") or 0) + (row.get("completion_tokens") or 0)
        return row
