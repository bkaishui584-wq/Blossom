"""本地能力补充工具：文本整理、任务待续、自检、备份。

全部由程序自己判断，不需要模型也不联网。备份和恢复沿用 memory/backup.py，
走 SQLite 自带的备份接口，桌宠在跑也能安全复制。高风险动作不给自己开后门：
恢复必须过权限中心的确认，工具本身不替用户拿主意。
"""

import re
import shutil
from pathlib import Path

from memory import backup as memory_backup
from tools.registry import Tool, ToolError

LOG_TAIL_LINES = 400
LOG_SAMPLE = 5
LOG_BAD_WORDS = ("error", "critical", "traceback", "exception", "错误", "失败", "异常")
BACKUP_LIMIT = 20

_TRAILING = re.compile(r"[ \t]+$", re.MULTILINE)
_BLANK_RUN = re.compile(r"\n{3,}")
_WORD = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")

TEXT_ACTIONS = ("clean", "dedupe", "stats")


def clean_text(text):
    """收拾常见脏东西：BOM、CRLF、行尾空白、连续空行。"""
    value = str(text or "").replace("\ufeff", "")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = _TRAILING.sub("", value)
    value = _BLANK_RUN.sub("\n\n", value)
    return value.strip("\n")


def dedupe_lines(text):
    """去掉重复的有内容行，保留第一次出现的顺序。返回 (文本, 去掉了几行)。"""
    seen = set()
    kept = []
    dropped = 0
    for line in clean_text(text).split("\n"):
        key = line.strip()
        if not key:
            kept.append(line)
            continue
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        kept.append(line)
    return "\n".join(kept).strip("\n"), dropped


def count_text(text):
    value = clean_text(text)
    lines = value.split("\n") if value else []
    return {
        "chars": len(value),
        "lines": len(lines),
        "blank": sum(1 for line in lines if not line.strip()),
        "words": len(_WORD.findall(value)),
    }


def writable_dir(path):
    """真的写一个空文件试一下，不靠权限位猜。"""
    target = Path(path)
    if not target.is_dir():
        return False
    probe = target / ".blossom-write-probe"
    try:
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def log_problems(log_dir, limit=LOG_SAMPLE):
    """日志尾巴里最像出问题的几行。读不到就返回空，不编。"""
    if not log_dir:
        return []
    path = Path(log_dir) / "deskpet.log"
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    hits = [
        line.strip()
        for line in lines[-LOG_TAIL_LINES:]
        if any(word in line.lower() for word in LOG_BAD_WORDS)
    ]
    return hits[-limit:]


def _as_text(payload):
    text = str(payload or "").strip()
    return text or None


def register_extra_tools(
    registry,
    database=None,
    config=None,
    task_store=None,
    grants=None,
    clock=None,
    logger=None,
):
    """装上文本整理、任务待续、自检和备份这组工具。

    grants 传一个返回当前持久授权清单的函数，自检里只报数量，不报细节。
    """

    def _db_path():
        path = getattr(config, "db_path", "") if config is not None else ""
        if not path and database is not None:
            path = getattr(database, "path", "")
        return str(path or "")

    def _backup_dir():
        path = _db_path()
        return Path(path).parent / "backups" if path else None

    # ---------- 文本整理 ----------
    def text_process_handler(text="", what="clean"):
        action = str(what or "clean").strip().lower()
        if action not in TEXT_ACTIONS:
            raise ToolError("不认识的整理方式：%s（只认 clean、dedupe、stats）" % what)
        if action == "clean":
            return clean_text(text)
        if action == "dedupe":
            body, dropped = dedupe_lines(text)
            if body:
                return body
            return "去重后没有剩下内容（去掉 %d 行重复）" % dropped
        info = count_text(text)
        return "字符 %d，行 %d（空行 %d），词 %d" % (
            info["chars"],
            info["lines"],
            info["blank"],
            info["words"],
        )

    registry.register(
        Tool(
            name="text_process",
            description="整理一段文本：清掉行尾空白和多余空行、去掉重复行、或统计字数行数。纯规则，可复算。",
            schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要整理的正文"},
                    "what": {
                        "type": "string",
                        "enum": list(TEXT_ACTIONS),
                        "description": "clean 清理、dedupe 去重、stats 统计",
                    },
                },
                "required": ["text"],
                "additionalProperties": False,
            },
            permission="READ",
            risk="只读本地文本",
            readonly=True,
            handler=text_process_handler,
            keywords=("整理文本", "清洗文本", "去重", "重复行", "字数", "统计字数", "空行"),
        )
    )

    # ---------- 任务待续 ----------
    def unfinished_handler(limit=10):
        if task_store is None:
            raise ToolError("这一版没接上任务记录，读不到没做完的任务")
        try:
            rows = task_store.unfinished(limit=max(1, int(limit or 10)))
        except Exception as exc:
            raise ToolError(str(exc))
        if not rows:
            return "没有没做完的任务，都收尾了。"
        lines = ["有 %d 个没做完的任务：" % len(rows)]
        for record in rows[:5]:
            total = len(getattr(record, "plan", ()) or ())
            place = "第 %d/%d 步" % (int(record.current_step or 0), total) if total else "还没排步骤"
            lines.append(
                "- %s（%s，%s）"
                % (getattr(record, "goal", "") or "没写目标", record.state_label, place)
            )
        lines.append("要看哪一条的详情就说，我不会自己重跑。")
        return "\n".join(lines)

    registry.register(
        Tool(
            name="task_unfinished",
            description="列出没做完的任务，重启后也还在。只列不跑重活。",
            schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "最多列几条"}},
                "required": [],
                "additionalProperties": False,
            },
            permission="READ",
            risk="只读任务记录",
            readonly=True,
            handler=unfinished_handler,
            summarize=_as_text,
            keywords=("没做完的任务", "待续任务", "上次的任务", "任务记录", "接着做"),
        )
    )

    # ---------- 自检 ----------
    def diagnostics_handler():
        rows = []
        program = getattr(config, "program_dir", "") if config is not None else ""
        data = getattr(config, "data_dir", "") if config is not None else ""
        logs = getattr(config, "log_dir", "") if config is not None else ""
        for label, path in (("程序目录", program), ("数据目录", data), ("日志目录", logs)):
            if not path:
                rows.append((label, "没读到"))
            elif not Path(path).is_dir():
                rows.append((label, "还没有这个目录（%s）" % path))
            else:
                rows.append((label, "在，%s（%s）" % ("能写" if writable_dir(path) else "只能读", path)))
        path = _db_path()
        if not path:
            rows.append(("数据库", "这一版没接上"))
        elif database is None:
            rows.append(("数据库", "路径是 %s，但没接上连接" % path))
        else:
            try:
                info = database.health()
            except Exception as exc:
                rows.append(("数据库", "打不开：%s" % exc))
            else:
                rows.append(
                    ("数据库", "能打开，%d 张表，SQLite %s"
                     % (len(info.get("tables") or []), info.get("sqlite_version") or "?"))
                )
        if config is None:
            rows.append(("模型", "没读到配置"))
        else:
            companion = "配好了" if config.has_companion() else "没配"
            task = "配好了" if config.has_task() else "没配"
            rows.append(("模型", "陪伴模型%s，任务模型%s；没配也照常用本地功能" % (companion, task)))
        wanted = ("webview", "ollama")
        found = [name for name in wanted if shutil.which(name)]
        rows.append(("可选组件", "装了：" + "、".join(found) if found else "可选组件都没装，不影响本地功能"))
        count = None
        if callable(grants):
            try:
                count = len(list(grants() or []))
            except Exception:
                count = None
        if count is None:
            rows.append(("权限", "这一版读不到授权清单"))
        else:
            rows.append(("权限", "当前有 %d 条持久授权；高风险操作每次都要单独确认" % count))
        problems = log_problems(logs)
        if problems:
            rows.append(("日志", "最近有 %d 条像出问题，最上面一条：%s" % (len(problems), problems[-1][:160])))
        else:
            rows.append(("日志", "最近没有报错"))
        return "；".join("%s：%s" % (label, text) for label, text in rows)

    registry.register(
        Tool(
            name="run_diagnostics",
            description="给桌宠做一次体检：目录、数据库、模型配置、权限和最近日志错误，读到的都是本机真实状态。",
            schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            permission="READ",
            risk="只读本机状态",
            readonly=True,
            handler=diagnostics_handler,
            summarize=_as_text,
            keywords=("自检", "体检", "诊断", "检查一下", "有什么问题", "错误日志"),
            timeout=5.0,
        )
    )

    # ---------- 备份 ----------
    def backup_list_handler():
        target = _backup_dir()
        if target is None:
            raise ToolError("这一版读不到数据库位置")
        if not target.is_dir():
            return "还没有备份。要备份就说一声，我建一份。"
        rows = sorted(
            (item for item in target.glob("*.sqlite3") if item.is_file()),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )[:BACKUP_LIMIT]
        if not rows:
            return "还没有备份。要备份就说一声，我建一份。"
        lines = ["有 %d 份备份，最近的在上面：" % len(rows)]
        for item in rows:
            size = item.stat().st_size / 1024.0 / 1024.0
            lines.append("- %s（%.1f MB）" % (item.name, size))
        return "\n".join(lines)

    registry.register(
        Tool(
            name="backup_list",
            description="列出已有的数据库备份，含文件名和大小。只读，不动任何数据。",
            schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            permission="READ",
            risk="只读备份目录",
            readonly=True,
            handler=backup_list_handler,
            summarize=_as_text,
            keywords=("备份列表", "有哪些备份", "备份文件"),
        )
    )

    def backup_create_handler():
        path = _db_path()
        if not path:
            raise ToolError("这一版读不到数据库位置")
        result = memory_backup.backup_database(path)
        if not result.get("ok"):
            raise ToolError(result.get("error") or "备份没做成")
        return "备份好了：%s" % result.get("path")

    registry.register(
        Tool(
            name="backup_create",
            description="给记忆库建一份备份，放在数据库旁边的 backups 目录，不动现有数据。",
            schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            permission="LOW",
            risk="写一个备份文件，不改现有数据",
            handler=backup_create_handler,
            keywords=("备份", "备份数据", "存一份", "导出数据"),
            idempotent=False,
            timeout=30.0,
        )
    )

    def restore_handler(path="", confirm=False):
        target = _db_path()
        if not target:
            raise ToolError("这一版读不到数据库位置")
        if not path:
            raise ToolError("要恢复哪一份？把备份文件路径给我")
        if not confirm:
            raise ToolError("恢复会覆盖现在的记忆，要先确认才做")
        check = memory_backup.validate_backup(path)
        if not check.get("ok"):
            raise ToolError("这份备份不能用：%s" % check.get("error"))
        result = memory_backup.restore_database(target, path, confirm=True)
        if not result.get("ok"):
            raise ToolError(result.get("error") or "恢复没做成")
        safety = result.get("safety") or ""
        return "恢复好了：%s。覆盖前的那一份存在 %s" % (result.get("restored"), safety or "（原来没有库）")

    registry.register(
        Tool(
            name="restore_backup",
            description="用一份备份覆盖当前记忆库。会先校验备份、再给当前库留一份副本；必须确认后才执行。",
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "备份文件的完整路径"},
                    "confirm": {"type": "boolean", "description": "确认为 true 才真的覆盖"},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            permission="HIGH",
            risk="覆盖当前记忆库",
            handler=restore_handler,
            keywords=("恢复备份", "还原数据", "恢复数据"),
            idempotent=False,
            timeout=60.0,
        )
    )
