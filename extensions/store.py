"""扩展的用户级安装状态。

记录「装没装、开没开、用户确认过什么」。状态写在用户数据目录的 extensions/
下面，重新打包不会丢，也不会写进程序目录。

文件坏了不装作没事：留一份 .bak 再重来，宁可丢状态也不丢真相。
"""

import json
import os
import time
from pathlib import Path

STATE_FILE = "state.json"


def default_dir(root=None):
    """用户数据目录下的 extensions/。取不到就退到当前目录。"""
    if root is not None:
        return Path(root)
    try:
        from security.config import data_root

        return Path(data_root()) / "extensions"
    except Exception:
        return Path.cwd() / "extensions"


class ExtensionStore:
    def __init__(self, root=None, clock=None):
        self.dir = Path(default_dir(root))
        self.path = self.dir / STATE_FILE
        self.clock = clock or time.time
        self._data = None
        self.last_error = ""

    # ---------- 读写 ----------
    def _load(self):
        if self._data is not None:
            return self._data
        data = {}
        if self.path.exists():
            try:
                with open(self.path, encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    data = loaded
                else:
                    raise ValueError("顶层不是对象")
            except Exception as exc:
                self.last_error = "状态文件读不出来：%s" % exc
                self._backup()
                data = {}
        if not isinstance(data.get("extensions"), dict):
            data["extensions"] = {}
        self._data = data
        return self._data

    def _backup(self):
        try:
            keep = self.path.with_suffix(".json.bak")
            os.replace(self.path, keep)
        except Exception:
            pass

    def _save(self):
        data = self._load()
        data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.clock()))
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".json.tmp")
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(temp, self.path)
            return True
        except Exception as exc:
            self.last_error = "状态写不进去：%s" % exc
            return False

    # ---------- 单条状态 ----------
    def state(self, ext_id):
        entry = self._load()["extensions"].get(str(ext_id or ""))
        return dict(entry) if isinstance(entry, dict) else {}

    def rows(self):
        return dict(self._load()["extensions"])

    def _update(self, ext_id, **fields):
        data = self._load()
        ext_id = str(ext_id or "")
        if not ext_id:
            return False
        entry = dict(data["extensions"].get(ext_id) or {})
        entry.update(fields)
        data["extensions"][ext_id] = entry
        return self._save()

    def set_enabled(self, ext_id, flag, reason=""):
        return self._update(
            ext_id,
            enabled=bool(flag),
            enabled_at=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.clock())) if flag else "",
            note=str(reason or ""),
        )

    def mark_installed(self, ext_id, pinned="", source=""):
        return self._update(
            ext_id,
            installed=True,
            pinned=str(pinned or ""),
            source=str(source or ""),
            installed_at=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.clock())),
        )

    def record_review(self, ext_id, note):
        return self._update(ext_id, review=str(note or ""))

    def uninstall(self, ext_id):
        """卸载只清状态，不删用户自己放的文件。"""
        data = self._load()
        ext_id = str(ext_id or "")
        if ext_id not in data["extensions"]:
            return False
        entry = dict(data["extensions"][ext_id])
        entry["installed"] = False
        entry["enabled"] = False
        entry["enabled_at"] = ""
        data["extensions"][ext_id] = entry
        return self._save()
