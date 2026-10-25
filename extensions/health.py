"""扩展健康检查。

只看本机事实：模块在不在、可执行文件在不在、路径存不存在、Python 版本够不够。
不联网、不下载、不安装，也不执行第三方代码。问不出来就说问不出来。
"""

import importlib.util
import json
import shutil
import sys
import time
from pathlib import Path

OK = "ok"
MISSING = "missing"
UNKNOWN = "unknown"
BLOCKED = "blocked"

STATUS_LABELS = {
    OK: "环境齐了",
    MISSING: "还没装",
    UNKNOWN: "查不到",
    BLOCKED: "许可证没过，不能启用",
}


def _python_ok(wanted):
    try:
        parts = tuple(int(item) for item in str(wanted or "3.10").split(".")[:3])
    except ValueError:
        return False
    return sys.version_info[: len(parts)] >= parts


def _check_one(spec):
    kind = str(spec.get("kind") or "").strip()
    name = str(spec.get("name") or "").strip()
    hint = str(spec.get("hint") or "").strip()
    found = False
    detail = ""
    try:
        if kind == "module":
            found = importlib.util.find_spec(name) is not None if name else False
            detail = "Python 模块 %s" % (name or "?")
        elif kind == "binary":
            path = shutil.which(name) if name else None
            found = bool(path)
            detail = "可执行文件 %s" % (path or (name or "?"))
        elif kind == "path":
            found = bool(name) and Path(name).expanduser().exists()
            detail = "路径 %s" % (name or "?")
        elif kind == "python":
            found = _python_ok(spec.get("min"))
            detail = "Python 至少 %s" % (spec.get("min") or "3.10")
        elif kind == "platform":
            wanted = str(spec.get("name") or "").strip().lower()
            if wanted in ("windows", "win32"):
                found = sys.platform.startswith("win")
            elif wanted in ("linux",):
                found = sys.platform.startswith("linux")
            elif wanted in ("darwin", "macos"):
                found = sys.platform.startswith("darwin")
            detail = "系统平台 %s" % wanted
        else:
            detail = "不认识的检查类型：%s" % (kind or "空")
    except Exception as exc:
        return {"kind": kind, "name": name, "status": UNKNOWN, "detail": "%s（%s）" % (detail, exc), "hint": hint}
    return {
        "kind": kind,
        "name": name,
        "status": OK if found else MISSING,
        "detail": detail,
        "hint": hint,
    }


def check(manifest, clock=None):
    """给一个扩展摸一遍环境。返回 {status, checks, detail, checked_at}。"""
    if manifest is None:
        return {"status": UNKNOWN, "detail": "不知道这是哪个扩展", "checks": [], "checked_at": ""}
    checked_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(clock() if clock else time.time()))
    if manifest.license_blocked():
        return {
            "status": BLOCKED,
            "detail": "许可证不允许随包分发，先不动它",
            "checks": [],
            "checked_at": checked_at,
        }
    checks = [_check_one(dict(spec)) for spec in (manifest.health or ())]
    if not checks:
        return {
            "status": OK,
            "detail": "不需要额外环境",
            "checks": [],
            "checked_at": checked_at,
        }
    missing = [item for item in checks if item["status"] != OK]
    if missing:
        first = missing[0]
        detail = first.get("hint") or ("缺：" + first.get("detail", ""))
        return {"status": MISSING, "detail": detail, "checks": checks, "checked_at": checked_at}
    return {"status": OK, "detail": "环境齐了", "checks": checks, "checked_at": checked_at}


def load_state(path):
    """读上次的健康检查结果；文件坏了就当没有，不拦着用。"""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}
