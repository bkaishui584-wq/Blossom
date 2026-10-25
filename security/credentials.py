"""模型 API Key 的安全存储（Windows DPAPI）。

密钥用当前 Windows 用户的 DPAPI 加密后单独落盘。密文只能被同一个用户、
同一台机器解开：换用户、换电脑、重装系统都解不开。这不是缺陷，是这层保护
的定义，界面上必须如实说明，不能暗示密钥可以随数据目录直接搬走。

拿不到 DPAPI（非 Windows、调用失败）时一律拒绝保存，绝不退回明文。
普通配置（地址、模型名）不属于这里，见 model/config.py。
"""

import ctypes
import os
import uuid
from ctypes import wintypes
from pathlib import Path

_DESCRIPTION = "Blossom model API key"
_ENTROPY = b"blossom.model-key.v1"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _crypt32():
    if os.name != "nt":
        return None
    try:
        return ctypes.windll.crypt32
    except Exception:
        return None


def _bind(crypt32):
    """显式声明参数类型：不声明的话字符串参数会按 ANSI 传，中文描述会乱。"""
    for name in ("CryptProtectData", "CryptUnprotectData"):
        fn = getattr(crypt32, name, None)
        if fn is None:
            return False
        fn.argtypes = [
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        fn.restype = wintypes.BOOL
    return True


def available():
    """这台机器能不能安全保存密钥。不能就不让用户以为已经存好了。"""
    crypt32 = _crypt32()
    return bool(crypt32 is not None and _bind(crypt32))


def _blob(data):
    buffer = ctypes.create_string_buffer(bytes(data), len(data))
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _entropy():
    return _blob(_ENTROPY)[0]


def _protect(raw):
    crypt32 = _crypt32()
    if crypt32 is None or not _bind(crypt32):
        raise RuntimeError("这台机器不支持 DPAPI")
    source, _keep = _blob(raw)
    output = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(source),
        _DESCRIPTION,
        ctypes.byref(_entropy()),
        None,
        None,
        0x1,  # CRYPTPROTECT_UI_FORBIDDEN：不弹系统对话框
        ctypes.byref(output),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)


def _unprotect(blob):
    crypt32 = _crypt32()
    if crypt32 is None or not _bind(crypt32):
        raise RuntimeError("这台机器不支持 DPAPI")
    source, _keep = _blob(blob)
    output = _DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        ctypes.byref(_entropy()),
        None,
        None,
        0x1,
        ctypes.byref(output),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)


class SecretStore:
    """一个密钥一个密文文件，文件名就是密钥标识。"""

    def __init__(self, folder):
        self.folder = Path(folder)

    def new_id(self):
        return uuid.uuid4().hex

    def file_for(self, secret_id):
        name = str(secret_id or "").strip()
        if not name or any(char in name for char in "\\/:*?\"<>|"):
            return None
        return self.folder / (name + ".bin")

    def save(self, secret_id, value):
        """加密保存。空值等于删除；DPAPI 不可用时如实失败。"""
        text = str(value or "").strip()
        if not text:
            return {"ok": False, "error": "密钥是空的"}
        target = self.file_for(secret_id)
        if target is None:
            return {"ok": False, "error": "密钥标识不合法"}
        if not available():
            return {"ok": False, "error": "这台机器没有可用的安全存储（DPAPI），为安全起见不保存密钥"}
        try:
            blob = _protect(text.encode("utf-8"))
        except Exception as exc:
            return {"ok": False, "error": "加密失败：%s" % exc}
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_suffix(".tmp")
            temp.write_bytes(blob)
            os.replace(temp, target)
        except Exception as exc:
            return {"ok": False, "error": "写不进密钥文件：%s" % exc}
        return {"ok": True, "secret_id": str(secret_id)}

    def load(self, secret_id):
        """读回明文密钥。读不到就返回空串，不抛异常。"""
        target = self.file_for(secret_id)
        if target is None or not target.is_file():
            return ""
        try:
            raw = _unprotect(target.read_bytes())
            return raw.decode("utf-8", "replace").strip()
        except Exception:
            return ""

    def has(self, secret_id):
        target = self.file_for(secret_id)
        return bool(target and target.is_file())

    def delete(self, secret_id):
        target = self.file_for(secret_id)
        if target is None:
            return False
        try:
            if target.is_file():
                target.unlink()
            return True
        except Exception:
            return False

    def mask(self, secret_id):
        """给界面看的一行状态，永远不包含密钥内容。"""
        if not self.has(secret_id):
            return "未保存"
        return "已保存（%s）" % ("可读取" if self.load(secret_id) else "读不出来")
