"""工作区工具：读文件、列目录、搜代码、写文件、跑测试、看 Git。

模型只能请求这些工具；真正执行由执行层完成，权限与确认照旧。
所有路径必须在工作区内，跑出去一律拒绝。命令不做任意 shell，只认白名单的测试跑法。
"""

import os
import re
import shlex
import shutil
import subprocess

from tools.registry import Tool, ToolError

# ---------- 输出压缩（可选） ----------
# 默认没有压缩器。装了可选压缩器才接上来，且只影响给模型看的那份文本。
# 原始输出一直留着，压缩发生时会另存一份完整文件并给出路径。
OUTPUT_REDUCER = None


def set_output_reducer(reducer):
    """接一个输出压缩器；传 None 就关掉。"""
    global OUTPUT_REDUCER
    OUTPUT_REDUCER = reducer
    return reducer


def _save_full_output(command, text):
    """把完整输出另存一份，返回路径；存不了就返回空串。"""
    try:
        from security.config import data_root
        from pathlib import Path
        import time

        folder = Path(data_root()) / "outputs"
        folder.mkdir(parents=True, exist_ok=True)
        name = time.strftime("%Y%m%d-%H%M%S") + "-" + str(abs(hash(command)) % 100000) + ".log"
        path = folder / name
        path.write_text(str(text), encoding="utf-8")
        return str(path)
    except Exception:
        return ""


def _reduced_view(command, output):
    """返回 (给模型看的文本, 说明)。没有压缩器或没压出差别就原样返回。"""
    reducer = OUTPUT_REDUCER
    if reducer is None or not hasattr(reducer, "reduce"):
        return output, "", ""
    try:
        result = reducer.reduce(command, output)
    except Exception as exc:
        return output, "压缩器出错了，用完整输出：%s" % exc, ""
    if not result.get("changed"):
        return output, "", ""
    text = str(result.get("text") or "")
    if not text:
        return output, "", ""
    keep = _save_full_output(command, output)
    note = "输出已压缩：%s → %s 字节（报错与拒绝行都保留）" % (
        result.get("before_bytes"),
        result.get("after_bytes"),
    )
    if keep:
        note = note + "；完整输出：%s" % keep
    return text, note, keep


MAX_ENTRIES = 200
MAX_READ_CHARS = 8000
MAX_MATCHES = 40
MAX_OUTPUT_CHARS = 4000
TEXT_SUFFIXES = (
    ".py", ".md", ".txt", ".json", ".yml", ".yaml", ".toml", ".cfg", ".ini",
    ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".c", ".h", ".cpp", ".hpp",
    ".cs", ".java", ".go", ".rs", ".sql", ".bat", ".ps1", ".sh", ".in",
)
SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", "dist", "build", ".venv", "venv",
    ".idea", ".vscode", ".mypy_cache", ".pytest_cache", "logs",
}
TEST_RUNNERS = {"unittest", "pytest", "compileall", "timeit"}
# V5 第四阶段：通用白名单命令。git 不在这里，写操作仍然只走 git_action。
ALLOWED_COMMANDS = (
    "python", "python3", "py", "python.exe", "pytest",
    "node", "cargo", "go", "gcc", "g++", "clang", "make",
)
# 这些工具授权后就能读写的工具名。持久授权的目录范围只对它们生效。
FILE_TOOL_NAMES = (
    "list_dir", "read_file", "search_files", "write_file", "read_document",
    "copy_file", "run_command", "run_tests",
)
BLOCKED_COMMAND_TOKENS = (
    "rm", "del", "format", "mkfs", "shutdown", "reg", "diskpart", "taskkill",
    "powershell", "cmd", "curl", "wget", "ssh", "scp", "netsh",
)
GIT_READ_SUBCOMMANDS = (
    "status", "diff", "log", "show", "branch", "rev-parse", "ls-files",
    "describe", "shortlog", "blame", "remote", "tag", "stash",
)
# 只读的 Git 也会被参数变成写操作：分支删除、标签删除、stash pop 都算写。
GIT_DESTRUCTIVE_ARGS = {
    "branch": ("-d", "-D", "-m", "-M", "--delete", "--move", "--edit-description", "--set-upstream-to"),
    "tag": ("-d", "-D", "--delete", "-a", "--annotate", "-f", "--force"),
    "stash": ("pop", "apply", "drop", "clear", "save", "push", "create", "branch", "store"),
    "remote": ("add", "remove", "rm", "rename", "set-url", "prune", "update"),
}
GIT_WRITE_ACTIONS = {
    "add": ["add"],
    "commit": ["commit"],
    "checkout": ["checkout"],
    "switch": ["switch"],
    "branch": ["branch"],
    "reset": ["reset"],
    "restore": ["restore"],
    "stash": ["stash"],
    "tag": ["tag"],
    "merge": ["merge"],
    "revert": ["revert"],
    "init": ["init"],
}
GIT_REFUSED = ("push", "clean", "gc", "filter-branch", "remote")


DOC_SUFFIXES = (".docx", ".xlsx", ".pptx", ".pdf")
# V9.1 第三阶段：装了 MarkItDown 之后这些格式也能读；自带的解析不认识它们。
EXTRA_DOC_SUFFIXES = (".html", ".htm", ".epub", ".rtf", ".ipynb")
_XML_TAG = re.compile(r"<[^>]+>")
_PDF_STREAM = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)


def _xml_text(blob):
    """从 Office 的 XML 里抠正文。用标准库，不装额外依赖。"""
    try:
        raw = blob.decode("utf-8", errors="replace")
    except Exception:
        return ""
    raw = re.sub(r"<(w:p|a:p|row)[ >]", "\n<\\1 ", raw)
    return re.sub(r"\s+\n", "\n", _XML_TAG.sub("", raw)).strip()


def _zip_part_text(path, pattern, limit=200000):
    import zipfile

    try:
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if re.match(pattern, name)]
            chunks = []
            total = 0
            for name in sorted(names):
                blob = archive.read(name)
                total += len(blob)
                if total > limit:
                    break
                chunks.append(_xml_text(blob))
            return "\n".join(part for part in chunks if part)
    except Exception:
        return ""


def extract_document_text(path, max_chars=6000):
    """按扩展名抽正文。抽不出来就返回空串，不猜。"""
    return extract_document(path, max_chars=max_chars)["text"]


def extract_document(path, max_chars=6000):
    """抽正文，并说清是哪一档读出来的。

    容器格式（docx/xlsx/pptx/pdf）优先用 MarkItDown，装不上就退回自带的朴素解析；
    纯文本直接读；只有 MarkItDown 认识的格式，没装就如实说读不了。
    """
    suffix = os.path.splitext(str(path))[1].lower()
    limit = int(max_chars or 6000)
    engine = _document_engine()
    if suffix in DOC_SUFFIXES:
        if engine is not None and not engine.too_big(path):
            text = engine.to_markdown(path, limit)
            if text.strip():
                return {"text": text[:limit], "engine": "markitdown", "format": suffix.lstrip("."), "error": ""}
        # 第二档：MarkItDown 没装或没读出来，用包里本来就有的小库兜底。
        text, fallback = _document_fallback(path, limit)
        if text.strip():
            return {"text": text[:limit], "engine": fallback, "format": suffix.lstrip("."), "error": ""}
    if suffix in EXTRA_DOC_SUFFIXES:
        if engine is None:
            return {"text": "", "engine": "", "format": suffix.lstrip("."), "error": "这个格式要装 MarkItDown 才能读"}
        text = engine.to_markdown(path, limit)
        if text.strip():
            return {"text": text[:limit], "engine": "markitdown", "format": suffix.lstrip("."), "error": ""}
        return {"text": "", "engine": "", "format": suffix.lstrip("."), "error": "MarkItDown 也没读出正文，可能是空文件或加密文件"}
    text = _builtin_document_text(path, limit)
    if text.strip():
        return {"text": text[:limit], "engine": "builtin", "format": suffix.lstrip("."), "error": ""}
    return {"text": "", "engine": "", "format": suffix.lstrip("."), "error": ""}


def _document_engine():
    """MarkItDown 适配层。没装就返回 None，不影响任何功能。"""
    try:
        from adapters import documents

        return documents if documents.available() else None
    except Exception:
        return None


def _document_fallback(path, max_chars=6000):
    """第二档兜底：openpyxl 读表格、pdfminer 读 PDF。返回 (文本, 引擎名)。"""
    try:
        from adapters import documents

        return documents.local_fallback(path, max_chars)
    except Exception:
        return "", ""


def _builtin_document_text(path, max_chars=6000):
    """程序自带的朴素解析：纯文本直接读，容器格式掏 XML，不依赖任何第三方包。"""
    suffix = os.path.splitext(str(path))[1].lower()
    text = ""
    if suffix in (".md", ".txt", ".csv", ".json", ".rst", ".log"):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read(int(max_chars) * 2)
        except OSError:
            return ""
    elif suffix == ".docx":
        text = _zip_part_text(path, r"word/document.*\.xml$")
    elif suffix == ".pptx":
        text = _zip_part_text(path, r"ppt/slides/slide\d+\.xml$")
    elif suffix == ".xlsx":
        text = _zip_part_text(path, r"xl/sharedStrings\.xml$")
    elif suffix == ".pdf":
        text = _pdf_text(path)
    return text[: int(max_chars or 6000)]


def _pdf_text(path, max_chars=6000):
    """PDF 只做最朴素的文本流尝试。抽不出就说抽不出。"""
    import zlib

    try:
        with open(path, "rb") as handle:
            blob = handle.read(4 * 1024 * 1024)
    except OSError:
        return ""
    chunks = []
    for match in _PDF_STREAM.finditer(blob):
        payload = match.group(1)
        data = payload
        try:
            data = zlib.decompress(payload)
        except Exception:
            data = payload
        found = re.findall(rb"\((?:[^()\\]|\\.)*\)", data)
        for item in found[:200]:
            chunks.append(item[1:-1].decode("latin-1", errors="replace"))
    return " ".join(chunk for chunk in chunks if chunk.strip())


def read_document_handler(root):
    def handler(path, max_chars=6000):
        target = guard(root, path)
        if not os.path.isfile(target):
            raise ToolError("找不到文件：%s" % path)
        suffix = os.path.splitext(target)[1].lower()
        known = suffix in DOC_SUFFIXES or suffix in EXTRA_DOC_SUFFIXES
        if not known and not _looks_text(target):
            raise ToolError("这个格式还不支持：%s" % (suffix or "未知"))
        found = extract_document(target, max_chars=max_chars)
        text = found["text"]
        if not text.strip():
            raise ToolError(
                found["error"]
                or "没能从这个文件里读出正文（%s）。格式可能不支持，或者内容是扫描件。"
                % (suffix or "未知")
            )
        note = found["engine"] or "text"
        if found["engine"] == "builtin" and suffix in DOC_SUFFIXES:
            note = "builtin（装了 MarkItDown 能读得更整齐）"
        return {
            "path": _display(target, root),
            "content": text,
            "chars": len(text),
            "format": found["format"] or "text",
            "engine": note,
        }

    return handler


def read_document_summary(data):
    return "读了 %s（%s，%d 字）。" % (data.get("path"), data.get("format"), data.get("chars", 0))


def workspace_root(config, fallback=None):
    for attr in ("workspace_root", "project_root", "data_root"):
        value = getattr(config, attr, None) if config is not None else None
        if value:
            return os.path.realpath(str(value))
    return os.path.realpath(str(fallback or os.getcwd()))


def authorized_roots(grants):
    """从持久授权里取出用户明确放行的目录。

    只认「持久授权 + 路径范围 + 文件类工具」，单次授权和拒绝都不算数。
    """
    roots = []
    for item in grants or []:
        try:
            if str(item.get("mode") or "").lower() != "always":
                continue
            if str(item.get("tool") or "") not in FILE_TOOL_NAMES:
                continue
            scope = str(item.get("scope") or "")
            if not scope.startswith("path:"):
                continue
            value = scope[len("path:"):].strip()
            if value:
                roots.append(value)
        except Exception:
            continue
    return roots


def _resolve_root(root):
    """root 可能是目录，也可能是一个「现在有哪些授权目录」的函数。"""
    return root() if callable(root) else root


def _roots_list(root):
    value = _resolve_root(root)
    items = value if isinstance(value, (list, tuple, set)) else [value]
    return [os.path.realpath(str(item)) for item in items if str(item or "").strip()]


def _first_root(root):
    roots = _roots_list(root)
    return roots[0] if roots else os.path.realpath(os.getcwd())


def guard(root, target):
    """把相对路径挂到第一个根目录上，并确保结果落在允许的目录里。

    root 可以是单个目录，也可以是多个已授权目录。跑出所有根目录一律拒绝。
    """
    roots = _roots_list(root) or [os.path.realpath(os.getcwd())]
    base = roots[0]
    raw = str(target or "").strip()
    path = raw if os.path.isabs(raw) else os.path.join(base, raw)
    resolved = os.path.realpath(path)
    for item in roots:
        if resolved == item or resolved.startswith(item + os.sep):
            return resolved
    raise ToolError("只能操作工作区或已授权目录内的路径：%s" % target)


def _display(target, root):
    """把绝对路径说成相对某个根目录的路径，界面和模型才读得懂。"""
    resolved = os.path.realpath(str(target))
    for item in _roots_list(root):
        if resolved == item:
            return "."
        if resolved.startswith(item + os.sep):
            return os.path.relpath(resolved, item)
    return resolved


def _looks_text(path):
    if os.path.splitext(path)[1].lower() in TEXT_SUFFIXES:
        return True
    return False


def _size_text(size):
    if size < 1024:
        return "%d B" % size
    if size < 1024 * 1024:
        return "%.1f KB" % (size / 1024.0)
    return "%.1f MB" % (size / (1024.0 * 1024.0))


def list_dir_handler(root):
    def handler(path="", limit=MAX_ENTRIES):
        target = guard(root, path or ".")
        if not os.path.isdir(target):
            raise ToolError("不是一个目录：%s" % path)
        entries = []
        try:
            names = sorted(os.listdir(target))
        except OSError as exc:
            raise ToolError("读不了这个目录：%s" % exc)
        for name in names:
            if name in SKIP_DIRS:
                continue
            full = os.path.join(target, name)
            if os.path.isdir(full):
                entries.append({"name": name, "type": "dir"})
            elif os.path.isfile(full):
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = 0
                entries.append({"name": name, "type": "file", "size": size})
            if len(entries) >= int(limit or MAX_ENTRIES):
                break
        return {"path": _display(target, root) or ".", "entries": entries}

    return handler


def list_dir_summary(data):
    entries = data.get("entries") or []
    if not entries:
        return "这个目录是空的。"
    dirs = [item["name"] for item in entries if item.get("type") == "dir"]
    files = [item["name"] for item in entries if item.get("type") == "file"]
    parts = []
    if dirs:
        parts.append("目录：" + "、".join(dirs[:8]))
    if files:
        parts.append("文件：" + "、".join(files[:8]))
    return "%s 里有 %d 项。" % (data.get("path") or ".", len(entries)) + "；".join(parts)


def read_file_handler(root):
    def handler(path, max_chars=MAX_READ_CHARS):
        target = guard(root, path)
        if not os.path.isfile(target):
            raise ToolError("找不到文件：%s" % path)
        if not _looks_text(target):
            raise ToolError("这个类型先不读，避免把二进制塞进上下文：%s" % os.path.basename(target))
        try:
            with open(target, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read(int(max_chars or MAX_READ_CHARS) + 1)
        except OSError as exc:
            raise ToolError("读不了这个文件：%s" % exc)
        truncated = len(text) > int(max_chars or MAX_READ_CHARS)
        return {
            "path": _display(target, root),
            "content": text[: int(max_chars or MAX_READ_CHARS)],
            "truncated": truncated,
            "lines": text.count("\n") + 1,
        }

    return handler


def read_file_summary(data):
    return "读了 %s（%d 行%s）。" % (
        data.get("path"),
        data.get("lines", 0),
        "，已截断" if data.get("truncated") else "",
    )


def search_files_handler(root):
    def handler(pattern, path="", limit=MAX_MATCHES):
        text_pattern = str(pattern or "").strip()
        if not text_pattern:
            raise ToolError("要给一个查找内容")
        try:
            regex = re.compile(text_pattern)
        except re.error as exc:
            raise ToolError("这不是合法的表达式：%s" % exc)
        base = guard(root, path or ".")
        matches = []
        scanned = 0
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
            for name in sorted(filenames):
                if not _looks_text(name):
                    continue
                full = os.path.join(dirpath, name)
                scanned += 1
                try:
                    with open(full, "r", encoding="utf-8", errors="ignore") as handle:
                        for number, line in enumerate(handle, start=1):
                            if regex.search(line):
                                matches.append(
                                    {
                                        "file": _display(full, root),
                                        "line": number,
                                        "text": line.strip()[:200],
                                    }
                                )
                                if len(matches) >= int(limit or MAX_MATCHES):
                                    return {"pattern": text_pattern, "matches": matches, "scanned": scanned, "truncated": True}
                except OSError:
                    continue
        return {"pattern": text_pattern, "matches": matches, "scanned": scanned, "truncated": False}

    return handler


def search_summary(data):
    matches = data.get("matches") or []
    if not matches:
        return "在已扫描的文本文件里没找到 %s。" % data.get("pattern")
    files = sorted({item["file"] for item in matches})
    return "找到 %d 处，分布在 %d 个文件里：%s" % (
        len(matches), len(files), "、".join(files[:5])
    )


def write_file_handler(root):
    def handler(path, content, overwrite=False):
        target = guard(root, path)
        exists = os.path.isfile(target)
        if exists and not overwrite:
            raise ToolError("目标已存在，要覆盖必须显式说明：%s" % _display(target, root))
        parent = os.path.dirname(target)
        if parent and not os.path.isdir(parent):
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as exc:
                raise ToolError("建不了目录：%s" % exc)
        data = str(content if content is not None else "")
        try:
            with open(target, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(data)
        except OSError as exc:
            raise ToolError("写不了这个文件：%s" % exc)
        return {
            "path": _display(target, root),
            "chars": len(data),
            "created": not exists,
        }

    return handler


def write_verify(root):
    def verify(done):
        if not isinstance(done, dict) or not done.get("path"):
            return None
        target = guard(root, done["path"])
        if not os.path.isfile(target):
            return None
        try:
            with open(target, "r", encoding="utf-8", errors="replace") as handle:
                chars = len(handle.read())
        except OSError:
            return None
        if chars != int(done.get("chars", -1)):
            return None
        return "已复核：%s 写进去了，%d 字" % (done["path"], chars)

    return verify


def write_summary(data):
    action = "新建" if data.get("created") else "覆盖"
    return "%s了 %s（%d 字）。" % (action, data.get("path"), data.get("chars", 0))


def copy_file_handler(root):
    def handler(source, target, overwrite=False):
        src = guard(root, source)
        dst = guard(root, target)
        if not os.path.isfile(src):
            raise ToolError("找不到要复制的文件：%s" % source)
        if os.path.exists(dst) and not overwrite:
            raise ToolError("目标已存在，要覆盖必须显式说明：%s" % _display(dst, root))
        parent = os.path.dirname(dst)
        if parent and not os.path.isdir(parent):
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as exc:
                raise ToolError("建不了目录：%s" % exc)
        try:
            shutil.copy2(src, dst)
        except OSError as exc:
            raise ToolError("复制失败：%s" % exc)
        return {"source": _display(src, root), "target": _display(dst, root)}

    return handler


def copy_file_summary(data):
    return "复制了 %s 到 %s。" % (data.get("source"), data.get("target"))


def run_command_handler(root, allowed=None):
    """白名单命令。只认几个开发工具，-c 直接执行代码一律拒绝。"""
    names = set(allowed or ALLOWED_COMMANDS)

    def handler(command, cwd="", timeout=120):
        raw = str(command or "").strip()
        if not raw:
            raise ToolError("要给一条命令")
        try:
            argv = [
                token.strip('"')
                for token in shlex.split(raw, posix=False)
                if token.strip('"')
            ]
        except ValueError as exc:
            raise ToolError("命令解析失败：%s" % exc)
        if not argv:
            raise ToolError("命令是空的")
        program = os.path.basename(argv[0]).lower()
        if program not in names:
            raise ToolError("这条命令不在白名单里：%s" % argv[0])
        lowered = [token.lower() for token in argv[1:]]
        if "-c" in lowered or "/c" in lowered:
            raise ToolError("不允许用 -c 直接执行代码")
        for token in lowered:
            if token in BLOCKED_COMMAND_TOKENS:
                raise ToolError("命令里出现不允许的指令：%s" % token)
        if program.startswith("py") and "-m" in lowered:
            index = lowered.index("-m")
            module = lowered[index + 1] if index + 1 < len(lowered) else ""
            if module not in TEST_RUNNERS:
                raise ToolError("只认这些模块入口：%s" % ", ".join(sorted(TEST_RUNNERS)))
        workdir = guard(root, cwd or ".")
        if not os.path.isdir(workdir):
            raise ToolError("工作目录不存在：%s" % cwd)
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            completed = subprocess.run(
                argv, cwd=workdir, capture_output=True, env=env, text=True,
                encoding="utf-8", errors="replace",
                timeout=max(5, min(300, int(timeout or 120))),
            )
        except subprocess.TimeoutExpired:
            raise ToolError("命令超时，先停下")
        except OSError as exc:
            raise ToolError("跑不起来：%s" % exc)
        output = (completed.stdout or "") + (completed.stderr or "")
        output = output[-MAX_OUTPUT_CHARS:]
        shown, note, keep = _reduced_view(raw, output)
        return {
            "command": raw,
            "cwd": _display(workdir, root),
            "returncode": int(completed.returncode),
            "output": shown,
            "output_note": note,
            "output_raw": output,
            "output_path": keep,
            "passed": completed.returncode == 0,
        }

    return handler


def run_command_summary(data):
    status = "通过" if data.get("passed") else "退出码 %s" % data.get("returncode")
    note = str(data.get("output_note") or "")
    return "命令跑完（%s）。%s" % (status, note)


def run_tests_handler(root):
    def handler(command="python -m unittest discover -s tests", cwd=""):
        raw = str(command or "").strip()
        if not raw:
            raise ToolError("要给一条测试命令")
        try:
            argv = shlex.split(raw, posix=False)
        except ValueError as exc:
            raise ToolError("命令解析失败：%s" % exc)
        argv = [token.strip('"') for token in argv if token.strip('"')]
        if not argv:
            raise ToolError("命令是空的")
        program = os.path.basename(argv[0]).lower()
        if program not in {"python", "python3", "py", "python.exe"}:
            raise ToolError("这个工具只跑测试，不执行任意命令：%s" % argv[0])
        lowered = [token.lower() for token in argv]
        if "-m" in lowered:
            module = lowered[lowered.index("-m") + 1] if lowered.index("-m") + 1 < len(lowered) else ""
            if module not in TEST_RUNNERS:
                raise ToolError("只认这些测试入口：%s" % ", ".join(sorted(TEST_RUNNERS)))
        elif not any(token.endswith((".py",)) for token in argv[1:]):
            raise ToolError("只跑测试：请用 python -m unittest / pytest / compileall，或指定一个测试脚本")
        for token in lowered[1:]:
            if token in BLOCKED_COMMAND_TOKENS:
                raise ToolError("命令里出现不允许的指令：%s" % token)
        workdir = guard(root, cwd or ".")
        if not os.path.isdir(workdir):
            raise ToolError("工作目录不存在：%s" % cwd)
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONPATH"] = _first_root(root)
        try:
            completed = subprocess.run(
                argv, cwd=workdir, capture_output=True, timeout=300, env=env, text=True,
                encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            raise ToolError("测试超过 300 秒，先停下")
        except OSError as exc:
            raise ToolError("跑不起来：%s" % exc)
        output = (completed.stdout or "") + (completed.stderr or "")
        output = output[-MAX_OUTPUT_CHARS:]
        shown, note, keep = _reduced_view(raw, output)
        return {
            "command": raw,
            "returncode": int(completed.returncode),
            "output": shown,
            "output_note": note,
            "output_raw": output,
            "output_path": keep,
            "passed": completed.returncode == 0,
        }

    return handler


def run_tests_summary(data):
    status = "通过" if data.get("passed") else "没通过"
    note = str(data.get("output_note") or "")
    return "测试%s（退出码 %s）。%s" % (status, data.get("returncode"), note)


def run_tests_verify(done):
    if not isinstance(done, dict):
        return None
    return "已复核：退出码 %s" % done.get("returncode")


def git_read_handler(root):
    def handler(subcommand="status", args=""):
        name = str(subcommand or "status").strip().lower()
        if name not in GIT_READ_SUBCOMMANDS:
            raise ToolError("只读这些 Git 子命令：%s" % ", ".join(GIT_READ_SUBCOMMANDS))
        extra = shlex.split(str(args or ""), posix=False)
        extra = [token.strip('"') for token in extra]
        allowed_bad = GIT_DESTRUCTIVE_ARGS.get(name, ())
        for token in extra:
            if token in BLOCKED_COMMAND_TOKENS:
                raise ToolError("参数里出现不允许的内容：%s" % token)
            if token in allowed_bad:
                raise ToolError("%s %s 会改动仓库，只读查询不允许带这个参数" % (name, token))
            if token.startswith("--output"):
                raise ToolError("只读查询不允许写文件")
        argv = ["git", name, *extra]
        try:
            completed = subprocess.run(
                argv, cwd=_first_root(root), capture_output=True, timeout=60, text=True,
                encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            raise ToolError("Git 命令超时")
        except OSError as exc:
            raise ToolError("跑不了 Git：%s" % exc)
        output = ((completed.stdout or "") + (completed.stderr or "")).strip()
        return {
            "command": " ".join(argv),
            "returncode": int(completed.returncode),
            "output": output[:MAX_OUTPUT_CHARS],
        }

    return handler


def git_read_summary(data):
    if data.get("returncode") != 0:
        return "Git 报错（退出码 %s）：%s" % (data.get("returncode"), (data.get("output") or "")[:120])
    first = (data.get("output") or "").strip().splitlines()
    return "git %s 有 %d 行输出。" % (data.get("command"), len(first))


def git_action_handler(root, config=None):
    def handler(action, args=""):
        name = str(action or "").strip().lower()
        if name in GIT_REFUSED:
            raise ToolError(
                "%s 会改远端或清工作区，属于禁止等级，当前不执行。要推送请自己确认后手动做。" % name
            )
        if name not in GIT_WRITE_ACTIONS:
            raise ToolError("只认这些写操作：%s" % ", ".join(sorted(GIT_WRITE_ACTIONS)))
        extra = [token.strip('"') for token in shlex.split(str(args or ""), posix=False)]
        for token in extra:
            if token in BLOCKED_COMMAND_TOKENS:
                raise ToolError("参数里出现不允许的内容：%s" % token)
        argv = ["git", *GIT_WRITE_ACTIONS[name], *extra]
        try:
            completed = subprocess.run(
                argv, cwd=_first_root(root), capture_output=True, timeout=60, text=True,
                encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            raise ToolError("Git 命令超时")
        except OSError as exc:
            raise ToolError("跑不了 Git：%s" % exc)
        output = ((completed.stdout or "") + (completed.stderr or "")).strip()
        return {
            "command": " ".join(argv),
            "returncode": int(completed.returncode),
            "output": output[:MAX_OUTPUT_CHARS],
        }

    return handler


def git_action_verify(done):
    if not isinstance(done, dict):
        return None
    if done.get("returncode") != 0:
        return None
    return "已复核：%s 退出码 0" % done.get("command")


def workspace_info_tool(root):
    def handler():
        base = _first_root(root)
        exists = os.path.isdir(base)
        return {
            "workspace": base,
            "exists": exists,
            "is_git": os.path.isdir(os.path.join(base, ".git")),
            "entries": len(os.listdir(base)) if exists else 0,
        }

    return handler


def workspace_info_summary(data):
    if not data.get("exists"):
        return "工作区目录还不存在：%s" % data.get("workspace")
    return "工作区在 %s，共 %d 项%s。" % (
        data.get("workspace"),
        data.get("entries", 0),
        "（是 Git 仓库）" if data.get("is_git") else "",
    )


def register_workspace_tools(registry, config, workspace=None, extra_roots=None):
    """装配工作区工具。root 是个函数：工作区加上用户持久授权过的目录。

    路径一律过 guard，跑出所有根目录就拒绝。extra_roots 由上层给，通常是
    权限中心里持久授权的目录；读不到就只认工作区。
    """
    base = workspace_root(config, fallback=workspace)

    def root():
        values = [base]
        try:
            extra = extra_roots() if callable(extra_roots) else extra_roots
        except Exception:
            extra = None
        values.extend(extra or [])
        return values

    registry.register(
        Tool(
            name="list_dir",
            description="列出工作区里某个目录的内容",
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对工作区的目录，留空表示根目录"},
                    "limit": {"type": "integer", "description": "最多列几项"},
                },
                "required": [],
                "additionalProperties": False,
            },
            permission="READ",
            risk="只读目录列表",
            readonly=True,
            handler=list_dir_handler(root),
            summarize=list_dir_summary,
            keywords=("目录", "文件列表", "项目结构", "列一下", "list"),
        )
    )
    registry.register(
        Tool(
            name="read_file",
            description="读取工作区里的文本文件",
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对工作区的文件路径"},
                    "max_chars": {"type": "integer", "description": "最多读多少字"},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            permission="READ",
            risk="只读文件内容",
            readonly=True,
            handler=read_file_handler(root),
            summarize=read_file_summary,
            keywords=("读文件", "看看代码", "打开文件", "读一下", "read"),
        )
    )
    registry.register(
        Tool(
            name="search_files",
            description="在工作区的文本文件里按内容搜索",
            schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "要查找的内容，支持正则"},
                    "path": {"type": "string", "description": "从哪个目录开始找"},
                    "limit": {"type": "integer", "description": "最多返回多少条"},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
            permission="READ",
            risk="只读搜索",
            readonly=True,
            handler=search_files_handler(root),
            summarize=search_summary,
            keywords=("查找", "搜索", "调用链", "哪里用了", "search", "grep"),
        )
    )
    registry.register(
        Tool(
            name="write_file",
            description="在工作区里新建或覆盖一个文本文件",
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对工作区的文件路径"},
                    "content": {"type": "string", "description": "完整文件内容"},
                    "overwrite": {"type": "boolean", "description": "允许覆盖已有文件"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            permission="HIGH",
            risk="会改动工作区里的文件",
            idempotent=False,
            handler=write_file_handler(root),
            summarize=write_summary,
            verify=write_verify(root),
            keywords=("改文件", "写文件", "修改代码", "patch", "write"),
        )
    )
    registry.register(
        Tool(
            name="run_tests",
            description="在工作区里跑测试或编译检查，只认 unittest / pytest / compileall",
            schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "测试命令，例如 python -m unittest discover -s tests"},
                    "cwd": {"type": "string", "description": "相对工作区的运行目录"},
                },
                "required": [],
                "additionalProperties": False,
            },
            permission="HIGH",
            risk="会启动本机进程",
            idempotent=False,
            handler=run_tests_handler(root),
            summarize=run_tests_summary,
            verify=run_tests_verify,
            keywords=("跑测试", "运行测试", "编译检查", "unittest", "pytest"),
        )
    )
    registry.register(
        Tool(
            name="git_read",
            description="只读的 Git 查询：状态、差异、提交历史",
            schema={
                "type": "object",
                "properties": {
                    "subcommand": {"type": "string", "description": "status / diff / log / show / branch 等"},
                    "args": {"type": "string", "description": "附加参数，例如 --stat"},
                },
                "required": [],
                "additionalProperties": False,
            },
            permission="READ",
            risk="只读 Git 状态",
            readonly=True,
            handler=git_read_handler(root),
            summarize=git_read_summary,
            keywords=("git", "差异", "提交历史", "状态", "diff", "review"),
        )
    )
    registry.register(
        Tool(
            name="git_action",
            description="改本地 Git 状态：add / commit / checkout / branch / reset 等，推送和清工作区被禁止",
            schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "add / commit / checkout / branch / reset / restore / stash 等"},
                    "args": {"type": "string", "description": "附加参数，例如 -m 提交说明"},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
            permission="HIGH",
            risk="会改动本地仓库状态",
            idempotent=False,
            handler=git_action_handler(root, config),
            verify=git_action_verify,
            keywords=("提交", "commit", "建分支", "回退", "git"),
        )
    )
    registry.register(
        Tool(
            name="read_document",
            description="读取文档正文：md/txt/csv/json，以及 docx/xlsx/pptx/pdf；装了 MarkItDown 还能读 html/epub/ipynb 等",
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对工作区的文件路径"},
                    "max_chars": {"type": "integer", "description": "最多读多少字"},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            permission="READ",
            risk="只读文档正文",
            readonly=True,
            handler=read_document_handler(root),
            summarize=read_document_summary,
            keywords=("文档", "word", "docx", "excel", "xlsx", "ppt", "pptx", "pdf", "读表格"),
        )
    )
    registry.register(
        Tool(
            name="workspace_info",
            description="查看当前工作区在哪里、是不是 Git 仓库",
            schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            permission="READ",
            risk="只读工作区信息",
            readonly=True,
            handler=workspace_info_tool(root),
            summarize=workspace_info_summary,
            keywords=("工作区", "项目位置", "workspace"),
        )
    )
    registry.register(
        Tool(
            name="copy_file",
            description="复制一个文件到别处，不删原文件；目标是已授权目录里的路径",
            schema={
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "来源文件"},
                    "target": {"type": "string", "description": "目标路径"},
                    "overwrite": {"type": "boolean", "description": "允许覆盖已有文件"},
                },
                "required": ["source", "target"],
                "additionalProperties": False,
            },
            permission="HIGH",
            risk="会新增文件，可能覆盖目标",
            idempotent=False,
            handler=copy_file_handler(root),
            summarize=copy_file_summary,
            keywords=("复制", "拷贝", "copy"),
        )
    )
    registry.register(
        Tool(
            name="run_command",
            description="在授权目录里跑一条白名单命令，例如 python -m pytest、node、cargo",
            schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要跑的命令"},
                    "cwd": {"type": "string", "description": "运行目录，默认工作区"},
                    "timeout": {"type": "integer", "description": "超时秒数，默认 120"},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            permission="HIGH",
            risk="会启动本机进程，命令和工作目录都过权限校验",
            idempotent=False,
            handler=run_command_handler(root),
            summarize=run_command_summary,
            keywords=("跑命令", "执行命令", "node", "cargo", "gcc", "make"),
        )
    )
    return registry
