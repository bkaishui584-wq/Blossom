"""实时字幕流水线：音频分段、在线识别、稳定句翻译与过期结果保护。

字幕默认只留在内存里。翻译只处理已经稳定的最终句，临时结果不会发到云端。
"""

import queue
import threading
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class SubtitleSegment:
    """一行字幕。时间使用会话开始后的秒数，拿不到时间戳时保持 0。"""

    session_id: str
    segment_id: str
    source_language: str = ""
    target_language: str = ""
    source_text: str = ""
    translated_text: str = ""
    is_final: bool = False
    recognition_status: str = "partial"
    translation_status: str = "idle"
    translation_error: str = ""
    start_time: float = 0.0
    end_time: float = 0.0
    created_at: float = field(default_factory=time.time)

    def to_dict(self):
        return {
            "session_id": self.session_id,
            "segment_id": self.segment_id,
            "source_language": self.source_language,
            "target_language": self.target_language,
            "source_text": self.source_text,
            "translated_text": self.translated_text,
            "is_final": self.is_final,
            "recognition_status": self.recognition_status,
            "translation_status": self.translation_status,
            "translation_error": self.translation_error,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "created_at": self.created_at,
        }


class SubtitleTranslator:
    """用现有的任务模型做句子翻译。没配置任务模型时保持不可用。"""

    def __init__(self, provider, logger=None, max_chars=2000):
        self.provider = provider
        self.logger = logger
        self.max_chars = max(1, int(max_chars or 1))

    def available(self):
        if self.provider is None:
            return False
        try:
            return bool(self.provider.is_available())
        except Exception:
            return False

    def translate(self, text, source_language="", target_language=""):
        body = " ".join(str(text or "").split())[: self.max_chars]
        target = str(target_language or "").strip()
        if not body:
            return False, "", "没有可翻译的内容"
        if not target:
            return False, "", "没有选择目标语言"
        if not self.available():
            return False, "", "任务模型不可用"
        source = str(source_language or "auto").strip() or "auto"
        messages = [
            {
                "role": "system",
                "content": (
                    "You translate subtitles. Translate the user text from %s to %s. "
                    "Return only the translation, without quotes, labels, or commentary. "
                    "If the text is already in the target language, return it unchanged."
                    % (source, target)
                ),
            },
            {"role": "user", "content": body},
        ]
        try:
            result = self.provider.chat(messages, temperature=0)
        except Exception as exc:
            self._warn("subtitle translation failed: %s", exc)
            return False, "", str(exc)
        if not getattr(result, "ok", False):
            return False, "", str(getattr(result, "error", "") or "翻译请求失败")
        translated = " ".join(str(getattr(result, "content", "") or "").split())
        if not translated:
            return False, "", "翻译服务返回了空内容"
        return True, translated[: self.max_chars * 2], ""

    def describe(self):
        return {
            "available": self.available(),
            "model": str(getattr(self.provider, "model", "") or ""),
            "provider": str(getattr(self.provider, "name", "") or ""),
        }

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)


class SubtitlePipeline:
    """把采集、识别和翻译串起来，并丢弃过期的翻译结果。"""

    def __init__(
        self,
        capture,
        asr,
        translator=None,
        on_event=None,
        on_error=None,
        logger=None,
        max_translation_queue=8,
        max_segments=400,
        read_timeout=0.2,
    ):
        self.capture = capture
        self.asr = asr
        self.translator = translator
        self.on_event = on_event
        self.on_error = on_error
        self.logger = logger
        self.read_timeout = max(0.05, float(read_timeout or 0.2))
        try:
            self.max_segments = max(1, int(max_segments))
        except (TypeError, ValueError):
            self.max_segments = 400
        self.session_id = "sub-" + uuid.uuid4().hex[:12]
        self.source_language = "auto"
        self.target_language = ""
        self.last_error = ""
        self._lock = threading.RLock()
        self._segments = {}
        self._current_id = ""
        self._counter = 0
        self._active = False
        self._paused = False
        self._started_at = 0.0
        self._generation = 0
        self._stop = threading.Event()
        self._audio_thread = None
        self._translation_stop = threading.Event()
        self._translation_thread = None
        self._translation_queue = queue.Queue(maxsize=max(1, int(max_translation_queue)))

    # ---------- 生命周期 ----------
    def begin(self, source_language="auto", target_language=""):
        """建立会话和识别流，但还不打开音频设备。测试可直接走这里。"""
        if self._active:
            return False
        if not self._asr_available():
            self.last_error = self._asr_note()
            return False
        try:
            if not self.asr.start():
                self.last_error = self._asr_note() or "识别器启动失败"
                return False
        except Exception as exc:
            self.last_error = "识别器启动失败：%s" % exc
            return False
        self.source_language = str(source_language or "auto").strip() or "auto"
        self.target_language = str(target_language or "").strip()
        self._started_at = time.monotonic()
        self._stop.clear()
        self._generation += 1
        self._active = True
        self._paused = False
        self.last_error = ""
        self._start_translation_worker()
        return True

    def start(self, source_id="", source_language="auto", target_language=""):
        if not self.begin(source_language, target_language):
            return False
        try:
            opened = bool(self.capture.start(source_id))
        except Exception as exc:
            opened = False
            self.last_error = "打开音频设备失败：%s" % exc
        if not opened:
            self.last_error = self.last_error or self._capture_note() or "音频设备不可用"
            self._active = False
            self._shutdown_translation()
            try:
                self.asr.stop()
            except Exception:
                pass
            return False
        self._audio_thread = threading.Thread(
            target=self._audio_loop,
            name="blossom-subtitle-audio",
            daemon=True,
        )
        self._audio_thread.start()
        return True

    def stop(self):
        was_active = self._active
        if was_active:
            self._stop.set()
            try:
                self.capture.stop()
            except Exception:
                pass
            thread = self._audio_thread
            self._audio_thread = None
            if thread is not None and thread.is_alive():
                thread.join(timeout=1.5)
            self._finish_current()
        self._active = False
        self._generation += 1
        self._cancel_pending_translations("字幕已停止")
        self._shutdown_translation()
        try:
            self.asr.stop()
        except Exception:
            pass
        return was_active

    close = stop

    def pause(self):
        if not self._active or self._paused:
            return False
        try:
            paused = bool(self.capture.pause())
        except Exception as exc:
            self.last_error = "暂停音频失败：%s" % exc
            return False
        if paused:
            self._paused = True
        return paused

    def resume(self):
        if not self._active or not self._paused:
            return False
        try:
            resumed = bool(self.capture.resume())
        except Exception as exc:
            self.last_error = "恢复音频失败：%s" % exc
            return False
        if resumed:
            self._paused = False
        return resumed

    # ---------- 音频 ----------
    def feed_audio(self, samples, sample_rate=16000):
        """同步喂一段音频，返回本次产生的字幕行。测试和回放都用它。"""
        if not self._active or self._paused:
            return []
        try:
            updates = self.asr.accept(samples, sample_rate=sample_rate) or []
        except Exception as exc:
            self._abort("识别失败：%s" % exc)
            return []
        error = str(getattr(self.asr, "last_error", "") or "")
        if error:
            self._abort(error)
            return []
        produced = []
        for update in updates:
            segment = self._apply_update(update)
            if segment is not None:
                produced.append(segment)
        return produced

    def _audio_loop(self):
        last_error = ""
        while not self._stop.is_set() and self._active:
            if self._paused:
                time.sleep(0.05)
                continue
            try:
                samples = self.capture.read(timeout=self.read_timeout)
            except Exception as exc:
                self._abort("读取音频失败：%s" % exc)
                return
            if samples is None:
                error = str(getattr(self.capture, "last_error", "") or "")
                if error and error != last_error:
                    last_error = error
                    self._abort(error)
                    return
                time.sleep(0.02)
                continue
            self.feed_audio(samples, sample_rate=getattr(self.capture, "sample_rate", 16000))
        if self._active:
            self._finish_current()

    def _abort(self, message):
        """音频线程失败后结束会话，避免界面继续显示运行中。"""
        message = str(message or "音频采集中断")
        with self._lock:
            if not self._active:
                return False
            self._active = False
            self._paused = False
            self._generation += 1
        self.last_error = message
        self._cancel_pending_translations("字幕因音频中断已停止")
        self._shutdown_translation()
        try:
            self.capture.stop()
        except Exception:
            pass
        try:
            self.asr.stop()
        except Exception:
            pass
        self._error(message)
        return True

    def _apply_update(self, update):
        text = " ".join(str(getattr(update, "text", "") or "").split())
        final = bool(getattr(update, "is_final", False))
        if not text and not final:
            return None
        with self._lock:
            if not self._current_id:
                self._counter += 1
                self._current_id = "%s:%d" % (self.session_id, self._counter)
                self._segments[self._current_id] = SubtitleSegment(
                    session_id=self.session_id,
                    segment_id=self._current_id,
                    source_language=self.source_language,
                    target_language=self.target_language,
                    start_time=self._elapsed(),
                )
                self._trim_segments()
            segment = self._segments.get(self._current_id)
            if segment is None:
                return None
            if text:
                segment.source_text = text
                source_text = text
            else:
                source_text = segment.source_text
            segment.is_final = final
            segment.recognition_status = "final" if final else "partial"
            if final:
                segment.end_time = self._elapsed()
                self._current_id = ""
                if not source_text:
                    return None
                if not self.target_language:
                    segment.translation_status = "disabled"
                elif not self._translator_available():
                    segment.translation_status = "disabled"
                    segment.translation_error = "翻译模型不可用"
                else:
                    segment.translation_status = "pending"
            needs_translation = bool(
                final and source_text and segment.translation_status == "pending"
            )
            segment_id = segment.segment_id
            event = SubtitleSegment(**segment.to_dict())
        self._emit(event)
        if needs_translation:
            self._enqueue_translation(segment_id, source_text)
        return event

    def _finish_current(self):
        try:
            update = self.asr.finish()
        except Exception:
            update = None
        if update is not None:
            self._apply_update(update)

    # ---------- 翻译 ----------
    def _start_translation_worker(self):
        if not self._translator_available():
            return
        if self._translation_thread is not None and self._translation_thread.is_alive():
            return
        stop_event = threading.Event()
        self._translation_stop = stop_event
        self._translation_thread = threading.Thread(
            target=lambda: self._translation_loop(stop_event),
            name="blossom-subtitle-translate",
            daemon=True,
        )
        self._translation_thread.start()

    def _shutdown_translation(self):
        self._translation_stop.set()
        while True:
            try:
                self._translation_queue.get_nowait()
            except queue.Empty:
                break
        try:
            self._translation_queue.put_nowait(None)
        except queue.Full:
            pass
        thread = self._translation_thread
        self._translation_thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.5)

    def _translation_loop(self, stop_event):
        while not stop_event.is_set():
            try:
                item = self._translation_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                break
            segment_id, generation, text, source_language, target_language = item
            if not self._active or generation != self._generation:
                continue
            try:
                ok, translated, error = self.translator.translate(
                    text,
                    source_language=source_language,
                    target_language=target_language,
                )
            except Exception as exc:
                ok, translated, error = False, "", "翻译失败：%s" % exc
            with self._lock:
                segment = self._segments.get(segment_id)
                if (
                    segment is None
                    or not self._active
                    or generation != self._generation
                    or segment.source_text != text
                ):
                    continue
                segment.translation_status = "done" if ok else "failed"
                segment.translated_text = translated if ok else ""
                segment.translation_error = "" if ok else str(error or "翻译失败")[:300]
                self._trim_segments()
                event = SubtitleSegment(**segment.to_dict())
            self._emit(event)

    def _enqueue_translation(self, segment_id, text):
        while True:
            try:
                self._translation_queue.put_nowait(
                    (
                        segment_id,
                        self._generation,
                        text,
                        self.source_language,
                        self.target_language,
                    )
                )
                return
            except queue.Full:
                try:
                    dropped = self._translation_queue.get_nowait()
                except queue.Empty:
                    dropped = None
                if dropped is not None:
                    self._mark_translation_skipped(dropped[0], "翻译队列已满")
                if dropped is None:
                    self._mark_translation_skipped(segment_id, "翻译队列已满")
                    return

    def _mark_translation_skipped(self, segment_id, reason):
        with self._lock:
            segment = self._segments.get(segment_id)
            if segment is None or segment.translation_status != "pending":
                return
            segment.translation_status = "skipped"
            segment.translation_error = reason
            event = SubtitleSegment(**segment.to_dict())
        self._emit(event)

    def _cancel_pending_translations(self, reason):
        with self._lock:
            events = []
            for segment in self._segments.values():
                if segment.translation_status != "pending":
                    continue
                segment.translation_status = "skipped"
                segment.translation_error = str(reason or "翻译已取消")
                events.append(SubtitleSegment(**segment.to_dict()))
        for event in events:
            self._emit(event)

    def _trim_segments(self):
        """只留近期字幕；正在翻译的句子先保留，避免晚到结果丢失。"""
        overflow = len(self._segments) - self.max_segments
        if overflow <= 0:
            return
        removable = sorted(
            (
                (segment.created_at, segment_id)
                for segment_id, segment in self._segments.items()
                if segment_id != self._current_id
                and segment.translation_status != "pending"
            ),
            key=lambda item: item[0],
        )
        for _created_at, segment_id in removable[:overflow]:
            self._segments.pop(segment_id, None)

    # ---------- 状态 ----------
    def latest(self, limit=8):
        with self._lock:
            rows = sorted(
                self._segments.values(),
                key=lambda item: (item.start_time, item.created_at),
            )
        return [SubtitleSegment(**row.to_dict()) for row in rows[-max(1, int(limit)) :]]

    def describe(self):
        return {
            "session_id": self.session_id,
            "active": bool(self._active),
            "paused": bool(self._paused),
            "source_language": self.source_language,
            "target_language": self.target_language,
            "segments": len(self._segments),
            "translation": self._translator_available(),
            "error": self.last_error,
        }

    def _asr_available(self):
        try:
            return bool(self.asr is not None and self.asr.is_available())
        except Exception:
            return False

    def _asr_note(self):
        describe = getattr(self.asr, "describe", None)
        if describe is not None:
            try:
                return str((describe() or {}).get("note") or "")
            except Exception:
                pass
        return ""

    def _capture_note(self):
        describe = getattr(self.capture, "describe", None)
        if describe is not None:
            try:
                return str((describe() or {}).get("note") or "")
            except Exception:
                pass
        return ""

    def _translator_available(self):
        if self.translator is None or not self.target_language:
            return False
        try:
            return bool(self.translator.available())
        except Exception:
            return False

    def _elapsed(self):
        return max(0.0, time.monotonic() - self._started_at) if self._started_at else 0.0

    def _emit(self, segment):
        if self.on_event is None:
            return
        try:
            self.on_event(segment)
        except Exception as exc:
            self._warn("subtitle callback failed: %s", exc)

    def _error(self, message):
        if self.on_error is not None:
            try:
                self.on_error(str(message or ""))
            except Exception as exc:
                self._warn("subtitle error callback failed: %s", exc)

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)
