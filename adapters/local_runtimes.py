"""本地运行时探测：这些组件装没装、能不能用，如实报出来。

只查本机状态，不下载、不安装、不启动常驻服务。没有就明说没有，并写清
该去哪儿配、要多少空间、许可是什么。有没有这些组件都不影响其它功能。

探测到的每一项都属于「扩展」：装的动作要用户自己确认，程序不代劳。
"""

import importlib.util
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

PROBE_TIMEOUT = 4.0

# kind 用来说明这类组件补的是哪种能力，和 V9.1 的 Provider 分类对齐。
KIND_LABEL = {
    "local-model": "本地模型",
    "document": "文档解析",
    "voice": "离线语音",
    "vision": "屏幕理解",
    "search": "全文检索",
    "index": "数据库索引",
    "render": "角色画面",
}


def _module_available(name):
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


# Windows 上 main、control 这类泛用名字会被 system32 里的 .cpl / .scr 抢走，
# 它们是控制面板和屏保的替身，不是我们要找的命令。认下来会报出一个假的「装好了」。
SHIM_SUFFIXES = (".cpl", ".scr", ".msc")


def _which(*names):
    for name in names:
        try:
            found = shutil.which(name)
        except Exception:
            found = None
        if found and not str(found).lower().endswith(SHIM_SUFFIXES):
            return found
    return ""


def _run(args, timeout=PROBE_TIMEOUT):
    """跑一个只读的版本/列表命令。超时或不存在都当没读到，不抛异常。"""
    try:
        done = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except Exception:
        return ""
    if done.returncode != 0:
        return ""
    return (done.stdout or "").strip()


def _ollama_models():
    """只列本地已有的模型名字，不拉取任何东西。"""
    if not _which("ollama"):
        return []
    text = _run(["ollama", "list"])
    rows = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if parts:
            rows.append(parts[0])
    return rows[:20]


def fts5_available(db_path=""):
    """真建一张临时 FTS5 虚表试试，不能只看版本号。"""
    try:
        handle = sqlite3.connect(":memory:")
    except Exception:
        return False
    try:
        handle.execute("CREATE VIRTUAL TABLE probe USING fts5(body)")
    except Exception:
        return False
    finally:
        handle.close()
    return True

def _memory_db_file(db_path=""):
    """把「数据目录」或「库文件」统一成一个路径。不猜别的名字。"""
    raw = str(db_path or "").strip()
    if not raw:
        return ""
    if os.path.isdir(raw):
        return os.path.join(raw, "deskpet.sqlite3")
    return raw


def memory_index_state(db_path=""):
    """只看记忆全文索引建没建、里面多少条。只读打开，绝不建也不改。"""
    path = _memory_db_file(db_path)
    if not path or not os.path.isfile(path):
        return {"exists": False, "indexed": 0, "tokenizer": "", "reason": "还没有数据库文件"}
    handle = None
    try:
        uri = "file:%s?mode=ro" % path.replace("?", "%3f").replace("#", "%23")
        handle = sqlite3.connect(uri, uri=True, timeout=5.0)
        found = handle.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'memories_fts'"
        ).fetchone()
        if not found:
            return {
                "exists": False,
                "indexed": 0,
                "tokenizer": "",
                "reason": "还没建；第一次用到记忆检索时会按需建",
            }
        total = handle.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0]
        tokenizer = ""
        match = re.search(r"tokenize\s*=\s*'([^']+)'", str(found[0] or ""), re.I)
        if match:
            tokenizer = match.group(1).lower()
        return {"exists": True, "indexed": int(total or 0), "tokenizer": tokenizer, "reason": ""}
    except Exception as exc:
        return {"exists": False, "indexed": 0, "tokenizer": "", "reason": "读不出来：%s" % exc}
    finally:
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass


def _ollama_entry():
    cli = _which("ollama")
    client = _module_available("ollama")
    models = _ollama_models() if cli else []
    return {
        "id": "ollama",
        "kind": "local-model",
        "label": "Ollama（本地模型运行环境）",
        "installed": bool(cli or client),
        "ready": bool(cli and models),
        "detail": (
            "命令行在 %s" % cli if cli else "没找到 ollama 命令"
        )
        + ("" if not client else "；Python 客户端已装"),
        "models": models,
        "needs": "要跑本地模型得先自己装 Ollama 并手动拉一个模型；本程序不自动下载",
        "license": "Ollama 本体 MIT；各模型许可各不相同，拉之前自己看一眼",
    }


def _llama_cpp_entry():
    module = _module_available("llama_cpp")
    cli = _which("llama-cli", "llama-server")
    return {
        "id": "llama-cpp",
        "kind": "local-model",
        "label": "llama.cpp（本地模型运行环境）",
        "installed": bool(module or cli),
        "ready": bool(module or cli),
        "detail": ("Python 绑定已装" if module else "没找到 Python 绑定")
        + ("" if not cli else "；命令在 %s" % cli),
        "models": [],
        "needs": "需要自备 GGUF 模型文件；本程序不下载模型",
        "license": "llama.cpp MIT；模型许可看模型自己的说明",
    }


def _sherpa_entry(config=None):
    module = _module_available("sherpa_onnx")
    model_path = str(getattr(config, "stt_model", "") or "") if config else ""
    kind = str(getattr(config, "stt_kind", "auto") or "auto") if config else "auto"
    try:
        from voice.stt import streaming_model_status

        model_state = streaming_model_status(model_path, kind=kind)
    except Exception as exc:
        model_state = {
            "state": "probe_failed",
            "ready": False,
            "runtime_ready": bool(module),
            "model_ready": False,
            "note": "模型状态读不出来：%s" % exc,
        }
    ready = bool(module and model_state.get("ready"))
    if ready:
        detail = "运行库和在线识别模型都齐了；%s" % model_state.get("note")
    elif not module:
        detail = "sherpa-onnx 没装；%s" % model_state.get("note")
    else:
        detail = "运行库已装；%s" % model_state.get("note")
    return {
        "id": "sherpa-onnx",
        "kind": "voice",
        "label": "sherpa-onnx（离线语音识别与合成）",
        "installed": bool(module),
        "ready": ready,
        "detail": detail,
        "models": [],
        "model_state": model_state.get("state"),
        "needs": (
            "实时字幕要流式模型（tokens.txt 加 encoder/decoder，transducer 还要 joiner）；"
            "模型目录填到 DESKPET_STT_MODEL，或填到字幕设置里。模型不随包分发，也不会自动下载"
        ),
        "license": "sherpa-onnx Apache-2.0；语音模型许可见各自的模型卡",
    }


def _document_entries():
    markitdown = _module_available("markitdown")
    docling = _module_available("docling")
    return [
        {
            "id": "markitdown",
            "kind": "document",
            "label": "MarkItDown（常见文档转文本）",
            "installed": bool(markitdown),
            "ready": bool(markitdown),
            "detail": (
                "已装；read_document 读容器格式时优先用它"
                if markitdown
                else "没装；read_document 退回自带的朴素解析"
            ),
            "needs": "装上之后 docx、xlsx、pptx、pdf 读得更整齐，还能读 html、epub、ipynb",
            "license": "MIT",
        },
        {
            "id": "docling",
            "kind": "document",
            "label": "Docling（复杂 PDF 与表格）",
            "installed": bool(docling),
            "ready": bool(docling),
            "detail": "已装" if docling else "没装；扫描件和复杂排版目前读不了",
            "needs": "要连模型一起装，磁盘占用大，按需再装",
            "license": "MIT（依赖里可能有别的许可，装前看一遍）",
        },
    ]


def _vision_entries():
    pywinauto = _module_available("pywinauto")
    uiautomation = _module_available("uiautomation")
    # 我们的读取实现认这两个绑定；系统自带的 UIA 本身不用装。
    binding = "pywinauto" if pywinauto else ("uiautomation" if uiautomation else "")
    return [
        {
            "id": "pywinauto",
            "kind": "vision",
            "label": "pywinauto（读界面控件）",
            "installed": bool(pywinauto),
            "ready": bool(pywinauto),
            "detail": (
                "已装；界面语义（第二层）用它读，仍要用户打开感知才读"
                if pywinauto
                else "没装；现在只读得到前台窗口标题和进程名"
            ),
            "needs": "要用界面语义（第二层）才需要装",
            "license": "BSD-3-Clause",
        },
        {
            "id": "windows-uia",
            "kind": "vision",
            "label": "Windows UI Automation",
            # 系统自带，所以 installed 恒为真；ready 看的是「有没有可用的绑定」。
            "installed": True,
            "ready": bool(binding),
            "detail": (
                "系统自带，可用绑定是 %s，界面语义能读（仍要用户显式打开感知）" % binding
                if binding
                else "系统自带；没找到可用的 Python 绑定，装 pywinauto 或 uiautomation 才能读界面语义"
            ),
            "needs": "系统组件不用装；装了绑定之后，仍要用户显式打开感知才读",
            "license": "随 Windows 提供",
        },
    ]


# WebView2 运行时：pywebview 自己不带浏览器内核，Windows 上要用系统里的这一个。
# 官方安装器会把版本号写进下面这两个注册表位置；便携版（FixedVersionRuntime）
# 则是把运行时文件夹放在程序目录旁边。两种都要认，漏认一种就会冤枉用户"没装"。
WEBVIEW2_CLIENT = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_REGISTRY = (
    ("HKEY_LOCAL_MACHINE", r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"),
    ("HKEY_LOCAL_MACHINE", r"SOFTWARE\Microsoft\EdgeUpdate\Clients"),
    ("HKEY_CURRENT_USER", r"Software\Microsoft\EdgeUpdate\Clients"),
)
WEBVIEW2_EXE = "msedgewebview2.exe"


def _webview2_registry_version():
    """(查没查得成, 版本号)。查不成返回 (False, "")，不拿"查不到"冒充"没装"。"""
    if os.name != "nt":
        return False, ""
    try:
        import winreg
    except Exception:
        return False, ""
    for hive_name, key_path in WEBVIEW2_REGISTRY:
        hive = getattr(winreg, hive_name, None)
        if hive is None:
            continue
        try:
            with winreg.OpenKey(hive, key_path + "\\" + WEBVIEW2_CLIENT) as key:
                value, _kind = winreg.QueryValueEx(key, "pv")
        except Exception:
            continue
        if str(value or "").strip():
            return True, str(value).strip()
    return True, ""


def _webview2_folders():
    """可能放着 WebView2 运行时的目录：系统装的位置 + 程序自己旁边。"""
    roots = []
    for name in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        base = (os.environ.get(name) or "").strip()
        if base:
            roots.append(Path(base) / "Microsoft" / "EdgeWebView" / "Application")
    for anchor in (getattr(sys, "_MEIPASS", ""), os.path.dirname(sys.executable or "")):
        if anchor:
            roots.append(Path(anchor))
    return roots


def _webview2_files_present():
    """目录里有没有 msedgewebview2.exe。只看存在与否，不执行它。"""
    for base in _webview2_folders():
        try:
            if not base.is_dir():
                continue
            for child in base.iterdir():
                try:
                    if (child / WEBVIEW2_EXE).is_file():
                        return True
                    if not child.is_dir():
                        continue
                    # 系统装的是 Application\<版本号>\msedgewebview2.exe
                    for version_folder in child.iterdir():
                        if (version_folder / WEBVIEW2_EXE).is_file():
                            return True
                except Exception:
                    continue
        except Exception:
            continue
    return False


def webview2_state():
    """这台机器有没有 Edge WebView2 运行时。

    installed 为 True/False 是查清了的结论，None 表示查不出来（不是 Windows、
    读不了注册表）。查不出来时不许说"没装"：那会把能用的机器误判成不能用。
    """
    if os.name != "nt":
        return {"installed": None, "version": "", "reason": "只在 Windows 上检查"}
    checked, version = _webview2_registry_version()
    if version:
        return {"installed": True, "version": version, "reason": ""}
    if _webview2_files_present():
        return {"installed": True, "version": "", "reason": ""}
    if not checked:
        return {"installed": None, "version": "", "reason": "读不了注册表，查不出装没装"}
    return {"installed": False, "version": "", "reason": "没有找到 Edge WebView2 运行时"}


def _render_entries():
    """角色画面这一项：Live2D 靠 pywebview 加系统里的 WebView2 运行时开窗口。"""
    webview = _module_available("webview")
    state = webview2_state()
    if not webview:
        installed, ready = False, False
        detail = "没装 pywebview，角色窗口起不来；桌宠退回占位形象，聊天照旧"
    elif state["installed"] is False:
        installed, ready = False, False
        detail = "缺 Microsoft Edge WebView2 运行时（装一次即可，装完重启桌宠）"
    elif state["installed"] is None:
        installed, ready = True, True
        detail = "有 pywebview；查不出 WebView2 版本号，真起来时才知道能不能用"
    else:
        installed, ready = True, True
        detail = "有 pywebview，WebView2 运行时 %s" % (state["version"] or "已就绪")
    return [
        {
            "id": "live2d-render",
            "kind": "render",
            "label": "角色画面（Live2D）",
            "installed": installed,
            "ready": ready,
            "detail": detail,
            "needs": "pywebview 与系统里的 Edge WebView2 运行时；Win10/11 一般自带，缺了装一次即可",
            "license": "pywebview：BSD-3-Clause；WebView2 运行时随 Windows 提供",
        }
    ]


def knowledge_index_state(db_path=""):
    """只看知识库建没建、里面多少文档和片段。只读打开，绝不建也不改。"""
    path = _memory_db_file(db_path)
    if not path or not os.path.isfile(path):
        return {"exists": False, "documents": 0, "chunks": 0, "reason": "还没有数据库文件"}
    handle = None
    try:
        uri = "file:%s?mode=ro" % path.replace("?", "%3f").replace("#", "%23")
        handle = sqlite3.connect(uri, uri=True, timeout=5.0)
        found = handle.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'documents'"
        ).fetchone()
        if not found:
            return {
                "exists": False,
                "documents": 0,
                "chunks": 0,
                "reason": "表还没建；重启桌宠后会自动补上",
            }
        docs = handle.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        chunks = handle.execute("SELECT COUNT(*) FROM doc_chunks").fetchone()[0]
        return {
            "exists": True,
            "documents": int(docs or 0),
            "chunks": int(chunks or 0),
            "reason": "",
        }
    except Exception as exc:
        return {"exists": False, "documents": 0, "chunks": 0, "reason": "读不出来：%s" % exc}
    finally:
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass


def _knowledge_entry(db_path=""):
    """知识库这条：文片段索引建没建、里面多少东西。"""
    state = knowledge_index_state(db_path)
    if state["exists"] and state["documents"]:
        detail = "已建索引：%d 份文档、%d 个片段（关键词命中）" % (
            state["documents"],
            state["chunks"],
        )
    elif state["exists"]:
        detail = "已就绪，还没加目录"
    else:
        detail = state["reason"] or "还没加目录"
    return {
        "id": "knowledge-base",
        "kind": "index",
        "label": "本地知识库（文件检索）",
        "installed": True,
        "ready": True,
        "detail": detail,
        "needs": "纯本地，不联网；关键词命中，不做向量检索",
        "license": "项目自带",
    }


def _search_entries():
    rg = _which("rg")
    return [
        {
            "id": "ripgrep",
            "kind": "search",
            "label": "ripgrep（命令行文本搜索）",
            "installed": bool(rg),
            "ready": bool(rg),
            "detail": "命令在 %s" % rg if rg else "没装；搜索走项目自带的纯 Python 实现",
            "needs": "只是加速项，不装也能搜；本项目不依赖它",
            "license": "MIT 或 Unlicense（双许可）",
        }
    ]


def _fts_entry(db_path=""):
    """全文检索这条：SQLite 支不支持 FTS5，记忆索引实际建了没有。"""
    supported = fts5_available()
    state = memory_index_state(db_path) if supported else {
        "exists": False,
        "indexed": 0,
        "tokenizer": "",
        "reason": "当前 SQLite 没有 FTS5",
    }
    if not supported:
        detail = "当前 SQLite 没有 FTS5；记忆检索退回关键词打分"
    elif state["exists"]:
        detail = "记忆全文索引已建（%s 分词，%d 条）" % (
            state["tokenizer"] or "默认",
            state["indexed"],
        )
    else:
        detail = "支持 FTS5；%s" % (state["reason"] or "索引还没建")
    return {
        "id": "sqlite-fts5",
        "kind": "index",
        "label": "SQLite FTS5 全文检索",
        "installed": supported,
        "ready": bool(supported),
        "detail": detail,
        "needs": "内置能力；索引不可用时记忆检索退回关键词打分，功能不受影响",
        "license": "SQLite 公共领域",
    }

def detect(db_path="", config=None):
    """把本机的运行时状态读一遍。每一项都带 installed / ready 和原因。"""
    entries = [
        _ollama_entry(),
        _llama_cpp_entry(),
        _sherpa_entry(config),
        *_document_entries(),
        *_vision_entries(),
        *_search_entries(),
        *_render_entries(),
        _fts_entry(db_path),
        _knowledge_entry(db_path),
    ]
    return {"db_path": str(db_path or ""), "entries": entries}


def rows(payload):
    """把探测结果压成给人看的一行行。"""
    entries = (payload or {}).get("entries") or []
    out = []
    for item in entries:
        mark = "装好了" if item.get("ready") else ("装了但还差东西" if item.get("installed") else "没装")
        line = "%s：%s" % (item.get("label") or item.get("id"), mark)
        extra = str(item.get("detail") or "").strip()
        if extra:
            line += "（%s）" % extra
        if item.get("models"):
            line += "；本地模型：%s" % "、".join(item["models"][:5])
        out.append((item.get("kind") or "", line))
    return out


def text(payload):
    """一段话说完本机现在能做什么、还差什么。"""
    lines = [line for _kind, line in rows(payload)]
    if not lines:
        return "没有探测到任何可选组件，基础功能照常可用。"
    return "；".join(lines)


def group_counts(payload):
    counts = {}
    for item in (payload or {}).get("entries") or []:
        kind = item.get("kind") or "other"
        bucket = counts.setdefault(kind, {"installed": 0, "total": 0})
        bucket["total"] += 1
        if item.get("installed"):
            bucket["installed"] += 1
    return counts


def storage_hint(payload):
    """哪些项要额外占地方，给用户一个提前量。"""
    hints = []
    for item in (payload or {}).get("entries") or []:
        if item.get("installed"):
            continue
        need = str(item.get("needs") or "").strip()
        if need and item.get("kind") in ("local-model", "document", "voice"):
            hints.append((item.get("label") or item.get("id"), need))
    return hints


def model_dir(base=""):
    """本地模型该放哪：默认在用户数据目录下的 models/。"""
    return str(Path(base) / "models") if base else ""
