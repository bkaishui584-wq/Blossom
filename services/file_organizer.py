"""Safe local file organization with preview, history and undo."""

import hashlib
import os
import time
import uuid
from pathlib import Path

CATEGORIES = {
    "images": {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".svg"},
    "documents": {".md", ".txt", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv"},
    "videos": {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm"},
    "audio": {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac"},
    "archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2"},
    "code": {".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".json", ".yml", ".yaml", ".toml", ".sql", ".rs", ".go", ".java", ".cpp", ".c", ".h"},
}


def category_for(path):
    suffix = Path(str(path or "")).suffix.lower()
    for name, suffixes in CATEGORIES.items():
        if suffix in suffixes:
            return name
    return "other"


class FileOrganizer:
    def __init__(self, database, authorized=None, logger=None, clock=time.time):
        self.db = database
        self.authorized = authorized
        self.logger = logger
        self.clock = clock

    def _roots(self):
        if not callable(self.authorized):
            return []
        try:
            values = self.authorized() or []
        except Exception:
            return []
        if isinstance(values, (str, os.PathLike)):
            values = [values]
        return [os.path.realpath(str(item)) for item in values if str(item).strip()]

    def is_authorized(self, path):
        target = os.path.realpath(str(path or ""))
        if not target:
            return False
        for root in self._roots():
            if target == root or target.startswith(root + os.sep):
                return True
        return False

    def _direct_files(self, root, max_files=1000):
        root = os.path.realpath(str(root or ""))
        if not os.path.isdir(root):
            raise ValueError("目录不存在：%s" % root)
        if not self.is_authorized(root):
            raise PermissionError("这个目录还没有授权")
        found = []
        try:
            entries = sorted(os.scandir(root), key=lambda item: item.name.lower())
        except OSError as exc:
            raise ValueError("目录读不了：%s" % exc)
        for entry in entries:
            if len(found) >= max_files:
                break
            try:
                if entry.is_file(follow_symlinks=False) and not entry.name.startswith("."):
                    found.append(os.path.realpath(entry.path))
            except OSError:
                continue
        return found

    def preview(self, root, mode="type", max_files=1000):
        mode = str(mode or "type").lower()
        if mode != "type":
            return {"ok": False, "error": "这一版只支持按文件类型整理"}
        files = self._direct_files(root, max_files=max_files)
        operation_id = "fileop-" + uuid.uuid4().hex[:12]
        now = float(self.clock())
        self.db.execute(
            "INSERT INTO file_operations (id, kind, root, state, note, created_at, updated_at)"
            " VALUES (?, 'organize', ?, 'planned', ?, ?, ?)",
            (operation_id, os.path.realpath(str(root)), "按类型整理", now, now),
        )
        items = []
        conflicts = []
        for source in files:
            category = category_for(source)
            target = os.path.join(os.path.realpath(str(root)), category, os.path.basename(source))
            status = "planned"
            error = ""
            if os.path.exists(target):
                status = "conflict"
                error = "目标文件已存在"
                conflicts.append(target)
            self.db.execute(
                "INSERT INTO file_operation_items"
                " (operation_id, source, target, action, status, error, created_at)"
                " VALUES (?, ?, ?, 'move', ?, ?, ?)",
                (operation_id, source, target, status, error, now),
            )
            items.append(
                {
                    "source": source,
                    "target": target,
                    "category": category,
                    "status": status,
                    "error": error,
                }
            )
        duplicates = self.find_duplicates(root)
        empty = [path for path in files if self._size(path) == 0]
        old = [
            path
            for path in files
            if self._age_days(path) >= 365
        ]
        summary = {
            "operation_id": operation_id,
            "root": os.path.realpath(str(root)),
            "files": len(files),
            "ready": sum(1 for item in items if item["status"] == "planned"),
            "conflicts": len(conflicts),
            "items": items[:200],
            "duplicates": duplicates[:20],
            "empty": empty[:20],
            "old": old[:20],
        }
        note = "文件 %d，可移动 %d，冲突 %d" % (
            summary["files"],
            summary["ready"],
            summary["conflicts"],
        )
        self.db.execute(
            "UPDATE file_operations SET note=?, updated_at=? WHERE id=?",
            (note, now, operation_id),
        )
        return {"ok": True, "data": summary}

    def apply(self, operation_id):
        operation = self.operation(operation_id)
        if operation is None:
            return {"ok": False, "error": "没有这次整理计划"}
        if not self.is_authorized(operation.get("root")):
            return {"ok": False, "error": "目录授权已经撤销，不能继续整理"}
        if operation["state"] != "planned":
            return {"ok": False, "error": "这次计划不是待执行状态"}
        rows = self._items(operation_id)
        moved = failed = 0
        stop = False
        now = float(self.clock())
        for row in rows:
            if stop:
                self._set_item(row["id"], "skipped", "前面的文件冲突，已安全停止")
                continue
            if row["status"] == "conflict":
                self._set_item(row["id"], "failed", row.get("error") or "目标文件已存在")
                failed += 1
                stop = True
                continue
            source, target = row["source"], row["target"]
            if not os.path.isfile(source):
                self._set_item(row["id"], "failed", "来源文件不见了")
                failed += 1
                stop = True
                continue
            if os.path.exists(target):
                self._set_item(row["id"], "failed", "目标文件已存在，没有覆盖")
                failed += 1
                stop = True
                continue
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                os.replace(source, target)
                self._set_item(row["id"], "moved")
                moved += 1
            except Exception as exc:
                self._set_item(row["id"], "failed", str(exc))
                failed += 1
                stop = True
        state = "applied" if not failed else "failed"
        self.db.execute(
            "UPDATE file_operations SET state=?, note=?, error=?, updated_at=? WHERE id=?",
            (
                state,
                "移动 %d 个文件" % moved,
                "" if not failed else "有 %d 个文件没处理，已安全停止" % failed,
                now,
                operation_id,
            ),
        )
        return {
            "ok": not failed,
            "data": {"operation_id": operation_id, "moved": moved, "failed": failed, "state": state},
            "error": "" if not failed else "有文件冲突或失败，已停止后续移动",
        }

    def undo(self, operation_id):
        operation = self.operation(operation_id)
        if operation is None:
            return {"ok": False, "error": "没有这次整理记录"}
        if not self.is_authorized(operation.get("root")):
            return {"ok": False, "error": "目录授权已经撤销，不能撤销这次操作"}
        rows = [row for row in self._items(operation_id) if row["status"] == "moved"]
        if not rows:
            return {"ok": False, "error": "这次操作没有可撤销的移动"}
        restored = failed = 0
        stop = False
        now = float(self.clock())
        for row in reversed(rows):
            if stop:
                continue
            target, source = row["target"], row["source"]
            if not os.path.isfile(target):
                self._set_item(row["id"], "failed", "移动后的文件不见了")
                failed += 1
                stop = True
                continue
            if os.path.exists(source):
                self._set_item(row["id"], "failed", "原位置已经有同名文件，没有覆盖")
                failed += 1
                stop = True
                continue
            try:
                os.makedirs(os.path.dirname(source), exist_ok=True)
                os.replace(target, source)
                self._set_item(row["id"], "restored", undone=True)
                restored += 1
            except Exception as exc:
                self._set_item(row["id"], "failed", str(exc))
                failed += 1
                stop = True
        state = "undone" if not failed else "undo_failed"
        self.db.execute(
            "UPDATE file_operations SET state=?, note=?, error=?, updated_at=? WHERE id=?",
            (
                state,
                "恢复 %d 个文件" % restored,
                "" if not failed else "撤销时遇到 %d 个冲突" % failed,
                now,
                operation_id,
            ),
        )
        return {
            "ok": not failed,
            "data": {"operation_id": operation_id, "restored": restored, "failed": failed, "state": state},
            "error": "" if not failed else "撤销没有全部完成",
        }

    def operation(self, operation_id):
        return self.db.query_one(
            "SELECT * FROM file_operations WHERE id = ?", (str(operation_id or ""),)
        )

    def _items(self, operation_id):
        return self.db.query(
            "SELECT * FROM file_operation_items WHERE operation_id = ? ORDER BY id",
            (str(operation_id or ""),),
        )

    def items(self, operation_id):
        return self._items(operation_id)

    def history(self, limit=50):
        rows = self.db.query(
            "SELECT * FROM file_operations ORDER BY created_at DESC LIMIT ?",
            (max(1, min(200, int(limit or 50))),),
        )
        for row in rows:
            counts = self.db.query_one(
                "SELECT COUNT(*) AS n,"
                " SUM(CASE WHEN status='moved' THEN 1 ELSE 0 END) AS moved,"
                " SUM(CASE WHEN status='restored' THEN 1 ELSE 0 END) AS restored"
                " FROM file_operation_items WHERE operation_id=?",
                (row["id"],),
            ) or {}
            row["items"] = int(counts.get("n") or 0)
            row["moved"] = int(counts.get("moved") or 0)
            row["restored"] = int(counts.get("restored") or 0)
        return rows

    def find_duplicates(self, root, max_files=500, max_bytes=20 * 1024 * 1024):
        files = self._direct_files(root, max_files=max_files)
        by_size = {}
        for path in files:
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if size <= 0 or size > max_bytes:
                continue
            by_size.setdefault(size, []).append(path)
        groups = []
        for size, paths in by_size.items():
            if len(paths) < 2:
                continue
            by_hash = {}
            for path in paths:
                digest = self._hash(path)
                if digest:
                    by_hash.setdefault(digest, []).append(path)
            for digest, same in by_hash.items():
                if len(same) > 1:
                    groups.append({"size": size, "sha256": digest, "paths": same})
        return groups

    def _set_item(self, item_id, status, error="", undone=False):
        self.db.execute(
            "UPDATE file_operation_items SET status=?, error=?, undone_at=? WHERE id=?",
            (status, str(error or "")[:500], float(self.clock()) if undone else None, int(item_id)),
        )

    @staticmethod
    def _hash(path):
        digest = hashlib.sha256()
        try:
            with open(path, "rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
        except OSError:
            return ""
        return digest.hexdigest()

    @staticmethod
    def _size(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

    @staticmethod
    def _age_days(path):
        try:
            return max(0, (time.time() - os.path.getmtime(path)) / 86400.0)
        except OSError:
            return 0
