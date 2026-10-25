"""实时字幕阶段 D：模型按需安装、真实状态和离线降级。

这一阶段不下载模型，也不把模型权重塞进安装包。测试只造临时目录，确认状态
能分开报清楚：运行库、模型目录、文件缺口、文件是否为空，以及能不能启动。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters import local_runtimes  # noqa: E402
from security.config import Config  # noqa: E402
from voice import stt  # noqa: E402


def write_model(root, joiner=True, empty=None):
    root.mkdir(parents=True, exist_ok=True)
    targets = {
        "tokens.txt": b"a 1\n",
        "encoder.onnx": b"encoder",
        "decoder.onnx": b"decoder",
        "joiner.onnx": b"joiner",
    }
    for name, body in targets.items():
        if name == "joiner.onnx" and not joiner:
            continue
        (root / name).write_bytes(b"" if name == empty else body)
    return root


class ModelStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def test_no_model_is_reported_without_creating_anything(self):
        missing = self.base / "not-created"
        state = stt.streaming_model_status("")
        self.assertFalse(state["ready"])
        self.assertEqual(state["state"], "no_model")
        self.assertIn("不会自动下载", state["note"])
        self.assertFalse(missing.exists())

    def test_a_missing_folder_is_not_created(self):
        missing = self.base / "nope"
        state = stt.streaming_model_status(missing)
        self.assertEqual(state["state"], "path_missing")
        self.assertFalse(missing.exists())

    def test_a_file_is_not_mistaken_for_a_model_folder(self):
        target = self.base / "model.onnx"
        target.write_bytes(b"x")
        state = stt.streaming_model_status(target)
        self.assertEqual(state["state"], "path_not_directory")
        self.assertFalse(state["ready"])

    def test_an_empty_folder_says_it_is_empty(self):
        state = stt.streaming_model_status(self.base)
        self.assertEqual(state["state"], "empty")
        self.assertIn("空", state["note"])

    def test_missing_tokens_is_reported(self):
        (self.base / "encoder.onnx").write_bytes(b"x")
        (self.base / "decoder.onnx").write_bytes(b"x")
        state = stt.streaming_model_status(self.base)
        self.assertEqual(state["state"], "missing_tokens")
        self.assertIn("tokens.txt", state["note"])

    def test_missing_onnx_is_reported(self):
        (self.base / "tokens.txt").write_text("a 1\n", encoding="utf-8")
        state = stt.streaming_model_status(self.base)
        self.assertEqual(state["state"], "missing_onnx")
        self.assertIn(".onnx", state["note"])

    def test_missing_encoder_or_decoder_is_reported(self):
        (self.base / "tokens.txt").write_text("a 1\n", encoding="utf-8")
        (self.base / "model.onnx").write_bytes(b"x")
        state = stt.streaming_model_status(self.base)
        self.assertEqual(state["state"], "missing_encoder_decoder")
        self.assertIn("encoder", state["note"])

    def test_transducer_missing_joiner_is_reported(self):
        write_model(self.base, joiner=False)
        state = stt.streaming_model_status(self.base, kind="transducer")
        self.assertEqual(state["state"], "missing_joiner")
        self.assertIn("joiner", state["note"])

    def test_offline_models_are_not_sold_as_streaming_models(self):
        write_model(self.base)
        state = stt.streaming_model_status(self.base, kind="sense_voice")
        self.assertEqual(state["state"], "incompatible_kind")
        self.assertIn("离线模型", state["note"])

    def test_empty_model_files_are_not_ready(self):
        write_model(self.base, empty="encoder.onnx")
        with mock.patch.object(stt, "_module_present", return_value=True), mock.patch.object(
            stt, "_numpy", return_value=object()
        ):
            state = stt.streaming_model_status(self.base)
        self.assertEqual(state["state"], "empty_file")
        self.assertFalse(state["ready"])

    def test_complete_model_and_runtime_are_ready_for_start(self):
        write_model(self.base)
        with mock.patch.object(stt, "_module_present", return_value=True), mock.patch.object(
            stt, "_numpy", return_value=object()
        ):
            state = stt.streaming_model_status(self.base)
        self.assertTrue(state["ready"])
        self.assertTrue(state["model_ready"])
        self.assertTrue(state["runtime_ready"])
        self.assertEqual(state["kind"], "transducer")
        self.assertIn("真正加载", state["note"])

    def test_complete_model_without_runtime_is_not_ready(self):
        write_model(self.base)
        with mock.patch.object(stt, "_module_present", return_value=False), mock.patch.object(
            stt, "_numpy", return_value=object()
        ):
            state = stt.streaming_model_status(self.base)
        self.assertEqual(state["state"], "runtime_missing")
        self.assertTrue(state["model_ready"])
        self.assertFalse(state["ready"])
        self.assertIn("voice", state["note"])

    def test_provider_describe_uses_the_same_state(self):
        write_model(self.base, joiner=False)
        with mock.patch.object(stt, "_module_present", return_value=True), mock.patch.object(
            stt, "_numpy", return_value=object()
        ):
            provider = stt.SherpaOnlineSttProvider(str(self.base))
            data = provider.describe()
        self.assertTrue(data["available"])
        self.assertEqual(data["state"], "ready")
        self.assertTrue(data["runtime_ready"])
        self.assertEqual(data["kind"], "paraformer")


class RuntimeEntryTests(unittest.TestCase):
    def test_the_runtime_entry_does_not_call_a_model_ready_just_because_the_package_exists(self):
        config = SimpleNamespace(stt_model="", stt_kind="auto")
        with mock.patch.object(local_runtimes, "_module_available", return_value=True), mock.patch.object(
            stt, "_module_present", return_value=True
        ), mock.patch.object(stt, "_numpy", return_value=object()):
            entry = local_runtimes._sherpa_entry(config)
        self.assertTrue(entry["installed"])
        self.assertFalse(entry["ready"])
        self.assertIn("还没选择", entry["detail"])

    def test_the_runtime_entry_becomes_ready_only_with_a_complete_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = write_model(Path(tmp))
            config = SimpleNamespace(stt_model=str(root), stt_kind="auto")
            with mock.patch.object(
                local_runtimes, "_module_available", return_value=True
            ), mock.patch.object(stt, "_module_present", return_value=True), mock.patch.object(
                stt, "_numpy", return_value=object()
            ):
                entry = local_runtimes._sherpa_entry(config)
        self.assertTrue(entry["installed"])
        self.assertTrue(entry["ready"])
        self.assertEqual(entry["model_state"], "ready")
        self.assertIn("DESKPET_STT_MODEL", entry["needs"])


class OfflineAndPackagingTests(unittest.TestCase):
    def test_no_api_key_or_network_is_needed_to_probe_the_model(self):
        config = Config(load_env=False)
        self.assertEqual(str(getattr(config, "stt_model", "") or ""), "")
        with mock.patch("urllib.request.urlopen") as urlopen:
            state = stt.streaming_model_status("")
        urlopen.assert_not_called()
        self.assertFalse(state["ready"])

    def test_probing_does_not_import_a_downloader(self):
        source = Path(stt.__file__).read_text(encoding="utf-8")
        for unwanted in ("urlopen", "requests.get", "pip install", "DownloadFile"):
            self.assertNotIn(unwanted, source)

    def test_the_voice_pack_ships_the_runtimes_but_no_models(self):
        text = (ROOT / "build_packs.py").read_text(encoding="utf-8")
        voice_block = text.split('"voice": {', 1)[1].split("},", 1)[0]
        self.assertIn("sherpa-onnx", voice_block)
        self.assertIn("soundcard", voice_block)
        self.assertNotIn(".onnx", voice_block)
        self.assertNotIn("models\\", voice_block)

    def test_the_docs_tell_users_models_are_not_downloaded_automatically(self):
        for name in ("docs/distribution.md", "docs/install-guide.md"):
            body = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("不会自动下载", body, name)
            self.assertIn("tokens.txt", body, name)


if __name__ == "__main__":
    unittest.main()
