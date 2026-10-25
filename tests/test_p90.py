"""V9.1 第三、四阶段验收：文档解析（MarkItDown）与离线语音（sherpa-onnx）。

两个都是「装了就用、没装就如实降级」的可选件：

- 文档：装了 markitdown 就用它读容器格式，没装退回程序自带的朴素解析。两条路都要能读出正文，
  而且返回结果要如实说明是哪一档读的。
- 语音：装了 sherpa-onnx 并且配好模型目录才谈得上离线识别；缺任一样都保持不可用并说明缺什么。

验收盯住：格式真的能读、用的是哪一档不许含糊、没装不许假装能用、路径仍然受工作区约束。
"""

import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters import documents  # noqa: E402
from database.db import Database  # noqa: E402
from security.audit import AuditLog  # noqa: E402
from security.config import Config  # noqa: E402
from tools import workspace as workspace_tools  # noqa: E402
from tools.permission import PermissionManager  # noqa: E402
from tools.registry import ToolExecutor, ToolRegistry  # noqa: E402
from voice import stt  # noqa: E402

HAS_MARKITDOWN = documents.available()
HAS_SHERPA = stt._module_present("sherpa_onnx")
HAS_NUMPY = stt._numpy() is not None


def make_docx(path, body="Blossom progress"):
    """造一个结构完整的最小 docx：mammoth 这类解析器认它。"""
    types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
        '2006/relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>%s</w:t></w:r></w:p></w:body></w:document>" % body
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", document)
    return path


class DocumentAdapterTests(unittest.TestCase):
    """适配层本身：装没装都说实话，太大的文件不硬啃。"""

    def test_available_is_a_boolean(self):
        self.assertIsInstance(documents.available(), bool)

    def test_describe_never_claims_ready_without_being_installed(self):
        data = documents.describe()
        self.assertIn("installed", data)
        self.assertIn("note", data)
        if not data["installed"]:
            self.assertFalse(data["ready"])

    def test_oversized_file_is_refused_before_parsing(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        big = Path(tmp.name) / "big.html"
        big.write_bytes(b"<html><body>hi</body></html>")
        with mock.patch.object(documents, "MAX_BYTES", 4):
            self.assertTrue(documents.too_big(big))
            self.assertEqual(documents.to_markdown(big, 100), "")

    def test_missing_file_is_not_an_exception(self):
        self.assertEqual(documents.to_markdown(str(Path(tempfile.gettempdir()) / "no-such-file.html"), 100), "")

    @unittest.skipUnless(HAS_MARKITDOWN, "没装 markitdown")
    def test_html_becomes_markdown(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        page = Path(tmp.name) / "page.html"
        page.write_text("<html><body><h1>Blossom</h1><ul><li>ollama</li></ul></body></html>", encoding="utf-8")
        text = documents.to_markdown(page, 500)
        self.assertIn("Blossom", text)
        self.assertIn("ollama", text)


class DocumentTierTests(unittest.TestCase):
    """两档调度：纯文本走便宜的，容器格式优先用 MarkItDown，结果要标明出处。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_plain_text_uses_the_builtin_reader(self):
        note = self.root / "note.txt"
        note.write_text("第一行\n第二行\n", encoding="utf-8")
        found = workspace_tools.extract_document(note, 200)
        self.assertEqual(found["engine"], "builtin")
        self.assertIn("第一行", found["text"])

    def test_unknown_suffix_reads_nothing_and_does_not_pretend(self):
        blob = self.root / "thing.xyz"
        blob.write_bytes(b"\x00\x01\x02binary")
        found = workspace_tools.extract_document(blob, 200)
        self.assertEqual(found["text"], "")
        self.assertEqual(found["engine"], "")

    @unittest.skipUnless(HAS_MARKITDOWN, "没装 markitdown")
    def test_docx_goes_through_markitdown(self):
        target = make_docx(self.root / "a.docx", body="Blossom 进度报告")
        found = workspace_tools.extract_document(target, 400)
        self.assertEqual(found["engine"], "markitdown")
        self.assertIn("Blossom", found["text"])

    def test_without_markitdown_docx_falls_back_to_the_builtin_reader(self):
        target = make_docx(self.root / "b.docx", body="Fallback works")
        with mock.patch.object(documents, "available", return_value=False):
            found = workspace_tools.extract_document(target, 400)
        self.assertEqual(found["engine"], "builtin")
        self.assertIn("Fallback works", found["text"])

    def test_markitdown_only_format_says_so_when_missing(self):
        target = self.root / "book.epub"
        target.write_bytes(b"pretend epub")
        with mock.patch.object(documents, "available", return_value=False):
            found = workspace_tools.extract_document(target, 400)
        self.assertEqual(found["text"], "")
        self.assertIn("MarkItDown", found["error"])

    def test_extract_document_text_keeps_the_old_signature(self):
        note = self.root / "plain.md"
        note.write_text("# 标题\n", encoding="utf-8")
        self.assertIn("标题", workspace_tools.extract_document_text(note, 100))


class ReadDocumentToolTests(unittest.TestCase):
    """真工具：受工作区约束，结果如实报用了哪一档。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        config = Config(load_env=False)
        # workspace_root 优先于 fallback，测试要把它指到临时目录，别碰真实仓库。
        config.workspace_root = str(self.root)
        self.registry = ToolRegistry()
        workspace_tools.register_workspace_tools(self.registry, config, workspace=str(self.root))
        audit = AuditLog(Database(":memory:"))
        self.executor = ToolExecutor(
            self.registry, PermissionManager(audit=audit), audit=audit
        )

    def test_tool_reports_which_engine_it_used(self):
        (self.root / "a.txt").write_text("hello", encoding="utf-8")
        out = self.executor.execute("read_document", {"path": "a.txt", "max_chars": 100})
        self.assertTrue(out.ok, out.error)
        payload = json.loads(out.result) if isinstance(out.result, str) else out.result
        self.assertIn("engine", payload)
        self.assertEqual(payload["format"], "txt")

    def test_unsupported_suffix_is_refused(self):
        (self.root / "a.bin").write_bytes(b"\x00\x01")
        out = self.executor.execute("read_document", {"path": "a.bin"})
        self.assertFalse(out.ok)

    def test_path_outside_the_workspace_is_refused(self):
        out = self.executor.execute("read_document", {"path": "../../outside.txt"})
        self.assertFalse(out.ok)


class SherpaEngineTests(unittest.TestCase):
    """离线识别：包、模型、numpy 三样齐了才算能用。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_sherpa_is_listed_in_the_engine_table(self):
        self.assertIn("sherpa_onnx", stt.ENGINES)
        self.assertIn("sherpa_onnx", stt.probe_engines())

    def test_without_a_model_it_stays_unavailable_and_says_why(self):
        provider = stt.SherpaSttProvider("", logger=None)
        self.assertFalse(provider.is_available())
        self.assertIn("模型", provider.describe()["note"])
        self.assertEqual(provider.transcribe(b"\x00\x01"), "")

    def test_missing_folder_is_reported(self):
        provider = stt.SherpaSttProvider(str(self.root / "nope"), logger=None)
        self.assertFalse(provider.is_available())
        self.assertIn("模型目录", provider.describe()["note"])

    def test_missing_tokens_file_is_reported(self):
        (self.root / "model.onnx").write_bytes(b"x")
        provider = stt.SherpaSttProvider(str(self.root), logger=None)
        self.assertFalse(provider.is_available())
        self.assertIn("tokens.txt", provider.describe()["note"])

    def test_sense_voice_layout_is_detected(self):
        (self.root / "model.onnx").write_bytes(b"x")
        (self.root / "tokens.txt").write_text("a 1\n", encoding="utf-8")
        provider = stt.SherpaSttProvider(str(self.root), logger=None, kind="sense_voice")
        self.assertIsNotNone(provider._plan())
        self.assertEqual(provider._plan()[0], "sense_voice")
        self.assertEqual(provider.describe()["kind"], "sense_voice")

    def test_whisper_layout_is_detected(self):
        (self.root / "tiny-encoder.onnx").write_bytes(b"x")
        (self.root / "tiny-decoder.onnx").write_bytes(b"x")
        (self.root / "tokens.txt").write_text("a 1\n", encoding="utf-8")
        provider = stt.SherpaSttProvider(str(self.root), logger=None)
        self.assertIsNotNone(provider._plan())
        self.assertEqual(provider._plan()[0], "whisper")

    def test_folder_name_hint_is_used(self):
        folder = self.root / "sherpa-onnx-paraformer-zh"
        folder.mkdir()
        (folder / "model.onnx").write_bytes(b"x")
        (folder / "tokens.txt").write_text("a 1\n", encoding="utf-8")
        provider = stt.SherpaSttProvider(str(folder), logger=None)
        self.assertEqual(provider.describe()["kind"], "paraformer")

    @unittest.skipUnless(HAS_NUMPY, "没有 numpy")
    def test_pcm16_becomes_float32_in_range(self):
        samples = stt.float32_audio(b"\x00\x00\x00\x40", 16000)
        self.assertIsNotNone(samples)
        self.assertEqual(len(samples), 2)
        self.assertAlmostEqual(float(samples[0]), 0.0, places=4)
        self.assertAlmostEqual(float(samples[1]), 0.5, places=4)

    def test_audio_helpers_never_raise_on_junk(self):
        self.assertIsNone(stt.float32_audio(b"", 16000))
        self.assertIsNone(stt.float32_audio("no-such-file.wav", 16000))

    def test_engine_without_model_is_not_ready(self):
        config = Config(load_env=False)
        data = stt.describe_engines(config)
        self.assertIsInstance(data["installed"], list)
        if not data["model"]:
            needs_model = [name for name in data["installed"] if name in ("sherpa_onnx", "vosk")]
            if needs_model:
                self.assertFalse(data["ready"], "没有模型目录就不该报就绪")

    @unittest.skipUnless(HAS_SHERPA and HAS_NUMPY, "没装 sherpa-onnx")
    def test_build_stt_picks_sherpa_when_configured(self):
        (self.root / "model.onnx").write_bytes(b"x")
        (self.root / "tokens.txt").write_text("a 1\n", encoding="utf-8")
        config = Config(load_env=False)
        config.stt_engine = "sherpa-onnx"
        config.stt_model = str(self.root)
        provider = stt.build_stt(config)
        self.assertEqual(provider.name, "sherpa-onnx")
        self.assertTrue(provider.is_available())


class RuntimeStatusWordingTests(unittest.TestCase):
    """能力页的措辞要跟实际接线一致，不能还说「没并进来」。"""

    def entries(self):
        from adapters import local_runtimes

        return {item["id"]: item for item in local_runtimes.detect()["entries"]}

    def test_markitdown_entry_points_at_read_document(self):
        item = self.entries()["markitdown"]
        self.assertIn("read_document", item["detail"] + item["needs"])

    def test_sherpa_entry_tells_where_the_model_goes(self):
        item = self.entries()["sherpa-onnx"]
        blob = item["detail"] + item["needs"]
        self.assertIn("DESKPET_STT_MODEL", blob)


def make_pdf(path, body="Blossom PDF text"):
    """手搓一个最小 PDF：pdfminer 只要求结构对，不用装生成器。"""
    stream = ("BT /F1 12 Tf 20 100 Td (%s) Tj ET" % body).encode("latin-1")
    parts = [
        b"%PDF-1.4\n",
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n",
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n",
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>endobj\n",
        b"4 0 obj<</Length %d>>stream\n" % len(stream) + stream + b"\nendstream\nendobj\n",
        b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n",
        b"trailer<</Root 1 0 R>>\n%%EOF\n",
    ]
    Path(path).write_bytes(b"".join(parts))
    return path


def has_openpyxl():
    try:
        import openpyxl  # noqa: F401

        return True
    except Exception:
        return False


def has_pdfminer():
    try:
        from pdfminer.high_level import extract_text  # noqa: F401

        return True
    except Exception:
        return False


class SecondTierDocumentTests(unittest.TestCase):
    """第二档兜底：MarkItDown 用不上时，表格走 openpyxl、PDF 走 pdfminer。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_without_markitdown_xlsx_uses_openpyxl(self):
        if not has_openpyxl():
            self.skipTest("没装 openpyxl")
        from openpyxl import Workbook

        target = self.root / "book.xlsx"
        book = Workbook()
        book.active.title = "成绩"
        book.active.append(["姓名", "分数"])
        book.active.append(["小明", 90])
        book.save(target)
        with mock.patch.object(documents, "available", return_value=False):
            found = workspace_tools.extract_document(target, 400)
        self.assertEqual(found["engine"], "openpyxl")
        self.assertIn("姓名", found["text"])
        self.assertIn("小明", found["text"])

    def test_without_markitdown_pdf_uses_pdfminer(self):
        if not has_pdfminer():
            self.skipTest("没装 pdfminer")
        target = make_pdf(self.root / "paper.pdf", body="Blossom PDF text")
        with mock.patch.object(documents, "available", return_value=False):
            found = workspace_tools.extract_document(target, 400)
        self.assertEqual(found["engine"], "pdfminer")
        self.assertIn("Blossom", found["text"])

    def test_second_tier_is_not_claimed_when_markitdown_did_the_work(self):
        if not (HAS_MARKITDOWN and has_openpyxl()):
            self.skipTest("缺 markitdown 或 openpyxl")
        from openpyxl import Workbook

        target = self.root / "both.xlsx"
        book = Workbook()
        book.active.append(["a", "b"])
        book.save(target)
        found = workspace_tools.extract_document(target, 400)
        self.assertEqual(found["engine"], "markitdown")

    def test_oversized_file_is_refused_by_the_second_tier_too(self):
        if not has_openpyxl():
            self.skipTest("没装 openpyxl")
        target = self.root / "huge.xlsx"
        target.write_bytes(b"x")
        with mock.patch.object(documents, "too_big", return_value=True):
            text, engine = documents.local_fallback(target, 400)
        self.assertEqual((text, engine), ("", ""))

    def test_missing_table_dependency_reads_nothing_not_junk(self):
        target = self.root / "nope.xlsx"
        target.write_bytes(b"not really a workbook")
        text, engine = documents.local_fallback(target, 400)
        self.assertEqual(text, "")


class VoiceConfigTests(unittest.TestCase):
    """语音配置项：默认值要保守，环境变量要能覆盖。"""

    def test_kind_defaults_to_auto(self):
        self.assertEqual(Config(load_env=False).stt_kind, "auto")

    def test_engine_and_language_default_to_empty(self):
        config = Config(load_env=False)
        self.assertEqual(config.stt_engine, "")
        self.assertEqual(config.stt_language, "")

    def test_env_overrides_kind_and_language(self):
        patch = {"DESKPET_STT_KIND": "sense_voice", "DESKPET_STT_LANGUAGE": "zh"}
        with mock.patch.dict(os.environ, patch):
            config = Config(load_env=False)
        self.assertEqual(config.stt_kind, "sense_voice")
        self.assertEqual(config.stt_language, "zh")


if __name__ == "__main__":
    unittest.main()
