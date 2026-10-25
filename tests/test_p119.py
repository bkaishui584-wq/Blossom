"""发行自检脚本：哈希、包边界和能力包版本规则。"""

import hashlib
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "verify_release.ps1"


def write_zip(path, entries):
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)


def write_hash(path):
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    Path(str(path) + ".sha256").write_text(digest, encoding="ascii")


def make_release(root, version="9.9.9", base_entries=None, voice_entries=None, extra_voice=False):
    name = "Blossom-%s-win64" % version
    base = root / (name + ".zip")
    entries = {
        name + "/Blossom.exe": b"dummy",
        name + "/README.md": b"# demo\n",
    }
    entries.update(base_entries or {})
    write_zip(base, entries)
    write_hash(base)

    setup = root / (name + "-setup.exe")
    setup.write_bytes(b"setup")
    write_hash(setup)

    voice = root / "voice-9.9.9.zip"
    write_zip(voice, voice_entries or {
        "site/soundcard/__init__.py": b"",
        "site/sherpa_onnx/__init__.py": b"",
    })
    write_hash(voice)
    if extra_voice:
        old = root / "voice-9.9.8.zip"
        write_zip(old, {"site/soundcard/__init__.py": b""})
        write_hash(old)

    docs = root / "docs-9.9.9.zip"
    write_zip(docs, {"site/markitdown/__init__.py": b""})
    write_hash(docs)
    return base, voice


def run_verify(root, version="9.9.9"):
    return subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            "-ReleaseDir",
            str(root),
            "-Version",
            version,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


class VerifyReleaseTests(unittest.TestCase):
    def test_valid_release_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_release(root)
            result = run_verify(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("发行自检全部通过", result.stdout)

    def test_tampered_hash_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base, _ = make_release(root)
            Path(str(base) + ".sha256").write_text("0" * 64, encoding="ascii")
            result = run_verify(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("[FAIL]", result.stdout)
            self.assertIn("哈希不一致", result.stdout)

    def test_base_zip_with_audio_runtime_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_release(root, base_entries={"site/soundcard/__init__.py": b""})
            result = run_verify(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("soundcard", result.stdout)

    def test_voice_pack_with_model_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_release(root, voice_entries={
                "site/soundcard/__init__.py": b"",
                "site/sherpa_onnx/__init__.py": b"",
                "models/model.onnx": b"weights",
            })
            result = run_verify(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("模型权重", result.stdout)

    def test_duplicate_voice_packs_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_release(root, extra_voice=True)
            result = run_verify(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("voice 能力包应只有 1 个", result.stdout)

    def test_missing_checksum_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base, _ = make_release(root)
            Path(str(base) + ".sha256").unlink()
            result = run_verify(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("缺少校验文件", result.stdout)


if __name__ == "__main__":
    unittest.main()
