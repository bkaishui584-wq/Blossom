"""P4：模型调用记录、Ollama 本地预设与 Provider 装饰器。"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database.db import Database
from model import adapter, config as model_config
from model.base import ChatDelta, ChatResult, ModelProvider, Usage
from model.logged import LoggedProvider
from services.model_calls import ModelCallLog


class FakeProvider(ModelProvider):
    name = "fake"
    label = "Fake"
    model = "fake-model"
    role = "companion"

    def __init__(self, ok=True, error="", raises=None):
        self.ok = ok
        self.error = error
        self.raises = raises

    def is_available(self):
        return True

    def chat(self, messages, tools=None, temperature=0.7):
        if self.raises:
            raise self.raises
        return ChatResult(
            ok=self.ok,
            content="ok" if self.ok else "",
            error=self.error,
            model=self.model,
            usage=Usage(prompt_tokens=3, completion_tokens=2),
        )

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(content="你")
        yield ChatDelta(content="好")
        yield ChatDelta(usage=Usage(prompt_tokens=4, completion_tokens=2), done=True)


class ModelCallLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "t.sqlite3")
        self.db.migrate()
        self.log = ModelCallLog(self.db, clock=lambda: 1761000000.0)

    def test_log_stores_status_latency_and_redacts_secrets(self):
        self.log.record(
            role="companion",
            provider="fake",
            model="m",
            ok=False,
            latency_ms=42,
            prompt_tokens=3,
            completion_tokens=2,
            error_kind="auth",
            error="bad key sk-secret-1234567890123456",
        )
        row = self.log.rows()[0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["latency_ms"], 42)
        self.assertEqual(row["total_tokens"], 5)
        self.assertNotIn("sk-secret", row["error"])
        summary = self.log.summary()
        self.assertEqual(summary["calls"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["total_tokens"], 5)

    def test_logged_provider_records_success_failure_and_stream(self):
        logged = LoggedProvider(FakeProvider(), self.log, "companion")
        result = logged.chat([{"role": "user", "content": "hello"}])
        self.assertTrue(result.ok)
        self.assertEqual(logged.model, "fake-model")

        failed = LoggedProvider(
            FakeProvider(ok=False, error="quota"), self.log, "companion"
        )
        failed.chat([{"role": "user", "content": "hello"}])
        with self.assertRaises(RuntimeError):
            LoggedProvider(FakeProvider(raises=RuntimeError("network")), self.log).chat([])

        streamed = LoggedProvider(FakeProvider(), self.log, "companion")
        output = list(streamed.stream([]))
        self.assertTrue(any(chunk.content for chunk in output))
        rows = self.log.rows()
        self.assertEqual(len(rows), 4)
        self.assertEqual(sum(row["total_tokens"] for row in rows), 16)
        self.assertTrue(any(row["streamed"] for row in rows))

    def test_catalog_is_wrapped_when_call_log_is_attached(self):
        from agent.core import AgentCore
        from agent.context import ContextManager

        class Memory:
            last_recall = {}

            def recall(self, *args, **kwargs):
                return [], "", ""

            def record(self, *args, **kwargs):
                return 1

            def record_usage(self, *args, **kwargs):
                return 1

        agent = AgentCore(
            FakeProvider(),
            Memory(),
            ContextManager(),
            model_call_log=self.log,
        )
        agent.set_model_catalog({"x": FakeProvider()})
        agent.model_catalog["x"].chat([])
        self.assertEqual(self.log.rows()[0]["provider"], "fake")

    def test_ollama_preset_is_local_and_needs_no_cloud_key(self):
        preset = model_config.PROVIDERS["ollama"]
        self.assertEqual(preset["base_url"], "http://127.0.0.1:11434/v1")
        self.assertEqual(model_config.provider_for_url(preset["base_url"]), "ollama")
        self.assertIn("ollama", adapter.PROVIDERS)
        self.assertIn("ollama", model_config.provider_label("ollama").lower())
        if adapter.OpenAI is not None:
            provider = adapter.OllamaProvider(model="qwen2.5:7b")
            self.assertTrue(provider.is_available())

    def test_migration_and_model_panel_wiring(self):
        tables = {
            row["name"]
            for row in self.db.query("SELECT name FROM sqlite_master WHERE type='table'")
        }
        self.assertIn("model_call_log", tables)
        source = (ROOT / "ui" / "model_panel.py").read_text(encoding="utf-8")
        self.assertIn("最近的模型调用", source)
        self.assertIn('"ollama"', source)


if __name__ == "__main__":
    unittest.main()
