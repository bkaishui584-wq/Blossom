"""记忆备份与删除：导出、导入、整库备份/恢复，以及明确的删除操作。

导出只拿用户看得见的数据（长期记忆、偏好、关系、日记），
不含内部表结构、审计记录与 token 记录。
导入只做合并：只补当前没有的，绝不覆盖或删除已有记忆。
删除必须显式确认，否则一律拒绝，避免误删。

整库恢复会先用备份覆盖当前库，覆盖前把当前库另存一份，绝不直接丢掉。
恢复之后需要重启桌宠。
"""

import json
import shutil
import sqlite3
import time
from pathlib import Path

VERSION = 1

# 可导出的记忆层：与记忆系统保持一致，其余层不导出。
EXPORT_KINDS = ("long_term", "project", "relationship", "feedback")

# 没确认就删除时的固定说法，方便程序与测试引用。
NEEDS_CONFIRM = "删除需要先确认，程序不会自动删除任何记忆"

_STAMP = "%Y%m%d-%H%M%S"


def _stamp(clock):
    return time.strftime(_STAMP, time.localtime(clock()))


def export_payload(memory, limit=2000, clock=time.time):
    """把可导出的记忆整理成一个 JSON 结构。缺任何一块都只是少一块，不报错。"""
    repository = getattr(memory, "repository", None)
    memories = []
    preferences = []
    if repository is not None:
        try:
            rows = repository.candidate_memories(
                kinds=EXPORT_KINDS, limit=max(1, int(limit))
            )
        except Exception:
            rows = []
        for row in rows:
            confidence = row.get("confidence")
            memories.append(
                {
                    "kind": row.get("kind", "long_term"),
                    "content": row.get("content", ""),
                    "keywords": row.get("keywords", ""),
                    "importance": int(row.get("importance") or 1),
                    "source": row.get("source", "USER_EXPLICIT"),
                    "confidence": float(confidence if confidence is not None else 1.0),
                    "confirmed": int(row.get("confirmed") or 0),
                    "created_at": str(row.get("created_at") or ""),
                    "last_used_at": str(row.get("last_used_at") or ""),
                }
            )
        try:
            preferences = [
                {"key": row["key"], "value": row["value"]}
                for row in repository.all_preferences()
            ]
        except Exception:
            preferences = []

    relationship = {}
    relation = getattr(memory, "relationship", None)
    if relation is not None:
        try:
            relationship = dict(relation.load() or {})
        except Exception:
            relationship = {}

    diary = []
    diary_service = getattr(memory, "diary", None)
    if diary_service is not None:
        try:
            diary = [
                {"day": row["day"], "content": row["content"]}
                for row in diary_service.read(max(1, int(limit)))
            ]
        except Exception:
            diary = []

    return {
        "version": VERSION,
        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(clock())),
        "memories": memories,
        "preferences": preferences,
        "relationship": relationship,
        "diary": diary,
    }


def export_to_file(memory, path, limit=2000, clock=time.time):
    """导出到一个 JSON 文件。写不动时如实返回失败。"""
    payload = export_payload(memory, limit=limit, clock=clock)
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        return {"ok": False, "path": str(target), "error": str(exc)}
    return {
        "ok": True,
        "path": str(target),
        "memories": len(payload["memories"]),
        "preferences": len(payload["preferences"]),
        "diary": len(payload["diary"]),
    }


def _read_file(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def _already_known(memory, kind, content):
    repository = getattr(memory, "repository", None)
    if repository is None:
        return False
    try:
        rows = repository.candidate_memories(kinds=(kind,), limit=500)
    except Exception:
        return False
    return any(str(row.get("content", "")).strip() == content for row in rows)


def _rows(value):
    """只接受列表。写坏的字段当空处理，不让导入整体失败。"""
    return value if isinstance(value, (list, tuple)) else []


def _has_preference(memory, key):
    try:
        return memory.get_preference(key) is not None
    except Exception:
        return False


def import_payload(memory, data):
    """只补缺的：已经有的跳过，不覆盖、不删除。返回加入与跳过的条数。"""
    if not isinstance(data, dict):
        return {"ok": False, "added": 0, "skipped": 0, "error": "文件格式不对"}
    added = 0
    skipped = 0

    for row in _rows(data.get("memories")):
        if not isinstance(row, dict):
            skipped += 1
            continue
        content = str(row.get("content") or "").strip()
        kind = str(row.get("kind") or "long_term")
        if not content or kind not in EXPORT_KINDS:
            skipped += 1
            continue
        if _already_known(memory, kind, content):
            skipped += 1
            continue
        try:
            confirmed = row.get("confirmed")
            memory.remember(
                content,
                kind=kind,
                importance=int(row.get("importance") or 1),
                source=str(row.get("source") or "USER_EXPLICIT"),
                confirmed=None if confirmed is None else bool(int(confirmed)),
            )
        except Exception:
            skipped += 1
            continue
        added += 1

    for row in _rows(data.get("preferences")):
        if not isinstance(row, dict):
            skipped += 1
            continue
        key = str(row.get("key") or "").strip()
        if not key:
            skipped += 1
            continue
        if _has_preference(memory, key):
            skipped += 1
            continue
        try:
            memory.set_preference(key, str(row.get("value") or ""))
        except Exception:
            skipped += 1
            continue
        added += 1

    return {"ok": True, "added": added, "skipped": skipped}


def import_from_file(memory, path):
    data = _read_file(path)
    if data is None:
        return {
            "ok": False,
            "added": 0,
            "skipped": 0,
            "path": str(path),
            "error": "读不到这个备份文件",
        }
    result = import_payload(memory, data)
    result["path"] = str(path)
    return result


def backup_database(db_path, dest_dir=None, clock=time.time):
    """整库备份。用 SQLite 自带的备份接口，桌宠正在跑也能安全复制。"""
    source = Path(db_path)
    if not source.is_file():
        return {"ok": False, "path": "", "error": "还没有数据库可以备份"}
    target_dir = Path(dest_dir) if dest_dir else source.parent / "backups"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / (source.stem + "-" + _stamp(clock) + ".sqlite3")
        handle = sqlite3.connect(str(source))
        try:
            copy = sqlite3.connect(str(target))
            try:
                handle.backup(copy)
            finally:
                copy.close()
        finally:
            handle.close()
    except Exception as exc:
        return {"ok": False, "path": "", "error": str(exc)}
    return {"ok": target.is_file(), "path": str(target)}


# 一个像样的桌宠备份至少要有这几张表，少一张就不认。
EXPECTED_TABLES = ("memories", "preferences", "messages")


def validate_backup(path):
    """恢复前先验一验：能不能打开、是不是 SQLite、是不是桌宠的库、完整不完整。"""
    source = Path(path)
    if not source.is_file():
        return {"ok": False, "error": "找不到这个备份文件"}
    try:
        handle = sqlite3.connect(str(source))
    except Exception as exc:
        return {"ok": False, "error": f"打不开这个文件：{exc}"}
    try:
        check = handle.execute("PRAGMA integrity_check").fetchone()
        if not check or str(check[0]).lower() != "ok":
            return {"ok": False, "error": "这个备份文件已损坏"}
        names = {
            row[0]
            for row in handle.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    except Exception as exc:
        return {"ok": False, "error": f"这个文件不是 SQLite 备份：{exc}"}
    finally:
        handle.close()
    missing = [name for name in EXPECTED_TABLES if name not in names]
    if missing:
        return {"ok": False, "error": "这个备份不像桌宠的数据库，缺少：" + "、".join(missing)}
    return {"ok": True, "tables": sorted(names)}


def restore_database(db_path, backup_path, clock=time.time, confirm=True):
    """用备份覆盖当前库。覆盖前把当前库另存一份，绝不直接丢掉。

    用 SQLite 自带的备份接口写回去，桌宠正在跑也能安全覆盖。
    备份先验真，确认关闭时不覆盖任何数据。
    """
    target = Path(db_path)
    source = Path(backup_path)
    if not confirm:
        return {"ok": False, "error": "恢复需要先确认，不会静默覆盖现有数据", "safety": ""}
    check = validate_backup(source)
    if not check["ok"]:
        return {"ok": False, "error": check["error"], "safety": ""}
    safety = ""
    try:
        if target.is_file():
            safety_path = target.with_name(
                target.stem + "-before-restore-" + _stamp(clock) + target.suffix
            )
            shutil.copy2(target, safety_path)
            safety = str(safety_path)
        handle = sqlite3.connect(str(source))
        try:
            copy = sqlite3.connect(str(target))
            try:
                handle.backup(copy)
            finally:
                copy.close()
        finally:
            handle.close()
    except Exception as exc:
        return {"ok": False, "error": str(exc), "safety": safety}
    return {"ok": True, "restored": str(target), "safety": safety}


def delete_memory(memory, memory_id, confirm=False):
    """删一条记忆。没有确认就拒绝。"""
    if not confirm:
        return {"ok": False, "removed": 0, "error": NEEDS_CONFIRM}
    try:
        number = int(memory_id)
    except (TypeError, ValueError):
        return {"ok": False, "removed": 0, "error": "记忆编号不对"}
    try:
        removed = memory.repository.delete_memory(number)
    except Exception as exc:
        return {"ok": False, "removed": 0, "error": str(exc)}
    return {"ok": True, "removed": removed}


def clear_kind(memory, kind, confirm=False):
    """清空某一类记忆。没有确认就拒绝。"""
    if not confirm:
        return {"ok": False, "removed": 0, "error": NEEDS_CONFIRM}
    name = str(kind or "").strip()
    if not name:
        return {"ok": False, "removed": 0, "error": "要清空哪一类记忆？"}
    try:
        removed = memory.repository.delete_kind(name)
    except Exception as exc:
        return {"ok": False, "removed": 0, "error": str(exc)}
    return {"ok": True, "removed": removed}


def clear_all(memory, confirm=False):
    """清空全部长期记忆。没有确认就拒绝。"""
    if not confirm:
        return {"ok": False, "removed": 0, "error": NEEDS_CONFIRM}
    try:
        removed = memory.repository.delete_all_memories()
    except Exception as exc:
        return {"ok": False, "removed": 0, "error": str(exc)}
    return {"ok": True, "removed": removed}


def describe(db_path):
    """开发者面板用的只读视图。绝不发给模型。"""
    folder = Path(db_path).parent / "backups"
    try:
        files = sorted(item.name for item in folder.glob("*.sqlite3"))
    except Exception:
        files = []
    return {
        "folder": str(folder),
        "backups": len(files),
        "latest": files[-1] if files else None,
    }
