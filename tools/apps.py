"""打开网址、文件和用户自己配置的应用。

模型只能请求，真正的启动由程序做，一律经过权限中心。
没配置应用清单时不启动任何程序，免得模型随手拉起任意可执行文件。
"""

import os
import subprocess

from tools.registry import Tool, ToolError

APP_ENV = "DESKPET_APPS"

# 只直接打开文档、图片、音视频这类文件；可执行文件和脚本一律不代开。
OPENABLE_SUFFIXES = (
    ".txt", ".md", ".log", ".pdf", ".csv",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".mp3", ".wav", ".m4a", ".mp4", ".mkv", ".mov",
)


def parse_apps(raw):
    """把 "记事本=notepad;浏览器=msedge" 解析成 {名字: [命令]}。

    解析不了的条目直接丢掉，宁可少几个也不乱猜。
    """
    apps = {}
    text = str(raw or "").replace("；", ";").replace("\n", ";")
    for item in text.split(";"):
        name, _, command = item.partition("=")
        name = name.strip()
        command = command.strip()
        if not name or not command:
            continue
        apps[name.lower()] = command.split()
    return apps


def _open(target, label):
    """交给系统默认程序打开。失败就说失败，不假装打开过。"""
    if os.name != "nt" or not hasattr(os, "startfile"):
        raise ToolError("当前系统不支持直接打开，得手动来")
    try:
        os.startfile(target)
    except OSError as exc:
        raise ToolError("打不开这个%s：%s" % (label, exc))
    return {"opened": target, "kind": label}


def open_url_handler():
    def handler(url):
        target = str(url or "").strip()
        if not target:
            raise ToolError("要给一个网址")
        lowered = target.lower()
        if not (lowered.startswith("http://") or lowered.startswith("https://")):
            raise ToolError("只打开 http 或 https 的网址")
        return _open(target, "网址")

    return handler


def open_path_handler():
    def handler(path):
        raw = str(path or "").strip()
        if not raw:
            raise ToolError("要说清打开哪个文件")
        target = os.path.realpath(os.path.expanduser(raw))
        if not os.path.exists(target):
            raise ToolError("找不到这个文件：%s" % raw)
        if os.path.isdir(target):
            return _open(target, "文件夹")
        suffix = os.path.splitext(target)[1].lower()
        if suffix not in OPENABLE_SUFFIXES:
            raise ToolError("这个类型不直接打开，免得启动可执行文件：%s" % (suffix or "未知"))
        return _open(target, "文件")

    return handler


def launch_app_handler(apps=None):
    table = apps if apps is not None else parse_apps(os.environ.get(APP_ENV))

    def handler(name):
        key = str(name or "").strip().lower()
        if not key:
            raise ToolError("要说清启动哪个应用")
        if key not in table:
            if not table:
                raise ToolError("还没有配置可启动的应用，先在设置里加一条")
            raise ToolError(
                "没有配置这个应用：%s。现在配了：%s" % (name, "、".join(sorted(table)))
            )
        argv = list(table[key])
        try:
            subprocess.Popen(argv, close_fds=True)
        except OSError as exc:
            raise ToolError("启动失败：%s" % exc)
        return {"app": name, "command": argv[0]}

    return handler


def register_app_tools(registry, config=None, apps=None):
    """装上打开网址、打开文件、启动已配置应用三个工具。"""
    if apps is None:
        raw = getattr(config, "apps", "") if config is not None else ""
        apps = parse_apps(raw or os.environ.get(APP_ENV))
    registry.register(
        Tool(
            name="open_url",
            description="用系统默认浏览器打开一个 http 或 https 网址",
            schema={
                "type": "object",
                "properties": {"url": {"type": "string", "description": "要打开的网址"}},
                "required": ["url"],
                "additionalProperties": False,
            },
            permission="MEDIUM",
            risk="会打开浏览器，需要你同意",
            idempotent=False,
            handler=open_url_handler(),
            keywords=("打开网址", "浏览器", "打开网页", "链接"),
        )
    )
    registry.register(
        Tool(
            name="open_path",
            description="用系统默认程序打开一个文件或文件夹（不含可执行文件）",
            schema={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "文件或文件夹路径"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            permission="HIGH",
            risk="会交给系统默认程序打开",
            idempotent=False,
            handler=open_path_handler(),
            keywords=("打开文件", "打开文件夹", "打开文档", "打开"),
        )
    )
    registry.register(
        Tool(
            name="launch_app",
            description="启动用户自己在设置里登记过的应用",
            schema={
                "type": "object",
                "properties": {"name": {"type": "string", "description": "应用名字"}},
                "required": ["name"],
                "additionalProperties": False,
            },
            permission="HIGH",
            risk="会启动本机程序，只能启动你登记过的",
            idempotent=False,
            handler=launch_app_handler(apps),
            keywords=("打开应用", "启动程序", "打开软件"),
        )
    )
    return apps