"""实时字幕历史：只在用户打开开关后保存最终字幕，并导出为常用文本格式。

音频永远不落盘；这里只存识别后的文字、译文、时间和来源会话。
"""

from pathlib import Path


class SubtitleHistoryRepository:
    """字幕历史的 SQL 读写与导出。业务开关由界面层决定。"""

    FORMATS = ("txt", "srt", "vtt")

    def __init__(self, database, logger=None):
        self.db = database
        self.logger = logger

    def available(self):
        return self.db is not None

    def save(self, segment):
        """保存一条最终字幕；译文稍后到达时更新同一行。"""
        if not self.available() or segment is None:
            return False
        data = segment.to_dict() if hasattr(segment, "to_dict") else dict(segment or {})
        if not bool(data.get("is_final")):
            return False
        source_text = str(data.get("source_text") or "").strip()
        if not source_text:
            return False
        session_id = str(data.get("session_id") or "")
        segment_id = str(data.get("segment_id") or "")
        if not session_id or not segment_id:
            return False
        try:
            self.db.execute(
                "INSERT INTO subtitle_segments ("
                "session_id, segment_id, source_language, target_language,"
                " source_text, translated_text, is_final, recognition_status,"
                " translation_status, translation_error, start_time, end_time, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(session_id, segment_id) DO UPDATE SET"
                " source_language = excluded.source_language,"
                " target_language = excluded.target_language,"
                " source_text = excluded.source_text,"
                " translated_text = excluded.translated_text,"
                " is_final = excluded.is_final,"
                " recognition_status = excluded.recognition_status,"
                " translation_status = excluded.translation_status,"
                " translation_error = excluded.translation_error,"
                " start_time = excluded.start_time,"
                " end_time = excluded.end_time",
                (
                    session_id,
                    segment_id,
                    str(data.get("source_language") or ""),
                    str(data.get("target_language") or ""),
                    source_text,
                    str(data.get("translated_text") or ""),
                    int(bool(data.get("is_final"))),
                    str(data.get("recognition_status") or ""),
                    str(data.get("translation_status") or ""),
                    str(data.get("translation_error") or ""),
                    float(data.get("start_time") or 0.0),
                    float(data.get("end_time") or 0.0),
                    float(data.get("created_at") or 0.0),
                ),
            )
            return True
        except Exception as exc:
            self._warn("subtitle history save failed: %s", exc)
            return False

    def recent(self, limit=100, session_id=""):
        if not self.available():
            return []
        sql = (
            "SELECT id, session_id, segment_id, source_language, target_language,"
            " source_text, translated_text, start_time, end_time, created_at"
            " FROM subtitle_segments WHERE is_final = 1"
        )
        params = []
        if session_id:
            sql += " AND session_id = ?"
            params.append(str(session_id))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        try:
            rows = self.db.query(sql, tuple(params))
        except Exception as exc:
            self._warn("subtitle history read failed: %s", exc)
            return []
        rows.reverse()
        return rows

    def count(self):
        if not self.available():
            return 0
        try:
            row = self.db.query_one("SELECT COUNT(*) AS n FROM subtitle_segments")
            return int((row or {}).get("n") or 0)
        except Exception:
            return 0

    def clear(self, session_id="", confirm=False):
        """清空真实记录；必须明确确认，避免误删。"""
        if not self.available() or not confirm:
            return 0
        if session_id:
            return self.db.execute(
                "DELETE FROM subtitle_segments WHERE session_id = ?", (str(session_id),)
            )
        return self.db.execute("DELETE FROM subtitle_segments")

    @staticmethod
    def format_cues(rows, fmt="srt"):
        """把记录转成 TXT / SRT / VTT。时间取真实识别时间。"""
        fmt = str(fmt or "srt").strip().lower()
        if fmt not in SubtitleHistoryRepository.FORMATS:
            fmt = "srt"
        rows = list(rows or [])
        if fmt == "txt":
            blocks = []
            for row in rows:
                start = _txt_time(row.get("start_time"))
                source = str(row.get("source_text") or "").strip()
                translated = str(row.get("translated_text") or "").strip()
                body = source
                if translated:
                    body += "\n" + translated
                if body:
                    blocks.append(("[%s] " % start if start else "") + body)
            return "\n\n".join(blocks) + ("\n" if blocks else "")

        lines = ["WEBVTT", ""] if fmt == "vtt" else []
        for index, row in enumerate(rows, 1):
            start = float(row.get("start_time") or 0.0)
            end = float(row.get("end_time") or start)
            if end <= start:
                end = start + 1.0
            if fmt == "srt":
                lines.append(str(index))
                lines.append("%s --> %s" % (_srt_time(start), _srt_time(end)))
            else:
                lines.append("%s --> %s" % (_vtt_time(start), _vtt_time(end)))
            source = str(row.get("source_text") or "").strip()
            translated = str(row.get("translated_text") or "").strip()
            if source:
                lines.append(source)
            if translated:
                lines.append(translated)
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def export(self, target, fmt="srt", limit=5000, session_id=""):
        """写一份导出文件。路径与格式由调用方明确给出。"""
        fmt = str(fmt or "srt").strip().lower()
        if fmt not in self.FORMATS:
            return {"ok": False, "error": "不支持的导出格式：%s" % fmt}
        target = str(target or "").strip()
        if not target:
            return {"ok": False, "error": "没有选择保存位置"}
        rows = self.recent(limit=limit, session_id=session_id)
        if not rows:
            return {"ok": False, "error": "还没有可导出的字幕"}
        try:
            path = Path(target).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self.format_cues(rows, fmt), encoding="utf-8")
            return {"ok": True, "path": str(path), "count": len(rows), "format": fmt}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _warn(self, message, *args):
        if self.logger is None:
            return
        try:
            self.logger.warning(message, *args)
        except Exception:
            pass


def _txt_time(value):
    seconds = max(0.0, float(value or 0.0))
    if seconds <= 0:
        return ""
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return "%02d:%02d:%02d" % (hours, minutes, secs)


def _srt_time(value):
    seconds = max(0.0, float(value or 0.0))
    millis = int(round((seconds - int(seconds)) * 1000))
    whole = int(seconds)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return "%02d:%02d:%02d,%03d" % (hours, minutes, secs, millis)


def _vtt_time(value):
    return _srt_time(value).replace(",", ".")
