"""语音识别接入点（STT）：接口、引擎探测与按需接入的实现。

不接真实识别引擎，也不采集麦克风。以后用户准备好的识别能力实现 transcribe 就能接进来：
本地识别和云端识别都只是「音频进、文字出」，换一个不影响其它部分。

和形象、发声一样：识别结果由程序处理，模型不能伪造识别内容。
没有识别引擎时保持不可用，不影响打字聊天。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


class SpeechToTextProvider(ABC):
    """语音识别接入点。本地 STT 与云端 STT 都实现这一套方法。"""

    name = "base"

    @abstractmethod
    def is_available(self):
        """现在能不能识别。"""

    @abstractmethod
    def transcribe(self, audio, sample_rate=None, language=None):
        """把一段音频转成文字。识别不出来时返回空字符串。"""

    @abstractmethod
    def stop(self):
        """停止识别。"""


class NullSttProvider(SpeechToTextProvider):
    """没有识别引擎时的默认实现：永远不可用，也不报错。"""

    name = "null"

    def is_available(self):
        return False

    def transcribe(self, audio, sample_rate=None, language=None):
        return ""

    def stop(self):
        return False

    def describe(self):
        return {
            "provider": self.name,
            "available": False,
            "note": "没有接入语音识别",
        }


# ---------- 引擎探测 ----------
# 只查本机装没装识别引擎，不下载、不安装、不联网。装了就用，没装就如实报不可用。
# 引擎名 -> 说明。用户自己 pip 装上对应包并配好模型，这里就能用。
ENGINES = {
    "sherpa_onnx": "离线识别（需要 sherpa-onnx 包与本地模型目录）",
    "vosk": "离线识别（需要 vosk 包与本地模型目录）",
    "faster_whisper": "离线识别（需要 faster-whisper 包）",
    "whisper": "离线识别（需要 openai-whisper 包）",
    "speech_recognition": "通用识别（需要 SpeechRecognition 包）",
}


def _module_present(name):
    """这个包现在能不能 import。探测本身不该抛异常。"""
    try:
        import importlib.util

        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def probe_engines():
    """逐个查本机有哪些识别引擎可用。全部缺失时返回空表。"""
    return {name: _module_present(name) for name in ENGINES}


def pcm16(audio):
    """把音频统一成 16 位小端 PCM 字节。

    接受原始 PCM 字节，也接受 WAV 文件路径；别的类型返回空串。
    """
    if isinstance(audio, (bytes, bytearray)):
        return bytes(audio)
    text = str(audio or "")
    if not text:
        return b""
    try:
        import wave
        from pathlib import Path

        if not Path(text).is_file():
            return b""
        with wave.open(text, "rb") as handle:
            if handle.getsampwidth() != 2:
                return b""
            return handle.readframes(handle.getnframes())
    except Exception:
        return b""


class VoskSttProvider(SpeechToTextProvider):
    """离线识别：vosk 包 + 本地模型目录。缺任一样都保持不可用。"""

    name = "vosk"

    def __init__(self, model_path="", logger=None, sample_rate=16000):
        self.model_path = str(model_path or "")
        self.logger = logger
        self.sample_rate = int(sample_rate or 16000)
        self._model = None
        self._failed = ""

    def is_available(self):
        if not self.model_path:
            return False
        from pathlib import Path

        if not Path(self.model_path).is_dir():
            return False
        if not _module_present("vosk"):
            return False
        return not self._failed

    def transcribe(self, audio, sample_rate=None, language=None):
        model = self._load()
        if model is None:
            return ""
        data = pcm16(audio)
        if not data:
            return ""
        try:
            import json

            import vosk

            recognizer = vosk.KaldiRecognizer(
                model, int(sample_rate or self.sample_rate)
            )
            recognizer.AcceptWaveform(data)
            heard = json.loads(recognizer.FinalResult() or "{}")
            return str(heard.get("text") or "").strip()
        except Exception as exc:
            self._warn("vosk transcribe failed: %s", exc)
            return ""

    def stop(self):
        return True

    def describe(self):
        return {
            "provider": self.name,
            "available": self.is_available(),
            "model": self.model_path or None,
            "note": self._failed or ("" if self.is_available() else "缺少 vosk 包或模型目录"),
        }

    # ---------- 内部 ----------
    def _load(self):
        if self._model is not None or self._failed:
            return self._model
        try:
            import vosk

            self._model = vosk.Model(self.model_path)
        except Exception as exc:
            self._failed = f"模型加载失败：{exc}"
            self._warn("vosk model load failed: %s", exc)
        return self._model

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)


# ---------- sherpa-onnx：离线识别 ----------
# 模型要用户自备，程序不下载。三种常见布局按目录名和文件名认，认不出就逐个试。
SHERPA_KINDS = ("sense_voice", "paraformer", "whisper")
KIND_HINTS = (
    ("sense", "sense_voice"),
    ("paraformer", "paraformer"),
    ("whisper", "whisper"),
)


def _numpy():
    """sherpa-onnx 的音频接口要 float32 数组；没有 numpy 就如实说不行。"""
    try:
        import numpy

        return numpy
    except Exception:
        return None


def float32_audio(audio, sample_rate=None):
    """把输入统一成 float32 单声道数组。接受 PCM16 字节，也接受 WAV 路径。"""
    numpy = _numpy()
    if numpy is None:
        return None
    data = pcm16(audio)
    if not data:
        return None
    if len(data) % 2:
        data = data[:-1]
    counts = numpy.frombuffer(data, dtype="<i2").astype("float32")
    return counts / 32768.0


def model_files(model_path):
    """列出模型目录里的文件；路径不存在或不合法时返回空表。"""
    from pathlib import Path

    if not model_path:
        return []
    target = Path(model_path)
    if target.is_file():
        return [target]
    if not target.is_dir():
        return []
    return sorted(item for item in target.iterdir() if item.is_file())


def streaming_model_status(model_path="", kind="auto"):
    """在线识别模型的状态，只读文件，不加载模型、不下载。

    实时字幕用的是流式模型，和离线 SenseVoice / Whisper 不是一回事。这里把
    「没选目录、目录不存在、文件不全、运行库没装、可以启动」分开报，界面不能
    把装了 sherpa-onnx 误写成离线识别已经可用。
    """
    from pathlib import Path

    path = str(model_path or "").strip()
    wanted = str(kind or "auto").strip().lower().replace("-", "_")
    has_runtime = _module_present("sherpa_onnx")
    has_numpy = _numpy() is not None
    runtime_ready = bool(has_runtime and has_numpy)
    result = {
        "path": path,
        "state": "no_model",
        "ready": False,
        "runtime_ready": runtime_ready,
        "model_ready": False,
        "kind": wanted,
        "note": "",
    }

    def missing_runtime_note():
        parts = []
        if not has_runtime:
            parts.append("sherpa-onnx 运行库")
        if not has_numpy:
            parts.append("numpy")
        return "；另外还没装：%s" % "、".join(parts) if parts else ""

    if not path:
        result["note"] = "还没选择在线识别模型目录；模型不会自动下载。" + missing_runtime_note()
        return result

    target = Path(path)
    try:
        if not target.exists():
            result["state"] = "path_missing"
            result["note"] = "模型目录不存在：%s" % path + missing_runtime_note()
            return result
        if not target.is_dir():
            result["state"] = "path_not_directory"
            result["note"] = "这里不是模型目录：%s" % path + missing_runtime_note()
            return result
        files = sorted(item for item in target.iterdir() if item.is_file())
    except OSError as exc:
        result["state"] = "unreadable"
        result["note"] = "模型目录读不出来：%s" % exc + missing_runtime_note()
        return result

    if not files:
        result["state"] = "empty"
        result["note"] = "模型目录是空的。" + missing_runtime_note()
        return result

    names = {item.name.lower(): item for item in files}
    tokens = names.get("tokens.txt")
    if tokens is None:
        result["state"] = "missing_tokens"
        result["note"] = "模型目录里没有 tokens.txt。" + missing_runtime_note()
        return result

    models = [item for item in files if item.suffix.lower() == ".onnx"]
    if not models:
        result["state"] = "missing_onnx"
        result["note"] = "模型目录里没有 .onnx 模型文件。" + missing_runtime_note()
        return result

    if wanted in ("sense_voice", "whisper"):
        result["state"] = "incompatible_kind"
        result["note"] = (
            "实时字幕要流式模型（transducer 或 paraformer），"
            "SenseVoice / Whisper 的离线模型不能用来做在线识别。"
            + missing_runtime_note()
        )
        return result
    if wanted not in ("auto", "", "transducer", "paraformer"):
        result["state"] = "invalid_kind"
        result["note"] = "不认识的在线识别模型类型：%s" % wanted + missing_runtime_note()
        return result

    encoder = next((item for item in models if "encoder" in item.name.lower()), None)
    decoder = next((item for item in models if "decoder" in item.name.lower()), None)
    joiner = next((item for item in models if "joiner" in item.name.lower()), None)
    if encoder is None or decoder is None:
        result["state"] = "missing_encoder_decoder"
        result["note"] = (
            "流式模型目录要有 encoder 和 decoder 两个 .onnx 文件。"
            + missing_runtime_note()
        )
        return result
    if wanted == "transducer" and joiner is None:
        result["state"] = "missing_joiner"
        result["note"] = "transducer 模型还缺 joiner .onnx 文件。" + missing_runtime_note()
        return result

    picked = wanted if wanted in ("transducer", "paraformer") else (
        "transducer" if joiner is not None else "paraformer"
    )
    needed = [tokens, encoder, decoder] + ([joiner] if picked == "transducer" else [])
    try:
        if any(item.stat().st_size <= 0 for item in needed):
            result["state"] = "empty_file"
            result["note"] = "模型目录里有空文件，文件可能没下完。" + missing_runtime_note()
            return result
    except OSError as exc:
        result["state"] = "unreadable"
        result["note"] = "模型文件读不出来：%s" % exc + missing_runtime_note()
        return result

    result["model_ready"] = True
    result["kind"] = picked
    if runtime_ready:
        result["state"] = "ready"
        result["ready"] = True
        result["note"] = (
            "运行库和 %s 模型文件都齐了；开始字幕时会真正加载模型。" % picked
        )
    else:
        result["state"] = "runtime_missing"
        result["note"] = (
            "模型文件齐全，但还缺%s；在「能力包」里装 voice 包后重启再试。"
            % ("sherpa-onnx 运行库" if not has_runtime else "numpy")
        )
    return result


class SherpaSttProvider(SpeechToTextProvider):
    """离线识别：sherpa-onnx 包 + 本地模型目录。缺任一样都保持不可用。

    支持的模型布局（都要求 tokens.txt）：
      whisper      <名字>-encoder.onnx + <名字>-decoder.onnx
      sense_voice  model.onnx 或 model.int8.onnx
      paraformer   model.onnx 或 model.int8.onnx

    认得出就直接用；认不出按 sense_voice -> paraformer -> whisper 逐个试，
    成功哪个就在状态里报哪个，不猜也不假装。
    """

    name = "sherpa-onnx"

    def __init__(
        self, model_path="", logger=None, sample_rate=16000, kind="auto", language=""
    ):
        self.model_path = str(model_path or "")
        self.logger = logger
        self.sample_rate = int(sample_rate or 16000)
        self.kind = str(kind or "auto").strip().lower().replace("-", "_")
        self.language = str(language or "").strip()
        self._recognizer = None
        self._failed = ""
        self.picked = ""

    # ---------- 状态 ----------
    def is_available(self):
        if self._failed or _numpy() is None or not _module_present("sherpa_onnx"):
            return False
        return self._plan() is not None

    def describe(self):
        plan = self._plan()
        return {
            "provider": self.name,
            "available": self.is_available(),
            "model": self.model_path or None,
            "kind": self.picked or (plan or ("", {}))[0],
            "note": self._failed or ("" if plan else self._missing_note()),
        }

    def stop(self):
        return True

    # ---------- 识别 ----------
    def transcribe(self, audio, sample_rate=None, language=None):
        recognizer = self._build()
        if recognizer is None:
            return ""
        rate = int(sample_rate or self.sample_rate)
        samples = float32_audio(audio, rate)
        if samples is None or samples.size == 0:
            return ""
        try:
            stream = recognizer.create_stream()
            stream.accept_waveform(rate, samples)
            recognizer.decode_stream(stream)
            return str(stream.result.text or "").strip()
        except Exception as exc:
            self._warn("sherpa transcribe failed: %s", exc)
            return ""

    # ---------- 内部 ----------
    def _files(self):
        return model_files(self.model_path)

    def _missing_note(self):
        files = self._files()
        if not files:
            return "没配模型目录，或者目录不存在"
        names = {item.name.lower() for item in files}
        if "tokens.txt" not in names:
            return "模型目录里没有 tokens.txt"
        if not any(item.suffix.lower() == ".onnx" for item in files):
            return "模型目录里没有 .onnx 模型文件"
        return "模型目录里的文件凑不出认识的布局（whisper / sense_voice / paraformer）"

    def _guess(self, files):
        blob = (self.model_path + " " + " ".join(item.name for item in files)).lower()
        for hint, kind in KIND_HINTS:
            if hint in blob:
                return kind
        return ""

    def _plan(self):
        """返回 (类型, 建识别器要的参数)；凑不出来返回 None。"""
        files = self._files()
        if not files:
            return None
        by_name = {item.name.lower(): item for item in files}
        tokens = by_name.get("tokens.txt")
        if tokens is None:
            return None
        models = [item for item in files if item.suffix.lower() == ".onnx"]
        encoder = next((i for i in models if "encoder" in i.name.lower()), None)
        decoder = next((i for i in models if "decoder" in i.name.lower()), None)
        single = next(
            (i for i in models if i.name.lower().startswith("model")), None
        ) or (models[0] if len(models) == 1 else None)
        self.picked = self.kind if self.kind in SHERPA_KINDS else self._guess(files)
        order = [kind for kind in [self.picked] if kind]
        order += [kind for kind in SHERPA_KINDS if kind not in order]
        for kind in order:
            if kind == "whisper":
                if encoder is not None and decoder is not None:
                    return (
                        "whisper",
                        {
                            "encoder": str(encoder),
                            "decoder": str(decoder),
                            "tokens": str(tokens),
                        },
                    )
            elif kind == "paraformer" and single is not None:
                return (
                    "paraformer",
                    {"paraformer": str(single), "tokens": str(tokens)},
                )
            elif kind == "sense_voice" and single is not None:
                return ("sense_voice", {"model": str(single), "tokens": str(tokens)})
        return None

    def _build(self):
        if self._recognizer is not None or self._failed:
            return self._recognizer
        plan = self._plan()
        if plan is None:
            self._failed = self._missing_note()
            return None
        if _numpy() is None:
            self._failed = "缺 numpy，音频转换不了"
            return None
        try:
            import sherpa_onnx
        except Exception as exc:
            self._failed = "sherpa-onnx 包没装：%s" % exc
            return None
        kind, kwargs = plan
        try:
            if kind == "whisper":
                self._recognizer = sherpa_onnx.OfflineRecognizer.from_whisper(
                    num_threads=2,
                    sample_rate=self.sample_rate,
                    language=self.language or "en",
                    **kwargs,
                )
            elif kind == "paraformer":
                self._recognizer = sherpa_onnx.OfflineRecognizer.from_paraformer(
                    num_threads=2, sample_rate=self.sample_rate, **kwargs
                )
            else:
                self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                    num_threads=2,
                    sample_rate=self.sample_rate,
                    language=self.language or "auto",
                    **kwargs,
                )
            self.picked = kind
        except Exception as exc:
            self._failed = "模型加载失败：%s" % exc
            self._warn("sherpa model load failed: %s", exc)
        return self._recognizer

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)


def build_stt(config=None, logger=None, provider=None):
    """按配置挑一个识别实现。没有可用引擎时返回 Null，并说明为什么。"""
    if provider is not None:
        return provider
    try:
        engines = probe_engines()
        model = str(getattr(config, "stt_model", "") or "")
        kind = str(getattr(config, "stt_kind", "") or "auto")
        language = str(getattr(config, "stt_language", "") or "")
        wanted = str(getattr(config, "stt_engine", "") or "").strip().lower().replace("-", "_")
        if wanted and wanted not in engines:
            return NullSttProvider()
        if engines.get("sherpa_onnx") or wanted == "sherpa_onnx":
            if model:
                candidate = SherpaSttProvider(
                    model, logger=logger, kind=kind, language=language
                )
                if candidate.is_available():
                    return candidate
        if engines.get("vosk") or wanted == "vosk":
            if model:
                candidate = VoskSttProvider(model, logger=logger)
                if candidate.is_available():
                    return candidate
        return NullSttProvider()
    except Exception as exc:
        if logger:
            logger.warning("stt probe failed: %s", exc)
        return NullSttProvider()


def describe_engines(config=None):
    """开发者面板用的一句话：装了哪些引擎、配没配模型。"""
    engines = probe_engines()
    installed = [name for name, ok in engines.items() if ok]
    model = str(getattr(config, "stt_model", "") or "")
    # 需要模型目录的引擎：光装包不算能用，还得配上模型。
    needs_model = {"sherpa_onnx", "vosk"}
    ready = bool(installed) and (
        bool(model) or any(name not in needs_model for name in installed)
    )
    return {
        "engines": engines,
        "installed": installed,
        "model": model or None,
        "ready": ready,
    }


@dataclass
class AsrUpdate:
    """在线识别的中间结果或最终句子。"""

    text: str = ""
    is_final: bool = False


class StreamingSttProvider(ABC):
    """连续音频识别接口。实时字幕只依赖这里，不依赖具体引擎。"""

    name = "stream-base"
    sample_rate = 16000
    last_error = ""

    @abstractmethod
    def is_available(self):
        """模型和依赖都齐了才算可用。"""

    @abstractmethod
    def start(self):
        """开始一个新句子流。"""

    @abstractmethod
    def accept(self, audio, sample_rate=None):
        """接收一段音频，返回零到多条 AsrUpdate。"""

    @abstractmethod
    def finish(self):
        """结束当前输入并返回最后一条结果。"""

    @abstractmethod
    def stop(self):
        """停止并释放识别器。"""

    @abstractmethod
    def describe(self):
        """给界面看的只读状态。"""


class NullStreamingSttProvider(StreamingSttProvider):
    """没有在线识别模型时保持不可用，并说明原因。"""

    name = "null-stream"

    def __init__(self, reason="没有可用的在线识别模型"):
        self.reason = str(reason or "没有可用的在线识别模型")

    def is_available(self):
        return False

    def start(self):
        return False

    def accept(self, audio, sample_rate=None):
        return []

    def finish(self):
        return None

    def stop(self):
        return False

    def describe(self):
        return {
            "provider": self.name,
            "available": False,
            "note": self.reason,
        }


class SherpaOnlineSttProvider(StreamingSttProvider):
    """sherpa-onnx 在线识别。支持 transducer 与 paraformer 在线模型。

    SenseVoice / Whisper 的离线模型不能冒充在线模型，这里会明确报不适用。
    """

    name = "sherpa-onnx-online"
    sample_rate = 16000

    def __init__(
        self, model_path="", logger=None, kind="auto", language="", num_threads=2
    ):
        self.model_path = str(model_path or "")
        self.logger = logger
        self.kind = str(kind or "auto").strip().lower().replace("-", "_")
        self.language = str(language or "").strip()
        try:
            self.num_threads = max(
                1, min(16, int(2 if num_threads is None else num_threads))
            )
        except (TypeError, ValueError):
            self.num_threads = 2
        self._recognizer = None
        self._stream = None
        self._failed = ""
        self.last_error = ""
        self.picked = ""

    def _plan(self):
        files = model_files(self.model_path)
        if not files:
            return None
        by_name = {item.name.lower(): item for item in files}
        tokens = by_name.get("tokens.txt")
        if tokens is None:
            return None
        models = [item for item in files if item.suffix.lower() == ".onnx"]
        encoder = next((item for item in models if "encoder" in item.name.lower()), None)
        decoder = next((item for item in models if "decoder" in item.name.lower()), None)
        joiner = next((item for item in models if "joiner" in item.name.lower()), None)
        if encoder is None or decoder is None:
            return None
        if self.kind in ("", "auto"):
            self.picked = "transducer" if joiner is not None else "paraformer"
        elif self.kind in ("transducer", "paraformer"):
            self.picked = self.kind
        else:
            return None
        if self.picked == "transducer" and joiner is None:
            return None
        params = {
            "tokens": str(tokens),
            "encoder": str(encoder),
            "decoder": str(decoder),
        }
        if joiner is not None:
            params["joiner"] = str(joiner)
        return self.picked, params

    def is_available(self):
        if self._failed:
            return False
        return bool(streaming_model_status(self.model_path, kind=self.kind).get("ready"))

    def start(self):
        self._stream = None
        recognizer = self._build()
        if recognizer is None:
            return False
        try:
            self._stream = recognizer.create_stream()
            return True
        except Exception as exc:
            self._failed = "创建识别流失败：%s" % exc
            self.last_error = self._failed
            self._warn("online stream create failed: %s", exc)
            return False

    def accept(self, audio, sample_rate=None):
        if self._stream is None and not self.start():
            return []
        samples = self._samples(audio, sample_rate)
        if samples is None or samples.size == 0:
            return []
        try:
            self._stream.accept_waveform(self.sample_rate, samples)
            self._recognizer.decode_stream(self._stream)
            text = " ".join(str(self._recognizer.get_result(self._stream) or "").split())
            final = bool(self._recognizer.is_endpoint(self._stream))
            if final:
                self._recognizer.reset(self._stream)
            self.last_error = ""
            return [AsrUpdate(text=text, is_final=final)]
        except Exception as exc:
            self._failed = "在线识别失败：%s" % exc
            self.last_error = self._failed
            self._warn("online decode failed: %s", exc)
            return []

    def finish(self):
        if self._stream is None or self._recognizer is None:
            return None
        try:
            self._stream.input_finished()
            self._recognizer.decode_stream(self._stream)
            text = " ".join(str(self._recognizer.get_result(self._stream) or "").split())
            self._recognizer.reset(self._stream)
            self._stream = None
            return AsrUpdate(text=text, is_final=True) if text else None
        except Exception as exc:
            self.last_error = "在线识别收尾失败：%s" % exc
            self._warn("online finish failed: %s", exc)
            self._stream = None
            return None

    def stop(self):
        self._stream = None
        self._recognizer = None
        return True

    def describe(self):
        state = streaming_model_status(self.model_path, kind=self.kind)
        return {
            "provider": self.name,
            "available": self.is_available(),
            "model": self.model_path or None,
            "kind": self.picked or state.get("kind") or None,
            "num_threads": self.num_threads,
            "state": state.get("state"),
            "runtime_ready": state.get("runtime_ready"),
            "note": self._failed or state.get("note", ""),
        }

    def _build(self):
        if self._recognizer is not None or self._failed:
            return self._recognizer
        plan = self._plan()
        if plan is None:
            self._failed = self.describe().get("note", "在线模型不可用")
            self.last_error = self._failed
            return None
        kind, params = plan
        try:
            import sherpa_onnx

            options = {
                "num_threads": self.num_threads,
                "sample_rate": self.sample_rate,
                "enable_endpoint_detection": True,
                "rule1_min_trailing_silence": 2.0,
                "rule2_min_trailing_silence": 1.0,
                "rule3_min_utterance_length": 18.0,
            }
            if kind == "transducer":
                self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
                    **params, **options
                )
            else:
                self._recognizer = sherpa_onnx.OnlineRecognizer.from_paraformer(
                    **params, **options
                )
            self.picked = kind
        except Exception as exc:
            self._failed = "在线模型加载失败：%s" % exc
            self.last_error = self._failed
            self._warn("online model load failed: %s", exc)
        return self._recognizer

    def _samples(self, audio, sample_rate):
        numpy = _numpy()
        if numpy is None:
            return None
        if isinstance(audio, (bytes, bytearray)):
            samples = float32_audio(audio, sample_rate)
        else:
            try:
                samples = numpy.asarray(audio, dtype="float32").reshape(-1)
            except Exception:
                return None
        if samples is None or samples.size == 0:
            return None
        rate = int(sample_rate or self.sample_rate)
        if rate != self.sample_rate and samples.size:
            target = max(1, int(round(samples.size * self.sample_rate / rate)))
            source_axis = numpy.arange(samples.size, dtype="float32")
            target_axis = numpy.linspace(0, samples.size - 1, target)
            samples = numpy.interp(target_axis, source_axis, samples).astype("float32")
        return samples

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)


def build_streaming_stt(
    model_path="", logger=None, kind="auto", language="", num_threads=2
):
    """按模型目录建在线识别器；没有模型时返回说明原因的 Null 实现。"""
    provider = SherpaOnlineSttProvider(
        model_path,
        logger=logger,
        kind=kind,
        language=language,
        num_threads=num_threads,
    )
    if provider.is_available():
        return provider
    return NullStreamingSttProvider(str(provider.describe().get("note") or ""))
