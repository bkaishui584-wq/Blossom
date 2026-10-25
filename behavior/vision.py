"""视觉能力：用户手动触发的一次截图，加上一个明确的 OCR 边界。

规矩：
    不静默截图。只有用户自己点「看一眼」并已授权，才截一次图，存进数据目录。
    不连着传。截图只落在本地，程序不自动上传给任何服务。
    不假装看得见。没有 OCR 引擎、没有多模态通道时，如实说做不到。
    不认人。不做人脸识别，不记录窗口内容。

以后接真视觉（OCR 引擎、YOLO、摄像头）时只加一个 Provider 实现，
界面与 Agent Core 都不用改：

    is_available() -> bool
    observe(source="screen") -> {"ok", "data", "error"}
"""

import ctypes
import os
import struct
import zlib
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

# 手动截图默认存这里：数据目录下，跟着用户数据一起备份，不进程序包。
CAPTURE_DIRNAME = "vision"
MAX_CAPTURE_SIDE = 4096
SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0

# 没有 OCR 引擎时的原话。别让界面自己编。
NO_OCR_ENGINE = "还没有装 OCR 引擎，读不了图上的字"


class VisionProvider(ABC):
    """视觉能力来源。默认读不到，也没有任何画面数据。"""

    name = "base"

    @abstractmethod
    def is_available(self):
        """能不能看。默认实现永远返回 False。"""

    @abstractmethod
    def observe(self, source="screen"):
        """看一眼并返回结构化结果。看不到就如实返回 ok=False。"""


class NullVisionProvider(VisionProvider):
    """没接视觉时的默认实现：如实说看不到，绝不编造画面内容。"""

    name = "null"

    def is_available(self):
        return False

    def observe(self, source="screen"):
        return {"ok": False, "error": "还没有接视觉能力", "data": None, "source": source}

    def describe(self):
        return {
            "provider": self.name,
            "available": False,
            "note": "只有接口，没有截图、没有 OCR、没有摄像头",
        }


# ---------------------------------------------------------------- 截图
def _write_png(path, width, height, rgb_rows):
    """写一张 8 位 RGB PNG。只用标准库，避免为了截个图去装图像库。"""

    def chunk(tag, payload):
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    raw = bytearray()
    for row in rgb_rows:
        raw.append(0)  # 每行过滤器 0（不过滤）
        raw.extend(row)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    data = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return len(data)


class ScreenCaptureProvider(VisionProvider):
    """Windows 屏幕截图。只在被明确调用时截一次，不轮询、不上传。"""

    name = "screen"

    def __init__(self, out_dir=None, logger=None, max_side=MAX_CAPTURE_SIDE):
        self.out_dir = out_dir
        self.logger = logger
        self.max_side = int(max_side)
        self.captures = 0
        self.last_capture = None

    def is_available(self):
        return os.name == "nt"

    def observe(self, source="screen", region=None):
        """截一次图并落盘。返回文件路径与尺寸，看不到就如实报错。"""
        if not self.is_available():
            return {"ok": False, "error": "只有 Windows 能截图", "data": None, "source": source}
        try:
            width, height, pixels = self._grab(region)
        except Exception as exc:
            self._warn("screen capture failed: %s", exc)
            return {"ok": False, "error": f"截图失败：{exc}", "data": None, "source": source}
        rows = self._rows(width, height, pixels)
        path = self._target_path()
        try:
            size = _write_png(path, width, height, rows)
        except Exception as exc:
            self._warn("screen capture write failed: %s", exc)
            return {"ok": False, "error": f"截图存不下来：{exc}", "data": None, "source": source}
        self.captures += 1
        data = {
            "path": str(path),
            "width": width,
            "height": height,
            "bytes": size,
            "region": list(region) if region else None,
        }
        self.last_capture = data
        return {"ok": True, "error": "", "data": data, "source": source}

    def describe(self):
        return {
            "provider": self.name,
            "available": self.is_available(),
            "captures": self.captures,
            "note": "只在用户手动触发时截一次，存本地，不自动上传",
        }

    # ---------- 内部 ----------
    def _target_path(self):
        base = Path(self.out_dir) if self.out_dir else Path.cwd() / "data" / CAPTURE_DIRNAME
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return base / f"capture-{stamp}.png"

    def _rows(self, width, height, pixels):
        """BGRA 缓冲转成 PNG 要的 RGB 行。顺手按上限缩一缩，别写出超大图。"""
        step = 1
        while max(width / step, height / step) > self.max_side:
            step += 1
        rows = []
        for y in range(0, height, step):
            row = bytearray()
            line = y * width * 4
            for x in range(0, width, step):
                offset = line + x * 4
                row += bytes((pixels[offset + 2], pixels[offset + 1], pixels[offset]))
            rows.append(row)
        return rows

    def _grab(self, region=None):
        """用 GDI 抓一次屏幕到内存。句柄一律声明成指针宽度，64 位下才不被截断。"""
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

        user32.GetDC.restype = wintypes.HDC
        user32.GetDC.argtypes = [wintypes.HWND]
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        user32.GetSystemMetrics.restype = ctypes.c_int
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        gdi32.CreateCompatibleDC.restype = wintypes.HDC
        gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
        gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        gdi32.SelectObject.restype = wintypes.HGDIOBJ
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        gdi32.BitBlt.argtypes = [
            wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.DWORD,
        ]
        gdi32.GetDIBits.argtypes = [
            wintypes.HDC,
            wintypes.HBITMAP,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.UINT,
        ]
        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi32.DeleteDC.argtypes = [wintypes.HDC]

        if region:
            left, top, width, height = (int(value) for value in region)
        else:
            left, top = 0, 0
            width = int(user32.GetSystemMetrics(0))
            height = int(user32.GetSystemMetrics(1))
        if width <= 0 or height <= 0:
            raise RuntimeError("拿不到屏幕尺寸")

        screen = user32.GetDC(None)
        if not screen:
            raise RuntimeError("拿不到屏幕句柄")
        memory = gdi32.CreateCompatibleDC(screen)
        bitmap = gdi32.CreateCompatibleBitmap(screen, width, height)
        old = None
        try:
            if not memory or not bitmap:
                raise RuntimeError("建不了离屏画布")
            old = gdi32.SelectObject(memory, bitmap)
            if not gdi32.BitBlt(memory, 0, 0, width, height, screen, left, top, SRCCOPY):
                raise RuntimeError("取屏幕内容失败")

            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [
                    ("biSize", wintypes.DWORD),
                    ("biWidth", ctypes.c_long),
                    ("biHeight", ctypes.c_long),
                    ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD),
                    ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long),
                    ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD),
                ]

            header = BITMAPINFOHEADER()
            header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            header.biWidth = width
            header.biHeight = -height  # 负数表示自上而下，省一次翻转
            header.biPlanes = 1
            header.biBitCount = 32
            header.biCompression = 0

            size = width * height * 4
            buffer = ctypes.create_string_buffer(size)
            got = gdi32.GetDIBits(
                memory, bitmap, 0, height, buffer, ctypes.byref(header), DIB_RGB_COLORS
            )
            if not got:
                raise RuntimeError("读像素失败")
            return width, height, bytes(buffer.raw[:size])
        finally:
            try:
                if old:
                    gdi32.SelectObject(memory, old)
                if bitmap:
                    gdi32.DeleteObject(bitmap)
                if memory:
                    gdi32.DeleteDC(memory)
            finally:
                user32.ReleaseDC(None, screen)

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)


# ---------------------------------------------------------------- OCR
class OcrProvider(ABC):
    """把图上的字读出来。默认没有引擎，如实说读不了。"""

    name = "base"

    @abstractmethod
    def is_available(self):
        """有没有可用的 OCR 引擎。"""

    @abstractmethod
    def read(self, path, region=None):
        """返回 {"ok", "text", "error"}。读不到就 ok=False，绝不编字。"""


class NullOcrProvider(OcrProvider):
    """没有装 OCR 引擎时的实现。"""

    name = "null"

    def is_available(self):
        return False

    def read(self, path, region=None):
        return {"ok": False, "text": "", "error": NO_OCR_ENGINE, "path": str(path)}

    def describe(self):
        return {"provider": self.name, "available": False, "note": NO_OCR_ENGINE}


# ---------------------------------------------------------------- 服务
class VisionService:
    """把「能不能看」「看什么」「看完了怎么记」收在一处。

    授权来自隐私中心；没有授权就什么都不做。截图只由用户手动触发。
    """

    def __init__(
        self,
        provider=None,
        ocr=None,
        allows=None,
        bus=None,
        logger=None,
        on_capture=None,
    ):
        # allows 可以传函数，也可以传隐私中心这类对象；没给就一律视为没授权。
        self.provider = provider or ScreenCaptureProvider(logger=logger)
        self.ocr = ocr or NullOcrProvider()
        self.allows = allows
        self._allows_fn = None
        if callable(allows):
            self._allows_fn = allows
        elif allows is not None:
            probe = getattr(allows, "allows", None)
            if callable(probe):
                self._allows_fn = probe
        self.bus = bus
        self.logger = logger
        self.on_capture = on_capture
        self.captures = 0
        self.last_result = None

    # ---------- 状态 ----------
    def available(self):
        try:
            return bool(self.provider.is_available())
        except Exception as exc:
            self._warn("vision availability failed: %s", exc)
            return False

    def allowed(self):
        return bool(self._allows("vision"))

    def status(self):
        """如实说明现在能不能看、缺什么。绝不发给模型。"""
        engine = self.ocr
        ocr_ok = False
        try:
            ocr_ok = bool(engine.is_available())
        except Exception:
            ocr_ok = False
        return {
            "screen": self.available(),
            "allowed": self.allowed(),
            "captures": self.captures,
            "ocr": ocr_ok,
            "note": "" if ocr_ok else NO_OCR_ENGINE,
        }

    def describe(self):
        data = self.status()
        data["model_analysis"] = False
        data["model_note"] = "当前模型通道只收文字，图还发不出去"
        return data

    # ---------- 看一眼 ----------
    def capture(self, region=None, reason="user"):
        """用户手动触发的一次截图。没授权就拒绝，理由说清楚。"""
        if not self.allowed():
            return {"ok": False, "error": "还没允许看屏幕，先到隐私设置里打开", "data": None}
        if not self.available():
            return {"ok": False, "error": "这台机器上截不了屏", "data": None}
        result = self.provider.observe("screen", region)
        self.last_result = result
        if not result.get("ok"):
            self._publish("vision.error", error=result.get("error", ""))
            return result
        self.captures += 1
        data = result.get("data") or {}
        self._publish(
            "vision.capture",
            path=data.get("path", ""),
            width=data.get("width"),
            height=data.get("height"),
            reason=str(reason or "user"),
        )
        if self.on_capture is not None:
            try:
                self.on_capture(dict(data))
            except Exception as exc:
                self._warn("vision callback failed: %s", exc)
        return result

    def read_text(self, path, region=None):
        """读图上的一段字。没有引擎时返回实话，不返回空字符串冒充没字。"""
        if not self.allowed():
            return {"ok": False, "text": "", "error": "还没允许看屏幕", "path": str(path)}
        return self.ocr.read(path, region)

    # ---------- 内部 ----------
    def _allows(self, key):
        if self._allows_fn is None:
            return False
        try:
            return bool(self._allows_fn(key))
        except Exception as exc:
            self._warn("vision permission read failed: %s", exc)
            return False

    def _publish(self, name, **payload):
        """发一条结构化事件。事件总线只收 Event，所以优先用它自己的 emit。"""
        if self.bus is None:
            return
        try:
            emit = getattr(self.bus, "emit", None)
            if callable(emit):
                emit(name, **payload)
                return
            from events.bus import Event  # 老总线只有 publish(Event) 时也能用

            self.bus.publish(Event(name=name, payload=dict(payload)))
        except Exception as exc:
            self._warn("vision event failed: %s", exc)

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)


def build_service(config=None, privacy=None, bus=None, logger=None, provider=None, ocr=None):
    """按配置装一个视觉服务。默认只截屏，OCR 保持"没有引擎"的实话。"""
    out_dir = None
    if config is not None:
        data_dir = getattr(config, "data_dir", None)
        if data_dir:
            out_dir = Path(data_dir) / CAPTURE_DIRNAME
    allows = privacy.allows if privacy is not None else None
    return VisionService(
        provider=provider or ScreenCaptureProvider(out_dir=out_dir, logger=logger),
        ocr=ocr or NullOcrProvider(),
        allows=allows,
        bus=bus,
        logger=logger,
    )