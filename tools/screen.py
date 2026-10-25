"""屏幕与上下文工具：读当前前台窗口的真实信息。

只读，默认关着。用户没把屏幕感知打开之前，这个工具只会如实说「关着」，
不截图、不录键盘、不做身份推断，也不把内容发到任何地方。

界面语义（可访问性树）是可选的：没人接语义实现时，只给窗口标题和进程名。
"""

import ctypes
import os

from model.base import LOCAL_ENDPOINT_HOSTS, endpoint_host
from tools.registry import Tool, ToolError

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_provider = None
_privacy = None


def set_vision_provider(provider=None, privacy=None):
    """挂上语义后端和隐私开关。只换传进来的那一样，另一样保持原样。"""
    global _provider, _privacy
    if provider is not None:
        _provider = provider
    if privacy is not None:
        _privacy = privacy
    return _provider


def perception_on():
    """默认关着：没有隐私开关就是关着。"""
    if _privacy is None:
        return False
    try:
        return bool(_privacy())
    except Exception:
        return False


def screen_to_model_allowed(privacy, config, role="task"):
    """屏幕内容能不能交给模型。

    本机模型不算"发出去"，直接放行；云端（或没配地址的官方接口）要用户单独打开
    「把屏幕内容交给模型」那一项。没有隐私中心时按老行为放行。
    """
    if privacy is None:
        return True
    try:
        if privacy.allows("screen_to_model"):
            return True
    except Exception:
        return False
    url = ""
    if config is not None:
        if str(role) == "companion":
            url = getattr(config, "companion_base_url", "") or getattr(config, "llm_base_url", "")
        else:
            url = getattr(config, "task_base_url", "") or getattr(config, "llm_base_url", "")
    host = endpoint_host(url)
    return bool(host) and host in LOCAL_ENDPOINT_HOSTS


def screen_model_policy(privacy, config):
    """给 Agent 用的规则：问一句「这个角色的屏幕内容能不能给模型」。"""
    return lambda role="task": screen_to_model_allowed(privacy, config, role)


def windows_available():
    return hasattr(ctypes, "windll")


def process_name(pid):
    """按进程号取可执行文件名。取不到返回空串，不猜。"""
    if not windows_available() or not pid:
        return ""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ""
    try:
        size = ctypes.c_ulong(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return os.path.basename(buffer.value)
    except Exception:
        return ""
    finally:
        kernel32.CloseHandle(handle)


def foreground_window():
    """当前前台窗口的标题和进程名。读不到就说清楚为什么。"""
    if not windows_available():
        return {"ok": False, "reason": "这个系统上没有 Windows 窗口接口"}
    user32 = ctypes.windll.user32
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return {"ok": False, "reason": "现在没有前台窗口"}
        length = int(user32.GetWindowTextLengthW(hwnd))
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return {
            "ok": True,
            "title": buffer.value or "（没有标题）",
            "process": process_name(pid.value),
        }
    except Exception as exc:
        return {"ok": False, "reason": "读前台窗口失败：%s" % exc}


def semantics():
    """可访问性树里的公开语义。没接实现就如实说没有。"""
    if _provider is None:
        return {"ok": False, "reason": "没接可读界面语义的实现，只给窗口标题"}
    snapshot = getattr(_provider, "snapshot", None)
    if not callable(snapshot):
        return {"ok": False, "reason": "接上的后端不支持语义读取"}
    try:
        data = snapshot() or {}
    except Exception as exc:
        return {"ok": False, "reason": "语义读取失败：%s" % exc}
    if not isinstance(data, dict):
        return {"ok": True, "text": str(data)[:300]}
    if not data.get("ok"):
        return {"ok": False, "reason": str(data.get("reason") or "语义读取失败")}
    inner = data.get("data")
    text = ""
    if isinstance(inner, dict):
        text = str(inner.get("text") or inner.get("title") or "")
    elif inner:
        text = str(inner)
    return {"ok": True, "text": text[:300]}


def screen_context_text(what="all"):
    """给模型看的一行读数。读不到一律抛错，不编。"""
    if not perception_on():
        raise ToolError(
            "屏幕感知默认关着。想让我看当前窗口，先把屏幕感知打开再问。"
        )
    info = foreground_window()
    if not info.get("ok"):
        raise ToolError(str(info.get("reason") or "读不到前台窗口"))
    parts = ["前台是「%s」" % info["title"]]
    if info.get("process"):
        parts.append("来自 %s" % info["process"])
    what = str(what or "all").strip().lower()
    if what in ("window", "窗口", "前台"):
        return "，".join(parts) + "。"
    state = semantics()
    if state.get("ok"):
        if state.get("text"):
            parts.append("界面上的文字：%s" % state["text"])
    elif what in ("semantics", "语义", "文字"):
        raise ToolError(str(state.get("reason") or "读不到界面语义"))
    return "，".join(parts) + "。"


def register_screen_tools(registry, config=None, privacy=None):
    """装上屏幕工具。默认关着：只有隐私开关打开才真的读。"""
    if privacy is None and config is not None:
        privacy = lambda: bool(getattr(config, "screen_perception", False))
    set_vision_provider(None, privacy)
    registry.register(
        Tool(
            name="screen_context",
            description="读当前前台窗口的应用和标题，屏幕感知打开后才有内容。只读，不截图。",
            schema={
                "type": "object",
                "properties": {
                    "what": {
                        "type": "string",
                        "enum": ["all", "window", "semantics"],
                        "description": "all 全给，window 只要窗口，semantics 只要界面文字",
                    }
                },
            },
            permission="READ",
            risk="只读前台窗口信息，不截图、不发外网",
            handler=screen_context_text,
            keywords=("前台", "当前窗口", "在用什么软件", "屏幕", "当前应用", "窗口"),
            readonly=True,
        )
    )
    return registry
