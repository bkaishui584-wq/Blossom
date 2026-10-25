"""真实音频采集：把麦克风里的声音录成一段 WAV 文件。

只用 Windows 自带的多媒体接口（winmm 的 mciSendString）与标准库，不下载任何音频库。
没麦克风、不是 Windows、或者权限关着时，都保持不可用并说明原因，不影响打字聊天。

只在用户明确按下录音时采一段，不做后台常驻采集，不上传音频，也不碰语音包里的文件。
"""

import os
import shutil
import time
import math
from abc import ABC, abstractmethod
from pathlib import Path

MIN_SECONDS = 0.2
MAX_SECONDS = 60.0
DEFAULT_SECONDS = 5.0
DEFAULT_RATE = 16000
DEFAULT_CHANNELS = 1
DEFAULT_BITS = 16

MCI_MAX = 512


def signal_level(samples):
    """返回一段单声道采样的峰值和 RMS。没有采样时保持 0，不编造音量。"""
    peak = 0.0
    total = 0.0
    count = 0
    try:
        for item in samples or ():
            value = abs(float(item))
            peak = max(peak, min(1.0, value))
            total += value * value
            count += 1
    except Exception:
        return {"peak": 0.0, "rms": 0.0, "db": -120.0, "percent": 0}
    rms = math.sqrt(total / count) if count else 0.0
    db = 20.0 * math.log10(max(rms, 1e-6))
    return {
        "peak": round(peak, 4),
        "rms": round(rms, 4),
        "db": round(db, 1),
        "percent": int(round(peak * 100)),
    }


class AudioCaptureProvider(ABC):
    """采一段音频的接入点。没有设备时保持不可用，不抛异常。"""

    name = "base"

    @abstractmethod
    def is_available(self):
        """现在能不能采到声音。"""

    @abstractmethod
    def record(self, seconds=None, path=None):
        """录一段，返回 {ok, path, seconds, bytes, error}。"""

    def describe(self):
        return {"provider": self.name, "available": False, "note": "没有接入音频采集"}


class NullCapture(AudioCaptureProvider):
    """没有麦克风时的默认实现：什么都不录，只说明原因。"""

    name = "null"

    def __init__(self, reason=""):
        self.reason = str(reason or "没有接入音频采集")

    def is_available(self):
        return False

    def record(self, seconds=None, path=None):
        return {"ok": False, "error": self.reason, "path": None, "seconds": 0.0, "bytes": 0}

    def describe(self):
        return {"provider": self.name, "available": False, "note": self.reason}


def clamp_seconds(seconds):
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        value = DEFAULT_SECONDS
    return max(MIN_SECONDS, min(MAX_SECONDS, value))


def default_capture_path(config=None):
    """录音落盘位置。优先放进数据目录，拿不到就用系统临时目录。"""
    base = ""
    for name in ("recordings_dir", "data_dir", "data_root"):
        candidate = getattr(config, name, "") if config is not None else ""
        if candidate:
            base = Path(str(candidate))
            break
    if not base:
        import tempfile

        base = Path(tempfile.gettempdir())
    elif base.name != "recordings":
        base = base / "recordings"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception:
        import tempfile

        base = Path(tempfile.gettempdir())
    return base / time.strftime("mic-%Y%m%d-%H%M%S.wav")


class MciCapture(AudioCaptureProvider):
    """用 Windows 自带的 MCI 录音。录完直接落一个 WAV 文件。"""

    name = "mci"

    def __init__(self, logger=None, rate=DEFAULT_RATE, channels=DEFAULT_CHANNELS):
        self.logger = logger
        self.rate = int(rate or DEFAULT_RATE)
        self.channels = int(channels or DEFAULT_CHANNELS)
        self._alias = "deskpet_rec"
        # 上一次录音失败的真实原因（系统原话），界面据此如实显示，不吞掉。
        self.last_error = ""

    # ---------- 探测 ----------
    def devices(self):
        """系统里能用的录音设备数量。探测失败当作 0。"""
        if os.name != "nt":
            return 0
        try:
            import ctypes

            return int(ctypes.windll.winmm.waveInGetNumDevs())
        except Exception as exc:
            self._warn("waveInGetNumDevs failed: %s", exc)
            return 0

    def is_available(self):
        return os.name == "nt" and self.devices() > 0

    def describe(self):
        available = self.is_available()
        if os.name != "nt":
            note = "只有 Windows 支持录音"
        elif not available:
            note = "这台机器没有可用的麦克风"
        elif self.last_error:
            note = f"设备在，但上次录音失败：{self.last_error}"
        else:
            note = f"可以录音（{self.devices()} 个设备）"
        return {
            "provider": self.name,
            "available": available,
            "devices": self.devices(),
            "note": note,
            "last_error": self.last_error,
        }

    # ---------- 录音 ----------
    def record(self, seconds=None, path=None):
        if not self.is_available():
            return {
                "ok": False,
                "error": self.describe()["note"],
                "path": None,
                "seconds": 0.0,
                "bytes": 0,
            }
        length = clamp_seconds(seconds)
        target = Path(str(path)) if path else default_capture_path(None)
        staged = self._stage_path()
        self._close()
        opened = False
        try:
            opened, error = self._command(f"open new type waveaudio alias {self._alias}")
            if not opened:
                return self._failed(f"打不开录音设备：{error}", staged)
            for command in (
                f"set {self._alias} time format milliseconds",
                f"set {self._alias} channels {self.channels}",
                f"set {self._alias} samplespersec {self.rate}",
                f"set {self._alias} bitspersample {DEFAULT_BITS}",
            ):
                self._command(command)
            started, error = self._command(f"record {self._alias}")
            if not started:
                return self._failed(f"设备不接受录音命令：{error}", staged)
            time.sleep(length)
            self._command(f"stop {self._alias}")
            saved, error = self._command(f'save {self._alias} "{staged}"')
            if not saved:
                return self._failed(f"保存录音失败：{error}", staged)
        except Exception as exc:
            self._warn("recording failed: %s", exc)
            return self._failed(f"录音出错了：{exc}", staged)
        finally:
            self._close()
        return self._finish(staged, target, length)

    # ---------- 内部 ----------
    def _stage_path(self):
        """先写进文件名没有空格的临时目录，MCI 对路径比较挑。"""
        import tempfile

        return Path(tempfile.gettempdir()) / f"{self._alias}-{int(time.time() * 1000)}.wav"

    def _finish(self, staged, target, length):
        try:
            size = staged.stat().st_size
        except OSError:
            size = 0
        if not size:
            self._drop(staged)
            return {
                "ok": False,
                "error": "没有录到声音（设备可能静音或被占用）",
                "path": None,
                "seconds": 0.0,
                "bytes": 0,
            }
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(staged, target)
        except Exception as exc:
            self._warn("copy recording failed: %s", exc)
            self._drop(staged)
            return {
                "ok": False,
                "error": f"录音存不下来：{exc}",
                "path": str(staged),
                "seconds": length,
                "bytes": size,
            }
        self._drop(staged)
        return {"ok": True, "path": str(target), "seconds": length, "bytes": size}

    def _failed(self, message, staged):
        self.last_error = str(message)
        self._drop(staged)
        return {"ok": False, "error": message, "path": None, "seconds": 0.0, "bytes": 0}

    def _drop(self, path):
        try:
            Path(path).unlink()
        except Exception:
            pass

    def _close(self):
        try:
            self._command(f"close {self._alias}")
        except Exception:
            pass

    def _command(self, text):
        """发一条 MCI 命令，返回 (是否成功, 提示文本)。"""
        try:
            import ctypes

            buffer = ctypes.create_unicode_buffer(MCI_MAX)
            code = int(
                ctypes.windll.winmm.mciSendStringW(
                    ctypes.c_wchar_p(text), buffer, MCI_MAX, None
                )
            )
        except Exception as exc:
            return False, str(exc)
        if code == 0:
            return True, buffer.value
        return False, self._error_text(code)

    def _error_text(self, code):
        try:
            import ctypes

            buffer = ctypes.create_unicode_buffer(MCI_MAX)
            if ctypes.windll.winmm.mciGetErrorStringW(code, buffer, MCI_MAX):
                return buffer.value or f"MCI 错误 {code}"
        except Exception:
            pass
        return f"MCI 错误 {code}"

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)


def _allows_fn(allows):
    """权限检查既接受回调，也接受带 allows(key) 的权限中心对象。"""
    if callable(allows):
        return allows
    probe = getattr(allows, "allows", None) if allows is not None else None
    return probe if callable(probe) else None


def build_capture(config=None, logger=None, allows=None, provider=None):
    """挑一个能用的录音实现。不可用时返回说明原因的 Null 实现。"""
    if provider is not None:
        return provider
    try:
        check = _allows_fn(allows)
        if check is not None and not check("listen"):
            return NullCapture("权限页里「能不能听」是关着的")
        if os.name != "nt":
            return NullCapture("只有 Windows 支持录音")
        candidate = MciCapture(logger=logger)
        if not candidate.is_available():
            return NullCapture("这台机器没有可用的麦克风")
        return candidate
    except Exception as exc:
        if logger:
            logger.warning("capture probe failed: %s", exc)
        return NullCapture("音频采集探测失败")


class AudioStreamProvider(ABC):
    """连续音频接入点。只在用户手动开始后读取，不默认常驻。"""

    name = "stream"

    @abstractmethod
    def sources(self):
        """返回可选音源，每项至少含 id、name、kind。"""

    @abstractmethod
    def start(self, source_id=""):
        """开始读取指定音源。"""

    @abstractmethod
    def read(self, timeout=0.2):
        """读取一小段单声道 float32 音频，采样率见 sample_rate。"""

    @abstractmethod
    def pause(self):
        """暂停读取并关闭当前音频流。"""

    @abstractmethod
    def resume(self):
        """恢复上一次选定的音源。"""

    @abstractmethod
    def stop(self):
        """停止并释放音频设备。"""

    @abstractmethod
    def describe(self):
        """给界面看的只读状态。"""

    sample_rate = DEFAULT_RATE

    def probe(self, source_id="", seconds=0.8):
        """短时测试音源；默认实现明确说明不支持。"""
        return {"ok": False, "error": "当前音源不支持测试", "level": signal_level([])}


class NullStreamCapture(AudioStreamProvider):
    """没有流式采集能力时保持安静，并说明原因。"""

    name = "null-stream"
    sample_rate = DEFAULT_RATE

    def __init__(self, reason="没有接入实时音频采集"):
        self.reason = str(reason or "没有接入实时音频采集")

    def sources(self):
        return []

    def start(self, source_id=""):
        return False

    def read(self, timeout=0.2):
        return None

    def pause(self):
        return False

    def resume(self):
        return False

    def stop(self):
        return False

    def describe(self):
        return {
            "provider": self.name,
            "available": False,
            "sources": [],
            "note": self.reason,
            "level": signal_level([]),
        }


class SoundCardStreamCapture(AudioStreamProvider):
    """用可选的 soundcard 包读取麦克风或 WASAPI 系统音频回环。

    没有安装 soundcard 时保持不可用，不自动下载，也不安装虚拟声卡。
    """

    name = "soundcard"
    sample_rate = DEFAULT_RATE

    def __init__(self, logger=None):
        self.logger = logger
        self.last_error = ""
        self._source_id = ""
        self._source_name = ""
        self._source_kind = ""
        self._recorder = None
        self._rate = DEFAULT_RATE
        self._channels = 1
        self._paused = False
        self.last_level = signal_level([])

    def _module(self):
        import soundcard

        return soundcard

    def sources(self):
        try:
            soundcard = self._module()
            rows = []
            for mic in soundcard.all_microphones(include_loopback=True):
                loopback = bool(getattr(mic, "isloopback", False))
                rows.append(
                    {
                        "id": str(getattr(mic, "id", "") or ""),
                        "name": str(getattr(mic, "name", "") or "未命名音源"),
                        "kind": "system" if loopback else "microphone",
                    }
                )
            self.last_error = ""
            return rows
        except Exception as exc:
            self.last_error = "未安装可选组件 soundcard，系统音频采集不可用：%s" % exc
            return []

    def start(self, source_id=""):
        self.stop()
        try:
            soundcard = self._module()
            mic = soundcard.get_microphone(str(source_id or ""), include_loopback=True)
        except Exception as exc:
            self.last_error = "打不开音频设备：%s" % exc
            self._warn("stream source open failed: %s", exc)
            return False
        self._source_id = str(source_id or getattr(mic, "id", "") or "")
        self._source_name = str(getattr(mic, "name", "") or "未命名音源")
        self._source_kind = "system" if getattr(mic, "isloopback", False) else "microphone"
        self._paused = False
        if not self._open(mic):
            self.stop()
            return False
        return True

    def _open(self, mic):
        if self._source_kind == "system":
            choices = ((48000, 2), (16000, 2), (48000, 1), (16000, 1))
        else:
            choices = ((16000, 1), (48000, 1), (16000, 2), (48000, 2))
        last_error = None
        for rate, channels in choices:
            recorder = None
            try:
                recorder = mic.recorder(
                    samplerate=rate,
                    channels=channels,
                    blocksize=max(1, int(rate * 0.05)),
                )
                recorder.__enter__()
                self._recorder = recorder
                self._rate = rate
                self._channels = channels
                self.last_error = ""
                return True
            except Exception as exc:
                last_error = exc
                if recorder is not None:
                    try:
                        recorder.__exit__(None, None, None)
                    except Exception:
                        pass
        self.last_error = "音频设备不接受录音格式：%s" % last_error
        self._warn("stream recorder open failed: %s", last_error)
        return False

    def read(self, timeout=0.2):
        if self._recorder is None or self._paused:
            return None
        try:
            import numpy

            seconds = max(0.05, min(float(timeout or 0.2), 0.5))
            frames = max(1, int(self._rate * seconds))
            data = self._recorder.record(numframes=frames)
            if data is None or len(data) == 0:
                return None
            samples = numpy.asarray(data, dtype="float32")
            if samples.ndim > 1:
                samples = samples.mean(axis=1)
            samples = samples.reshape(-1)
            if self._rate != DEFAULT_RATE and samples.size:
                target = max(1, int(round(samples.size * DEFAULT_RATE / self._rate)))
                source_axis = numpy.arange(samples.size, dtype="float32")
                target_axis = numpy.linspace(0, samples.size - 1, target)
                samples = numpy.interp(target_axis, source_axis, samples).astype("float32")
            self.last_error = ""
            self.last_level = signal_level(samples)
            return samples
        except Exception as exc:
            self.last_error = "读取音频失败：%s" % exc
            self._warn("stream read failed: %s", exc)
            return None

    def pause(self):
        self._paused = True
        self._close_recorder()
        return True

    def resume(self):
        if self._paused and self._source_id:
            try:
                soundcard = self._module()
                mic = soundcard.get_microphone(self._source_id, include_loopback=True)
                if self._open(mic):
                    self._paused = False
                    return True
            except Exception as exc:
                self.last_error = "恢复音频设备失败：%s" % exc
        return False

    def stop(self):
        self._close_recorder()
        self._source_id = ""
        self._source_name = ""
        self._source_kind = ""
        self._paused = False
        self.last_level = signal_level([])
        return True

    def probe(self, source_id="", seconds=0.8):
        """用户主动测试音源：短时读取，算真实峰值，结束后立即释放设备。"""
        length = max(0.2, min(float(seconds or 0.8), 3.0))
        if not self.start(source_id):
            return {
                "ok": False,
                "error": self.last_error or "音频设备打不开",
                "level": signal_level([]),
            }
        best = signal_level([])
        read_any = False
        try:
            end = time.monotonic() + length
            while time.monotonic() < end:
                samples = self.read(timeout=min(0.12, length))
                if samples is None:
                    continue
                read_any = True
                value = signal_level(samples)
                if value["peak"] > best["peak"]:
                    best = value
        except Exception as exc:
            return {"ok": False, "error": "测试音源失败：%s" % exc, "level": best}
        finally:
            self.stop()
        if not read_any:
            return {
                "ok": False,
                "error": self.last_error or "没有读到音频（设备可能静音或被占用）",
                "level": best,
            }
        return {
            "ok": True,
            "error": "",
            "level": best,
            "note": "音量 %d%%" % best["percent"]
            if best["percent"] > 0
            else "设备能读，但当前没有声音",
        }

    def describe(self):
        rows = self.sources()
        note = ""
        if self.last_error:
            note = self.last_error
        elif not rows:
            note = "没有可用的麦克风或系统音频回环"
        else:
            note = "可用（%d 个音源）" % len(rows)
        return {
            "provider": self.name,
            "available": bool(rows),
            "sources": rows,
            "note": note,
            "last_error": self.last_error,
            "running": self._recorder is not None,
            "source": self._source_name,
            "level": self.last_level,
        }

    def _close_recorder(self):
        recorder = self._recorder
        self._recorder = None
        if recorder is None:
            return
        try:
            recorder.__exit__(None, None, None)
        except Exception:
            pass

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)


def build_stream_capture(logger=None, provider=None):
    """返回流式采集实现。soundcard 未安装时也返回真实对象，让界面显示原因。"""
    if provider is not None:
        return provider
    return SoundCardStreamCapture(logger=logger)
