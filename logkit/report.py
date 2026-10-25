"""诊断包：把日志和一份脱敏的运行环境说明打成一个压缩包。

导出前把密钥值再抹一遍。包里只有程序路径、版本和日志，没有密钥明文。
"""

import platform
import sys
import time
import zipfile
from pathlib import Path

from logkit.logger import redact

STAMP = "%Y%m%d-%H%M%S"
LOG_GLOB = "deskpet.log*"


def version_text():
    try:
        from app.version import describe

        return describe()
    except Exception:
        return "Blossom"


def has_secret(config):
    """只回答「配没配密钥」，永远不回答密钥是什么。"""
    try:
        values = config.secret_values
        if callable(values):
            values = values()
        return bool(values)
    except Exception:
        return False


def build_report(config=None, extra=None):
    """给人看的诊断报告。所有内容先过一遍脱敏。"""
    lines = [
        "Blossom 诊断报告",
        "生成时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
        "版本：%s" % version_text(),
        "Python：%s" % sys.version.split()[0],
        "系统：%s %s（%s）" % (platform.system(), platform.release(), platform.machine()),
        "打包运行：%s" % ("是" if getattr(sys, "frozen", False) else "否"),
    ]
    if config is not None:
        lines.extend(["", "路径与配置（不含密钥）："])
        try:
            view = config.redacted()
        except Exception:
            view = {}
        for key in sorted(view):
            lines.append("  %s = %s" % (key, view[key]))
        lines.append("  密钥是否已配置：%s" % ("是" if has_secret(config) else "否"))
    if extra:
        lines.extend(["", "补充信息："])
        for key in sorted(dict(extra)):
            lines.append("  %s = %s" % (key, dict(extra)[key]))
    return redact("\n".join(lines))


def log_files(config):
    folder = Path(getattr(config, "log_dir", None) or ".")
    try:
        return sorted(item for item in folder.glob(LOG_GLOB) if item.is_file())
    except Exception:
        return []


def default_target(config=None):
    stamp = time.strftime(STAMP)
    base = Path(getattr(config, "data_dir", None) or Path.cwd())
    return base / "diagnostics" / ("Blossom-diagnostics-%s.zip" % stamp)


def export_report(config=None, target=None, extra=None):
    """打一个诊断包。写不动时如实返回失败，不假装导出过。"""
    path = Path(target) if target else default_target(config)
    written = []
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("report.txt", build_report(config, extra=extra))
            written.append("report.txt")
            for item in log_files(config):
                try:
                    text = item.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                bundle.writestr("logs/" + item.name, redact(text))
                written.append("logs/" + item.name)
    except Exception as exc:
        return {"ok": False, "path": str(path), "files": written, "error": str(exc)}
    return {"ok": True, "path": str(path), "files": written, "error": ""}