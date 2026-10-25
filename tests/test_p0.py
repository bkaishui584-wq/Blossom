"""P0 验收：密钥脱敏、SQLite 迁移幂等、模型未配置时不伪造结果。"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.db import Database
from logkit.logger import configure, get_logger, redact, register_secret
from model.adapter import OpenAICompatibleProvider, build_provider
from security.config import Config


class ConfigTests(unittest.TestCase):
    def test_redacted_never_exposes_key(self):
        os.environ["DESKPET_LLM_API_KEY"] = "sk-test-secret-123456"
        try:
            config = Config(load_env=False)
            self.assertEqual(config.redacted()["llm_api_key"], "set")
            self.assertNotIn("sk-test-secret-123456", str(config.redacted()))
        finally:
            os.environ.pop("DESKPET_LLM_API_KEY", None)

    def test_relative_paths_anchor_to_project_root(self):
        os.environ["DESKPET_DB_PATH"] = "./data/deskpet.sqlite3"
        try:
            config = Config(load_env=False)
        finally:
            os.environ.pop("DESKPET_DB_PATH", None)
        self.assertTrue(config.db_path.is_absolute())
        self.assertEqual(config.db_path.parent, config.project_root / "data")


class DatabaseTests(unittest.TestCase):
    def test_migrate_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "t.sqlite3")
            first_run = db.migrate()
            self.assertTrue(first_run)
            self.assertEqual(db.migrate(), [])
            self.assertIn("schema_migrations", db.health()["tables"])

    def test_wal_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "t.sqlite3")
            self.assertEqual(db.query_one("PRAGMA journal_mode")["journal_mode"], "wal")


class ProviderTests(unittest.TestCase):
    def test_unconfigured_provider_reports_error(self):
        provider = OpenAICompatibleProvider()
        self.assertFalse(provider.is_available())
        result = provider.chat([{"role": "user", "content": "hi"}])
        self.assertFalse(result.ok)
        self.assertTrue(result.error)

    def test_unknown_provider_name_returns_none(self):
        class FakeConfig:
            llm_provider = "no-such-provider"

        self.assertIsNone(build_provider(FakeConfig()))


class LogTests(unittest.TestCase):
    def test_key_never_reaches_log_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = configure(tmp, "INFO", dev_mode=False)
            try:
                register_secret("sk-test-secret-123456")
                get_logger("test").info("key=%s", "sk-test-secret-123456")
                for handler in logger.handlers:
                    handler.flush()
                logged = (Path(tmp) / "deskpet.log").read_text(encoding="utf-8")
                self.assertNotIn("sk-test-secret-123456", logged)
                self.assertIn("***", logged)
            finally:
                for handler in list(logger.handlers):
                    logger.removeHandler(handler)
                    handler.close()

    def test_redact_masks_bearer_token(self):
        self.assertNotIn("abcdef123456", redact("Authorization: Bearer abcdef123456"))

    def test_redact_masks_common_credential_fields(self):
        text = (
            "password=hunter2 passwd: 'two words' secret=\"shh\" "
            "api_key=abc123 apikey: def456 access_token=ghi789 "
            "refresh_token=\"jkl012\" token=mno345 client_secret='pqr678' "
            '"password": "json-value" "authorization": "Bearer xyz987" '
            "Authorization: Basic auth-secret"
        )
        cleaned = redact(text)
        for value in (
            "hunter2",
            "two words",
            "shh",
            "abc123",
            "def456",
            "ghi789",
            "jkl012",
            "mno345",
            "pqr678",
            "json-value",
            "xyz987",
            "auth-secret",
        ):
            self.assertNotIn(value, cleaned)
        self.assertIn("password=***", cleaned)
        self.assertIn("api_key=***", cleaned)

    def test_format_arguments_are_redacted_after_interpolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = configure(tmp, "INFO", dev_mode=False)
            try:
                get_logger("test").info("password=%s token=%s", "hunter2", "abc123")
                for handler in logger.handlers:
                    handler.flush()
                logged = (Path(tmp) / "deskpet.log").read_text(encoding="utf-8")
                self.assertNotIn("hunter2", logged)
                self.assertNotIn("abc123", logged)
                self.assertIn("password=***", logged)
                self.assertIn("token=***", logged)
            finally:
                for handler in list(logger.handlers):
                    logger.removeHandler(handler)
                    handler.close()


if __name__ == "__main__":
    unittest.main()
