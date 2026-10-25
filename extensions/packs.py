"""能力包：把可选能力做成单独下载的包，基础包不背它们的体积。

文档解析（markitdown 那一套）和离线语音（sherpa-onnx）加起来一百多 MB，
多数用户用不到。它们现在不打进基础包，而是打成独立的 zip：
用户要用时在能力中心装入，包解开到数据目录的 ``packs/<名字>/``，
启动时把里面的 ``site`` 目录接到 ``sys.path`` 前面，后面的 import 就能找到。

几条规矩：
    * 只认带 pack.json 的包，名字必须和目录对得上。
    * 解开时不允许绝对路径、不允许 ``..``、不允许符号链接，防目录穿越。
    * 卸载只删这一个包自己的目录，绝不碰用户数据。
    * 没有装任何包时，这里什么都不做，主程序照常启动。
"""

import io
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

PACKS_DIRNAME = "packs"
MANIFEST_NAME = "pack.json"
SITE_DIRNAME = "site"
MAX_MANIFEST_BYTES = 64 * 1024


def packs_dir(config=None):
    """能力包放在数据目录下的 packs/。取不到数据目录就用用户目录兜底。"""
    root = getattr(config, "data_root", None) or getattr(config, "data_dir", None)
    if not root:
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "DeskPet"
    return Path(root) / PACKS_DIRNAME


def _read_manifest(folder):
    path = Path(folder) / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def installed(config=None):
    """已经装好的能力包。读不出清单的目录跳过，不当成装好了。"""
    root = packs_dir(config)
    rows = []
    if not root.is_dir():
        return rows
    for folder in sorted(root.iterdir()):
        if not folder.is_dir():
            continue
        manifest = _read_manifest(folder)
        if not manifest:
            continue
        name = str(manifest.get("name") or "")
        if name != folder.name:
            # 目录名和清单对不上：不认，免得被改名绕过。
            continue
        rows.append(
            {
                "name": name,
                "title": str(manifest.get("title") or name),
                "version": str(manifest.get("version") or ""),
                "provides": [str(item) for item in (manifest.get("provides") or [])],
                "note": str(manifest.get("note") or ""),
                "path": str(folder),
                "site": str(folder / SITE_DIRNAME),
            }
        )
    return rows


def site_dirs(config=None):
    """已装包里真实存在的 site 目录。没有就返回空列表。"""
    rows = []
    for row in installed(config):
        site = Path(row["site"])
        if site.is_dir():
            rows.append(str(site))
    return rows


def activate(config=None):
    """把已装的能力包接进 sys.path。重复调用不会重复插入。"""
    added = []
    for path in site_dirs(config):
        if path not in sys.path:
            sys.path.insert(0, path)
            added.append(path)
    return added


def _safe_members(archive):
    """过一遍包里的条目，有越界的一律拒绝。"""
    for info in archive.infolist():
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or ":" in name.split("/")[0]:
            raise ValueError("包里有绝对路径：%s" % info.filename)
        parts = [part for part in name.split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            raise ValueError("包里有向上跳的路径：%s" % info.filename)
    return True


def install(config, archive_path):
    """装一个能力包。装坏了不留半个目录。"""
    root = packs_dir(config)
    source = Path(archive_path)
    if not source.is_file():
        return {"ok": False, "error": "找不到这个包：%s" % source}
    try:
        with zipfile.ZipFile(source) as archive:
            _safe_members(archive)
            raw = archive.read(MANIFEST_NAME)
            manifest = json.loads(raw.decode("utf-8"))
            name = str(manifest.get("name") or "").strip()
            if not name or any(char in name for char in "\\/:*?\"<>|"):
                return {"ok": False, "error": "包清单里的名字不合法"}
            target = root / name
            if target.exists():
                return {
                    "ok": False,
                    "error": "已经装过同名的包了：%s" % name,
                    "hint": "先卸载再装，或者直接覆盖目录。",
                }
            root.mkdir(parents=True, exist_ok=True)
            staging = root / (name + ".installing")
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            try:
                archive.extractall(staging)
                staging.replace(target)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
    except (zipfile.BadZipFile, ValueError, KeyError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": "这个包不能用：%s" % exc}
    except Exception as exc:
        return {"ok": False, "error": "装包失败：%s" % exc}
    activate(config)
    row = {"ok": True, "name": name, "path": str(target)}
    row.update({"title": str(manifest.get("title") or name)})
    return row


def remove(config, name):
    """卸载一个能力包：只删它自己的目录。"""
    wanted = str(name or "").strip()
    if not wanted or any(char in wanted for char in "\\/:*?\"<>|"):
        return {"ok": False, "error": "名字不合法"}
    target = packs_dir(config) / wanted
    if not target.is_dir():
        return {"ok": False, "error": "没有装这个包：%s" % wanted}
    try:
        shutil.rmtree(target)
    except Exception as exc:
        return {"ok": False, "error": "卸载失败：%s" % exc}
    # 路径还在 sys.path 里没关系：目录没了，import 自然找不到。
    return {"ok": True, "name": wanted}
