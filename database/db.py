"""SQLite 访问层。数据库操作只经过这里，模型不能直接读写数据库。"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

# (version, name, sql)：新表结构按版本追加，不修改已发布的条目。
MIGRATIONS = [
    (
        14,
        "session_meta",
        """
        CREATE TABLE IF NOT EXISTS session_meta (
            session_id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            pinned INTEGER NOT NULL DEFAULT 0,
            archived INTEGER NOT NULL DEFAULT 0,
            tags TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_session_meta_flags
            ON session_meta(archived, pinned, updated_at DESC);
        """,
    ),
    (
        1,
        "memory_core",
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            summarized INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

        CREATE TABLE IF NOT EXISTS summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            content TEXT NOT NULL,
            covers_until_message_id INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_summaries_session ON summaries(session_id, id);

        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL DEFAULT 'long_term',
            content TEXT NOT NULL,
            keywords TEXT NOT NULL DEFAULT '',
            importance INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            last_used_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind, id);

        CREATE TABLE IF NOT EXISTS preferences (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS token_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """,
    ),
    (
        2,
        "audit_log",
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            level TEXT NOT NULL DEFAULT '',
            decision TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_audit_kind ON audit_log(kind, id);
        """,
    ),
    (
        3,
        "calendar_events",
        """
        CREATE TABLE IF NOT EXISTS calendar_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            when_at TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_calendar_when ON calendar_events(when_at, id);
        """,
    ),
    (
        4,
        "memory_v2",
        """
        ALTER TABLE memories ADD COLUMN source TEXT NOT NULL DEFAULT 'USER_EXPLICIT';
        ALTER TABLE memories ADD COLUMN confidence REAL NOT NULL DEFAULT 1.0;
        ALTER TABLE memories ADD COLUMN superseded_by INTEGER NOT NULL DEFAULT 0;
        CREATE INDEX IF NOT EXISTS idx_memories_active ON memories(kind, superseded_by, id);
        """,
    ),
    (
        5,
        "persona_samples",
        """
        CREATE TABLE IF NOT EXISTS persona_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            content TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'USER_EXPLICIT',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_persona_samples_kind ON persona_samples(kind, id);
        """,
    ),
    (
        6,
        "diary_entries",
        """
        CREATE TABLE IF NOT EXISTS diary_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day TEXT NOT NULL UNIQUE,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_diary_day ON diary_entries(day DESC);
        """,
    ),
    (
        7,
        "relationship_state",
        """
        CREATE TABLE IF NOT EXISTS relationship_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            data TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """,
    ),
    (
        8,
        "timeline",
        """
        CREATE TABLE IF NOT EXISTS timeline (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL UNIQUE,
            detail TEXT NOT NULL DEFAULT '',
            happened_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """,
    ),
    (
        9,
        "memory_v3",
        """
        ALTER TABLE memories ADD COLUMN confirmed INTEGER NOT NULL DEFAULT 1;
        ALTER TABLE memories ADD COLUMN locked INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE memories ADD COLUMN archived INTEGER NOT NULL DEFAULT 0;
        CREATE INDEX IF NOT EXISTS idx_memories_browse ON memories(archived, kind, id);

        CREATE TABLE IF NOT EXISTS preference_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            changed_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_pref_history_key ON preference_history(key, id);
        """,
    ),
    (
        10,
        "memory_owner",
        """
        ALTER TABLE memories ADD COLUMN pet_id TEXT NOT NULL DEFAULT '';
        CREATE INDEX IF NOT EXISTS idx_memories_owner ON memories(pet_id, kind, id);
        """,
    ),
    (
        12,
        "skill_runs",
        """
        CREATE TABLE IF NOT EXISTS skill_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            skill TEXT NOT NULL,
            request TEXT NOT NULL DEFAULT '',
            ok INTEGER NOT NULL DEFAULT 0,
            steps INTEGER NOT NULL DEFAULT 0,
            seconds REAL NOT NULL DEFAULT 0,
            tools TEXT NOT NULL DEFAULT '',
            tokens INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_skill_runs_skill ON skill_runs(skill, id DESC);
        """,
    ),
    (
        11,
        "agent_tasks",
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            goal TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'new',
            session TEXT NOT NULL DEFAULT '',
            project TEXT NOT NULL DEFAULT '',
            current_step INTEGER NOT NULL DEFAULT 0,
            pending_question TEXT NOT NULL DEFAULT '',
            pending_confirmation TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            attempts INTEGER NOT NULL DEFAULT 0,
            plan TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state, updated_at DESC);

        CREATE TABLE IF NOT EXISTS task_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            step_index INTEGER NOT NULL DEFAULT 0,
            title TEXT NOT NULL DEFAULT '',
            tool TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            result TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            verify TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_task_steps ON task_steps(task_id, step_index);
        """,
    ),
    (
        13,
        "local_todos",
        """
        CREATE TABLE IF NOT EXISTS todos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '',
            due_at TEXT NOT NULL DEFAULT '',
            priority INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'open',
            source TEXT NOT NULL DEFAULT 'USER_EXPLICIT',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            done_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_todos_state ON todos(state, due_at, id);
        """,
    ),
    (
        16,
        "knowledge_base",
        """
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL DEFAULT '',
            format TEXT NOT NULL DEFAULT '',
            size INTEGER NOT NULL DEFAULT 0,
            mtime REAL NOT NULL DEFAULT 0,
            engine TEXT NOT NULL DEFAULT '',
            chunks INTEGER NOT NULL DEFAULT 0,
            chars INTEGER NOT NULL DEFAULT 0,
            root TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_documents_root ON documents(root);

        CREATE TABLE IF NOT EXISTS doc_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id INTEGER NOT NULL,
            ord INTEGER NOT NULL DEFAULT 0,
            locator TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_doc_chunks_doc ON doc_chunks(doc_id, ord);

        CREATE TABLE IF NOT EXISTS doc_sources (
            root TEXT PRIMARY KEY,
            note TEXT NOT NULL DEFAULT '',
            docs INTEGER NOT NULL DEFAULT 0,
            added_at TEXT NOT NULL DEFAULT (datetime('now')),
            last_scan TEXT NOT NULL DEFAULT ''
        );
        """,
    ),
    (
        17,
        "scheduled_jobs",
        """
        CREATE TABLE IF NOT EXISTS scheduled_jobs (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'once',
            event TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}',
            state TEXT NOT NULL DEFAULT 'pending',
            due_at REAL NOT NULL,
            next_run_at REAL,
            interval_seconds INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            retryable INTEGER NOT NULL DEFAULT 1,
            idempotent INTEGER NOT NULL DEFAULT 1,
            idempotency_key TEXT NOT NULL DEFAULT '',
            result TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            last_run_at REAL,
            cancelled_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_due
            ON scheduled_jobs(state, next_run_at, due_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduled_jobs_key
            ON scheduled_jobs(idempotency_key)
            WHERE idempotency_key <> '';

        CREATE TABLE IF NOT EXISTS scheduled_job_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            state TEXT NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 0,
            started_at REAL NOT NULL,
            finished_at REAL,
            result TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_scheduled_job_runs_job
            ON scheduled_job_runs(job_id, id DESC);
        """,
    ),
    (
        18,
        "calendar_v2",
        """
        ALTER TABLE calendar_events ADD COLUMN end_at TEXT NOT NULL DEFAULT '';
        ALTER TABLE calendar_events ADD COLUMN priority INTEGER NOT NULL DEFAULT 1;
        ALTER TABLE calendar_events ADD COLUMN repeat_rule TEXT NOT NULL DEFAULT '';
        ALTER TABLE calendar_events ADD COLUMN remind_minutes INTEGER NOT NULL DEFAULT 10;
        ALTER TABLE calendar_events ADD COLUMN state TEXT NOT NULL DEFAULT 'open';
        ALTER TABLE calendar_events ADD COLUMN todo_id INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE calendar_events ADD COLUMN updated_at TEXT NOT NULL DEFAULT '';
        CREATE INDEX IF NOT EXISTS idx_calendar_state
            ON calendar_events(state, when_at, id);
        """,
    ),
    (
        19,
        "file_operations",
        """
        CREATE TABLE IF NOT EXISTS file_operations (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL DEFAULT 'organize',
            root TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'planned',
            note TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_file_operations_state
            ON file_operations(state, created_at DESC);

        CREATE TABLE IF NOT EXISTS file_operation_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_id TEXT NOT NULL,
            source TEXT NOT NULL,
            target TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL DEFAULT 'move',
            status TEXT NOT NULL DEFAULT 'planned',
            error TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            undone_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_file_operation_items_op
            ON file_operation_items(operation_id, id);
        """,
    ),
    (
        20,
        "work_records",
        """
        CREATE TABLE IF NOT EXISTS work_intervals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            app TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT '',
            domain TEXT NOT NULL DEFAULT '',
            project TEXT NOT NULL DEFAULT '',
            started_at REAL NOT NULL,
            ended_at REAL,
            seconds INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'foreground',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_work_intervals_day
            ON work_intervals(started_at DESC, id);

        CREATE TABLE IF NOT EXISTS work_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_work_audit_created
            ON work_audit(created_at DESC, id);
        """,
    ),
    (
        21,
        "model_call_log",
        """
        CREATE TABLE IF NOT EXISTS model_call_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT '',
            provider TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'ok',
            latency_ms INTEGER NOT NULL DEFAULT 0,
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            error_kind TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            streamed INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_model_call_log_created
            ON model_call_log(created_at DESC, id);
        """,
    ),
    (
        22,
        "subtitle_segments",
        """
        CREATE TABLE IF NOT EXISTS subtitle_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            segment_id TEXT NOT NULL,
            source_language TEXT NOT NULL DEFAULT '',
            target_language TEXT NOT NULL DEFAULT '',
            source_text TEXT NOT NULL DEFAULT '',
            translated_text TEXT NOT NULL DEFAULT '',
            is_final INTEGER NOT NULL DEFAULT 0,
            recognition_status TEXT NOT NULL DEFAULT '',
            translation_status TEXT NOT NULL DEFAULT '',
            translation_error TEXT NOT NULL DEFAULT '',
            start_time REAL NOT NULL DEFAULT 0,
            end_time REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            UNIQUE(session_id, segment_id)
        );
        CREATE INDEX IF NOT EXISTS idx_subtitle_segments_created
            ON subtitle_segments(created_at DESC, id DESC);
        """,
    ),
    # 15 memory_fts：记忆全文索引。它由 memory/fts.py 在运行时按需建立，
    # 不写成声明式迁移：精简版 SQLite 未必有 FTS5，写死在这里会让启动直接失败。
    # 建成功时会在 schema_migrations 里补一条 (15, "memory_fts")。
]


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        except Exception:
            # 建连接的准备步骤也可能失败。失败也要把连接收掉，
            # 不然这个手会把库锁住，后面每一次连接都只能干等。
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def execute(self, sql, params=()):
        with self.connect() as conn:
            return conn.execute(sql, params).rowcount

    def insert(self, sql, params=()):
        with self.connect() as conn:
            return conn.execute(sql, params).lastrowid

    def query(self, sql, params=()):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params)]

    def query_one(self, sql, params=()):
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def migrate(self):
        """按版本增量建表，可重复执行。返回本次应用的版本号。"""
        applied = []
        with self.connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY,"
                " name TEXT NOT NULL,"
                " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
            )
            done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
            for version, name, sql in sorted(MIGRATIONS):
                if version in done:
                    continue
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name) VALUES (?, ?)", (version, name)
                )
                applied.append(version)
        return applied

    def health(self):
        with self.connect() as conn:
            version = conn.execute("SELECT sqlite_version()").fetchone()[0]
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            ]
        return {"path": str(self.path), "sqlite_version": version, "tables": tables}
