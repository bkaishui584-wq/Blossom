"""语音输出接入点的 Windows 落地实现。

用系统自带的语音组件（SAPI）在当前进程里发声：不开子进程、不碰命令行、不联网、不加第三方依赖。
说什么仍然由程序决定，模型不能直接控制发声；情绪只能作为参数传进来。

不可用时只记日志并如实说做不到，绝不抛异常，也不会让桌宠崩掉。
"""

import ctypes
import os

from voice.tts import NullSpeechProvider, SpeechProvider

# 只需要进程内组件，不需要另外起进程。
CLSCTX_INPROC_SERVER = 0x1
COINIT_APARTMENTTHREADED = 0x2

DISPATCH_METHOD = 0x1
DISPATCH_PROPERTYGET = 0x2
DISPATCH_PROPERTYPUT = 0x4
DISPID_PROPERTYPUT = -3

VT_I4 = 3
VT_BSTR = 8
VT_DISPATCH = 9

SPF_ASYNC = 1
SPF_PURGEBEFORESPEAK = 2

RUNNING_DONE = 1
RUNNING_SPEAKING = 2

# 系统语音组件的类号与 IDispatch 接口号，按名字固定，不会变。
CLSID_SPVOICE = (0x96749377, 0x3391, 0x11D2, (0x9E, 0xE3, 0x00, 0xC0, 0x4F, 0x79, 0x73, 0x96))
IID_IDISPATCH = (0x00020400, 0x0000, 0x0000, (0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46))

_FUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(values):
    return GUID(values[0], values[1], values[2], (ctypes.c_ubyte * 8)(*values[3]))


class _VariantValue(ctypes.Union):
    # 真实的 VARIANT 里还带着 16 字节的 DECIMAL，所以整个结构是 24 字节。
    # 少算这 8 字节，第二个参数就会读到越界内存，系统会回「参数类型不对」。
    _fields_ = [
        ("lVal", ctypes.c_long),
        ("bstrVal", ctypes.c_void_p),
        ("punkVal", ctypes.c_void_p),
        ("decVal", ctypes.c_ubyte * 16),
    ]


class VARIANT(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("wReserved1", ctypes.c_ushort),
        ("wReserved2", ctypes.c_ushort),
        ("wReserved3", ctypes.c_ushort),
        ("value", _VariantValue),
    ]


class DISPPARAMS(ctypes.Structure):
    _fields_ = [
        ("rgvarg", ctypes.POINTER(VARIANT)),
        ("rgdispidNamedArgs", ctypes.POINTER(ctypes.c_long)),
        ("cArgs", ctypes.c_uint),
        ("cNamedArgs", ctypes.c_uint),
    ]


class IDispatchVtbl(ctypes.Structure):
    _fields_ = [
        ("QueryInterface", _FUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))),
        ("AddRef", _FUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)),
        ("Release", _FUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)),
        ("GetTypeInfoCount", _FUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint))),
        ("GetTypeInfo", _FUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_uint, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p))),
        ("GetIDsOfNames", _FUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_wchar_p), ctypes.c_uint, ctypes.c_ulong, ctypes.POINTER(ctypes.c_long))),
        ("Invoke", _FUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_long, ctypes.POINTER(GUID), ctypes.c_ulong, ctypes.c_ushort, ctypes.POINTER(DISPPARAMS), ctypes.POINTER(VARIANT), ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint))),
    ]


class IDispatch(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.POINTER(IDispatchVtbl))]


def _clamp(value, low, high, default=0):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return max(low, min(high, default))


def _int_variant(value):
    item = VARIANT()
    item.vt = VT_I4
    item.value.lVal = int(value)
    return item


def _hex(hr):
    return "0x%08x" % (hr & 0xFFFFFFFF)


class SapiVoice:
    """当前进程里直接驱动系统语音组件。只说程序给的话，别的什么都不做。"""

    def __init__(self, ptr, oleaut32):
        self._ptr = ptr
        self._oleaut32 = oleaut32
        self._dispatch = ctypes.cast(ptr, ctypes.POINTER(IDispatch))
        self._vtbl = self._dispatch.contents.lpVtbl.contents

    # ---------- 对外 ----------
    def set_rate(self, value):
        self._put("Rate", value)

    def set_volume(self, value):
        self._put("Volume", value)

    def speak(self, text, async_=True):
        """说一句。默认异步，说完之前就返回，不挡住界面。"""
        flags = SPF_ASYNC if async_ else 0
        self._speak(text, flags)

    def stop(self):
        """闭嘴：用清空重说的方式打断当前这句。"""
        self._speak("", SPF_PURGEBEFORESPEAK)

    def speaking(self):
        """现在是不是正在出声。读不到就如实说没有。"""
        child = None
        try:
            child = self._child("Status")
            state = child._get_int("RunningState")
        except Exception:
            state = RUNNING_DONE
        finally:
            if child is not None:
                child.close()
        return state == RUNNING_SPEAKING

    def close(self):
        try:
            self._vtbl.Release(self._ptr)
        except Exception:
            pass

    # ---------- 内部 ----------
    def _speak(self, text, flags):
        token = self._alloc(text)
        try:
            self._invoke("Speak", DISPATCH_METHOD, [token, _int_variant(flags)])
        finally:
            self._oleaut32.SysFreeString(token.value.bstrVal)

    def _alloc(self, text):
        raw = self._oleaut32.SysAllocString(ctypes.c_wchar_p(text))
        if not raw:
            raise OSError("分配字符串失败")
        item = VARIANT()
        item.vt = VT_BSTR
        item.value.bstrVal = raw
        return item

    def _put(self, name, value):
        named = (ctypes.c_long * 1)(DISPID_PROPERTYPUT)
        self._invoke(name, DISPATCH_PROPERTYPUT, [_int_variant(value)], named=named)

    def _get_int(self, name):
        result = self._invoke(name, DISPATCH_PROPERTYGET, [])
        if result.vt != VT_I4:
            raise OSError("读到的不是数字")
        return result.value.lVal

    def _child(self, name):
        result = self._invoke(name, DISPATCH_PROPERTYGET, [])
        if result.vt != VT_DISPATCH or not result.value.punkVal:
            raise OSError("读到的不是一个对象")
        return SapiVoice(ctypes.c_void_p(result.value.punkVal), self._oleaut32)

    def _invoke(self, name, mode, args, named=None):
        iid = GUID()
        dispid = ctypes.c_long()
        names = (ctypes.c_wchar_p * 1)(name)
        hr = self._vtbl.GetIDsOfNames(
            self._ptr, ctypes.byref(iid), names, 1, 0, ctypes.byref(dispid)
        )
        if hr < 0:
            raise OSError("%s 不在系统语音组件里：%s" % (name, _hex(hr)))
        count = len(args)
        variants = (VARIANT * max(1, count))()
        for index, item in enumerate(args):
            variants[count - 1 - index] = item
        params = DISPPARAMS()
        params.rgvarg = ctypes.cast(variants, ctypes.POINTER(VARIANT))
        params.rgdispidNamedArgs = named
        params.cArgs = count
        params.cNamedArgs = 1 if named is not None else 0
        result = VARIANT()
        failure = ctypes.c_uint()
        hr = self._vtbl.Invoke(
            self._ptr,
            dispid,
            ctypes.byref(iid),
            0,
            mode,
            ctypes.byref(params),
            ctypes.byref(result),
            None,
            ctypes.byref(failure),
        )
        if hr < 0:
            raise OSError("%s 失败：%s" % (name, _hex(hr)))
        return result


def open_sapi_voice():
    """在当前进程里打开系统语音组件。做不到就抛异常，由调用方兜住。"""
    if os.name != "nt":
        raise OSError("当前系统不是 Windows")
    ole32 = ctypes.WinDLL("ole32")
    oleaut32 = ctypes.WinDLL("oleaut32")
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(GUID),
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    ole32.CoCreateInstance.restype = ctypes.c_long
    oleaut32.SysAllocString.argtypes = [ctypes.c_wchar_p]
    oleaut32.SysAllocString.restype = ctypes.c_void_p
    oleaut32.SysFreeString.argtypes = [ctypes.c_void_p]

    hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    if hr < 0 and hr != 0x80010106:  # 已经初始化过就不算错
        raise OSError("初始化系统组件失败：%s" % _hex(hr))

    clsid = _guid(CLSID_SPVOICE)
    iid = _guid(IID_IDISPATCH)
    raw = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(
        ctypes.byref(clsid), None, CLSCTX_INPROC_SERVER, ctypes.byref(iid), ctypes.byref(raw)
    )
    if hr < 0 or not raw:
        raise OSError("找不到系统语音组件：%s" % _hex(hr))
    return SapiVoice(raw, oleaut32)


class WindowsSpeechProvider(SpeechProvider):
    """用 Windows 自带语音发声。开了开关才会被装进来。"""

    name = "windows-speech"

    def __init__(self, rate=0, volume=100, logger=None, factory=None):
        self.rate = _clamp(rate, -10, 10)
        self.volume = _clamp(volume, 0, 100, default=100)
        self.logger = logger
        self.factory = factory or open_sapi_voice
        self.spoken = 0
        self.last_text = ""
        self.reason = ""
        self._voice = None
        self._available = None

    def is_available(self):
        if self._available is None:
            self._available = self._open()
        return self._available

    def speak(self, text, emotion=None):
        """说一句。没有语音组件、没有内容，就什么都不做并返回 False。"""
        if not self.is_available():
            return False
        line = " ".join(str(text or "").split())
        if not line:
            return False
        try:
            self._voice.stop()
            self._voice.speak(line)
        except Exception as exc:
            self._warn("speech failed: %s", exc)
            return False
        self.spoken += 1
        self.last_text = line
        return True

    def stop(self):
        if self._voice is None:
            return False
        try:
            self._voice.stop()
        except Exception as exc:
            self._warn("speech stop failed: %s", exc)
            return False
        return True

    def speaking(self):
        if self._voice is None:
            return False
        try:
            return bool(self._voice.speaking())
        except Exception:
            return False

    def describe(self):
        """开发者面板用的只读视图。绝不发给模型。"""
        available = self.is_available()
        return {
            "provider": self.name,
            "available": available,
            "speaking": self.speaking(),
            "engine": "系统自带语音" if available else (self.reason or "不可用"),
            "rate": self.rate,
            "volume": self.volume,
            "spoken": self.spoken,
            "note": "只在当前进程里说话，不开子进程、不联网、不加依赖",
        }

    # ---------- 内部 ----------
    def _open(self):
        if os.name != "nt":
            self.reason = "当前系统不是 Windows"
            return False
        try:
            voice = self.factory()
        except Exception as exc:
            self.reason = "系统语音组件不可用：%s" % exc
            return False
        if voice is None:
            self.reason = "系统语音组件不可用"
            return False
        self._voice = voice
        self.reason = ""
        try:
            self._voice.set_volume(self.volume)
            self._voice.set_rate(self.rate)
        except Exception as exc:
            self._warn("speech settings failed: %s", exc)
        return True

    def _warn(self, message, *args):
        if self.logger is None:
            return
        try:
            self.logger.warning(message, *args)
        except Exception:
            pass


def build_provider(config, logger=None, pack=None):
    """按配置装一个语音输出实现。开关关着就用空实现，绝不报错。

    选中语音包时用它的语速和音量；没选就用配置里的。
    语音包里的 voice 名字只记录备查，当前引擎用系统默认发音。
    """
    if pack is None and not getattr(config, "voice_output", False):
        return NullSpeechProvider()
    rate = getattr(config, "voice_rate", 0)
    volume = getattr(config, "voice_volume", 100)
    if pack is not None:
        rate = getattr(pack, "rate", rate)
        volume = getattr(pack, "volume", volume)
    return WindowsSpeechProvider(rate=rate, volume=volume, logger=logger)
