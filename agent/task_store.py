"""任务持久化。只做 SQL 读写，判断留在 Agent 层。

任务历史留得住，重启后能看；待续任务只列出来给用户看，不自动重跑。
"""

import json

from agent.task import TERMINAL_STATES, TaskRecord, TaskState, now_stamp


class TaskStore:
    def __init__(self, database, logger=None):
        self.db = database
        self.logger = logger

    def _warn(self, message, *args):
        if self.logger:
            try:
                self.logger.warning(message, *args)
            except Exception:
                pass

    # ---------- 写入 ----------
    def save(self, record):
        """整条写入。id 已存在就更新，不新增一行。"""
        if record is None or not getattr(record, "id", ""):
            return None
        stamp = now_stamp()
        record.updated_at = stamp
        if not record.created_at:
            record.created_at = stamp
        payload = (
            record.goal,
            record.state,
            record.session,
            record.project,
            int(record.current_step or 0),
            record.pending_question,
            record.pending_confirmation,
            record.error,
            int(record.attempts or 0),
            json.dumps(list(record.plan or []), ensure_ascii=False),
            record.created_at,
            record.updated_at,
        )
        try:
            changed = self.db.execute(
                "UPDATE tasks SET goal=?, state=?, session=?, project=?, current_step=?,"
                " pending_question=?, pending_confirmation=?, error=?, attempts=?,"
                " plan=?, created_at=?, updated_at=? WHERE id=?",
                (*payload, record.id),
            )
            if not changed:
                self.db.execute(
                    "INSERT INTO tasks (goal, state, session, project, current_step,"
                    " pending_question, pending_confirmation, error, attempts, plan,"
                    " created_at, updated_at, id)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*payload, record.id),
                )
        except Exception as exc:
            self._warn("task save failed: %s", exc)
            return None
        return record.id

    def replace_steps(self, task_id, steps):
        """步骤整表替换。步骤少，删了重写比逐条 diff 省事。"""
        try:
            rows = []
            for row in steps or []:
                step = row.to_dict() if hasattr(row, "to_dict") else dict(row or {})
                rows.append(
                    (
                        str(task_id),
                        int(step.get("index") or 0),
                        str(step.get("title") or ""),
                        str(step.get("tool") or ""),
                        str(step.get("status") or "pending"),
                        int(step.get("attempts") or 0),
                        str(step.get("result") or "")[:500],
                        str(step.get("error") or "")[:500],
                        str(step.get("verify") or "")[:500],
                    )
                )
            # 删除和重写在同一个事务里，进程中途退出也不会把步骤表清空。
            with self.db.connect() as conn:
                conn.execute("DELETE FROM task_steps WHERE task_id = ?", (str(task_id),))
                if rows:
                    conn.executemany(
                        "INSERT INTO task_steps (task_id, step_index, title, tool, status,"
                        " attempts, result, error, verify)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        rows,
                    )
        except Exception as exc:
            self._warn("task steps save failed: %s", exc)
            return 0
        return len(steps or [])

    # ---------- 读取 ----------
    def get(self, task_id):
        row = self.db.query_one("SELECT * FROM tasks WHERE id = ?", (str(task_id),))
        return TaskRecord.from_row(row) if row else None

    def steps(self, task_id):
        return self.db.query(
            "SELECT * FROM task_steps WHERE task_id = ? ORDER BY step_index, id",
            (str(task_id),),
        )

    def recent(self, limit=20):
        rows = self.db.query(
            "SELECT * FROM tasks ORDER BY updated_at DESC, id DESC LIMIT ?", (int(limit),)
        )
        return [TaskRecord.from_row(row) for row in rows]

    def unfinished(self, limit=20):
        """待续任务。这些只是列出来，等用户点头才继续。"""
        marks = ",".join("?" * len(TERMINAL_STATES))
        rows = self.db.query(
            f"SELECT * FROM tasks WHERE state NOT IN ({marks})"
            " ORDER BY updated_at DESC, id DESC LIMIT ?",
            (*TERMINAL_STATES, int(limit)),
        )
        return [TaskRecord.from_row(row) for row in rows]

    def mark_interrupted(self):
        """程序上次没跑完就退了：把还在「执行中」的标成等用户。"""
        try:
            return self.db.execute(
                "UPDATE tasks SET state = ?, updated_at = ? WHERE state IN (?, ?)",
                (
                    TaskState.WAITING_USER.value,
                    now_stamp(),
                    TaskState.RUNNING.value,
                    TaskState.WAITING_TOOL.value,
                ),
            )
        except Exception as exc:
            self._warn("task interrupt mark failed: %s", exc)
            return 0

    def stats(self):
        row = self.db.query_one("SELECT COUNT(*) AS n FROM tasks")
        total = int(row["n"]) if row else 0
        marks = ",".join("?" * len(TERMINAL_STATES))
        done = self.db.query_one(
            f"SELECT COUNT(*) AS n FROM tasks WHERE state = ?", (TaskState.COMPLETED.value,)
        )
        pending = self.db.query_one(
            f"SELECT COUNT(*) AS n FROM tasks WHERE state NOT IN ({marks})", TERMINAL_STATES
        )
        return {
            "total": total,
            "completed": int(done["n"]) if done else 0,
            "unfinished": int(pending["n"]) if pending else 0,
        }
