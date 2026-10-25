"""语音包：一个文件夹装一套发声设置。

    voices/<任意名字>/voicepack.json
    {
      "id": "xiaodie",
      "name": "小蝶声音",
      "version": "1.0",
      "engine": "sapi",
      "voice": "Microsoft Huihui",
      "rate": 0,
      "volume": 100,
      "note": "一句话说明"
    }

只读 JSON 元数据：不复制、不加载、不执行包里的任何脚本或程序。
导入时只把校验过的元数据写进 voices/<id>/voicepack.json，其它文件一概不进项目。
没装语音包就用系统默认声音，不影响打字聊天。
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

MANIFEST = "voicepack.json"
# 目前只认 Windows 自带朗读；以后换引擎在这里加名字，别偷偷放行未知引擎。
ENGINES = ("sapi",)
MAX_MANIFEST_BYTES = 64 * 1024
MAX_TEXT = 80

# 内部设置：选中的语音包，不进模型上下文（voice. 前缀在记忆层被挡掉）。
PREFERENCE_KEY = "voice.pack"

_ID_OK = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")


@dataclass
class VoicePack:
    """一套发声设置。字段全部来自 JSON，程序不解释里面的内容。"""

    id: str
    name: str = ""
    version: str = ""
    engine: str = "sapi"
    voice: str = ""
    rate: int = 0
    volume: int = 100
    note: str = ""
    path: str = ""

    def describe(self):
        """给界面看的只读视图。绝不发给模型。"""
        return {
            "id": self.id,
            "name": self.name or self.id,
            "version": self.version or "未标版本",
            "engine": self.engine,
            "voice": self.voice or "系统默认",
            "rate": self.rate,
            "volume": self.volume,
            "note": self.note,
            "path": self.path,
        }

    def label(self):
        """一行字：名字 + 版本。"""
        return f"{self.name or self.id}（{self.version or '未标版本'}）"


def _clamp(value, low, high, default):
    try:
        return max(low, min(high, int(float(value))))
    except (TypeError, ValueError):
        return default


def _text(value, limit=MAX_TEXT):
    return str(value or "").strip()[:limit]


def from_dict(data, path=""):
    """把一份元数据变成语音包。缺 id、引擎不认、字段格式不对，一律拒绝。"""
    if not isinstance(data, dict):
        return None
    pack_id = _text(data.get("id"), 40)
    if not _ID_OK.match(pack_id or ""):
        return None
    engine = _text(data.get("engine"), 20).lower() or "sapi"
    if engine not in ENGINES:
        return None
    return VoicePack(
        id=pack_id,
        name=_text(data.get("name")),
        version=_text(data.get("version"), 20),
        engine=engine,
        voice=_text(data.get("voice")),
        rate=_clamp(data.get("rate", 0), -10, 10, 0),
        volume=_clamp(data.get("volume", 100), 0, 100, 100),
        note=_text(data.get("note"), 200),
        path=str(path or ""),
    )


def load(folder):
    """读一个语音包文件夹里的元数据。文件太大、不是 JSON、字段不合法都返回 None。"""
    manifest = Path(folder) / MANIFEST
    try:
        if not manifest.is_file() or manifest.stat().st_size > MAX_MANIFEST_BYTES:
            return None
        with manifest.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return None
    return from_dict(data, path=str(folder))


def scan(root):
    """扫一遍语音包目录。目录不在、某个包坏了都跳过，不抛异常。"""
    if not root:
        return []
    base = Path(root)
    try:
        folders = sorted(item for item in base.iterdir() if item.is_dir())
    except Exception:
        return []
    packs = []
    for folder in folders:
        pack = load(folder)
        if pack is not None:
            packs.append(pack)
    return packs


def find(root, pack_id):
    """按 id 找一个包。找不到返回 None。"""
    target = _text(pack_id, 40)
    if not target:
        return None
    for pack in scan(root):
        if pack.id == target:
            return pack
    return None


def import_pack(root, source, overwrite=False):
    """导入一个语音包：只校验并记下元数据，不复制也不执行任何其它文件。

    返回 {"ok": bool, "error": str, "pack": VoicePack 或 None}。
    """
    folder = Path(source) if source else None
    if folder is None or not folder.is_dir():
        return _fail("请选一个语音包文件夹")
    pack = load(folder)
    if pack is None:
        return _fail(f"文件夹里没有可用的 {MANIFEST}")
    base = Path(root)
    target = (base / pack.id).resolve()
    try:
        base.resolve().mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return _fail(f"语音包目录不可用：{exc}")
    if base.resolve() not in target.parents:
        return _fail("语音包名字不合法")
    if target.exists() and not overwrite:
        return _fail(f"已经有同名的语音包：{pack.id}")
    try:
        target.mkdir(parents=True, exist_ok=True)
        payload = {
            "id": pack.id,
            "name": pack.name,
            "version": pack.version,
            "engine": pack.engine,
            "voice": pack.voice,
            "rate": pack.rate,
            "volume": pack.volume,
            "note": pack.note,
        }
        with (target / MANIFEST).open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
    except Exception as exc:
        return _fail(f"写入失败：{exc}")
    pack.path = str(target)
    return {"ok": True, "error": "", "pack": pack}


def selected(preferences):
    """当前选中的语音包 id。读不到就返回空字符串。"""
    if preferences is None:
        return ""
    try:
        raw = preferences.get_preference(PREFERENCE_KEY)
    except Exception:
        return ""
    return _text(raw, 40)


def select(preferences, pack_id):
    """选定一个语音包。空字符串表示回到系统默认。"""
    if preferences is None:
        return False
    try:
        preferences.set_preference(PREFERENCE_KEY, _text(pack_id, 40))
        return True
    except Exception:
        return False


def resolve(root, preferences):
    """把「选中的 id」变成真正的语音包。没选或找不到都返回 None。"""
    current = selected(preferences)
    if not current:
        return None
    return find(root, current)


def _fail(message):
    return {"ok": False, "error": str(message), "pack": None}
