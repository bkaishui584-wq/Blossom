"""关键操作审计。权限判断与执行结果都落库。"""


class AuditLog:
    MAX_DETAIL_CHARS = 500

    def __init__(self, database):
        self.db = database

    def record(self, kind, name="", level="", decision="", detail=""):
        try:
            return self.db.insert(
                "INSERT INTO audit_log (kind, name, level, decision, detail)"
                " VALUES (?, ?, ?, ?, ?)",
                (kind, name, str(level), decision, str(detail)[: self.MAX_DETAIL_CHARS]),
            )
        except Exception:
            # 审计写失败不能拖垮对话，调用方负责记录这个异常。
            return None

    def recent(self, limit=50, kind=None, name=None, decision=None):
        """按时间倒序列出审计。可按 kind / name / decision 过滤。"""
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(str(kind))
        if name:
            clauses.append("name = ?")
            params.append(str(name))
        if decision:
            clauses.append("decision = ?")
            params.append(str(decision))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(int(limit))
        return self.db.query(
            "SELECT id, kind, name, level, decision, detail, created_at"
            f" FROM audit_log{where} ORDER BY id DESC LIMIT ?",
            tuple(params),
        )

    def kinds(self):
        """出现过的审计类别，给界面做筛选用。"""
        return [
            row["kind"]
            for row in self.db.query(
                "SELECT DISTINCT kind FROM audit_log ORDER BY kind ASC"
            )
        ]
