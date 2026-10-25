"""多模型接入的后端：密钥存储、配置仓库、错误映射、Provider 与连接测试。

全程用临时目录和假客户端：不碰真实密钥，不发真实请求，不产生费用。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from model import adapter, errors
from model import config as model_config
from security import credentials


class FakeConfig:
    def __init__(self, root):
        self.data_root = str(root)
        self.data_dir = str(Path(root) / "data")
        self.model_configs = model_config.ModelConfigRepository(self.data_dir)
        self.secrets = credentials.SecretStore(Path(self.data_dir) / "secrets")
        self.llm_provider = "openai-compatible"
        self.llm_timeout = 30
        self.llm_proxy = ""
        self.llm_retries = 1
        self.logger = None
        self.llm_base_url = ""
        self.llm_api_key = ""
        self.llm_model = ""
        self.companion_base_url = ""
        self.companion_api_key = ""
        self.companion_model = ""
        self.task_base_url = ""
        self.task_api_key = ""
        self.task_model = ""


@unittest.skipUnless(os.name == "nt", "DPAPI 只在 Windows 上有")
class SecretStoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = credentials.SecretStore(Path(tmp.name) / "secrets")

    def test_save_and_load_roundtrip(self):
        result = self.store.save("abc", "sk-test-1234567890")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.store.load("abc"), "sk-test-1234567890")
        self.assertTrue(self.store.has("abc"))

    def test_the_file_never_contains_the_plain_key(self):
        self.store.save("abc", "sk-test-1234567890")
        raw = (Path(self.store.folder) / "abc.bin").read_bytes()
        self.assertNotIn(b"sk-test-1234567890", raw)

    def test_delete_removes_the_secret(self):
        self.store.save("abc", "sk-test")
        self.assertTrue(self.store.delete("abc"))
        self.assertFalse(self.store.has("abc"))
        self.assertEqual(self.store.load("abc"), "")

    def test_an_invalid_id_is_refused(self):
        self.assertFalse(self.store.save("../evil", "x")["ok"])
        self.assertIsNone(self.store.file_for("a/b"))

    def test_no_dpapi_means_no_saving(self):
        with mock.patch.object(credentials, "available", return_value=False):
            result = self.store.save("abc", "sk-test")
        self.assertFalse(result["ok"])
        self.assertIn("安全存储", result["error"])
        self.assertFalse(self.store.has("abc"))

    def test_a_corrupt_file_reads_as_empty(self):
        target = Path(self.store.folder) / "bad.bin"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"not a dpapi blob")
        self.assertEqual(self.store.load("bad"), "")


class ErrorMappingTests(unittest.TestCase):
    class Boom(Exception):
        def __init__(self, message, status_code=0):
            super().__init__(message)
            self.status_code = status_code

    def test_status_codes_win(self):
        self.assertEqual(errors.classify(self.Boom("x", 401)), errors.AUTH)
        self.assertEqual(errors.classify(self.Boom("x", 402)), errors.QUOTA)
        self.assertEqual(errors.classify(self.Boom("x", 429)), errors.RATE_LIMIT)
        self.assertEqual(errors.classify(self.Boom("x", 404)), errors.BAD_MODEL)
        self.assertEqual(errors.classify(self.Boom("x", 500)), errors.SERVER)
        self.assertEqual(errors.classify(self.Boom("x", 418)), errors.OTHER)

    def test_text_hints_work_without_status(self):
        self.assertEqual(errors.classify(text="invalid api key"), errors.AUTH)
        self.assertEqual(errors.classify(text="insufficient quota"), errors.QUOTA)
        self.assertEqual(errors.classify(text="model not found"), errors.BAD_MODEL)
        self.assertEqual(errors.classify(text="Request timed out"), errors.TIMEOUT)
        self.assertEqual(errors.classify(text="getaddrinfo failed"), errors.NETWORK)

    def test_redact_strips_keys_and_headers(self):
        text = "key=sk-abcdefghijklmnop Authorization: Bearer abcdefghijklmnop"
        cleaned = errors.redact(text)
        self.assertNotIn("sk-abcdefghijklmnop", cleaned)
        self.assertNotIn("abcdefghijklmnop", cleaned)

    def test_redact_strips_credential_fields_from_errors(self):
        text = "password=hunter2 api_key=abc123 token='two words'"
        cleaned = errors.redact(text)
        self.assertNotIn("hunter2", cleaned)
        self.assertNotIn("abc123", cleaned)
        self.assertNotIn("two words", cleaned)

    def test_friendly_carries_actionable_hint(self):
        line = errors.friendly(errors.AUTH, "DeepSeek", "错误详情")
        self.assertIn("认证失败", line)
        self.assertIn("控制台", line)
        self.assertIn("错误详情", line)

    def test_retryable_set(self):
        for kind in (errors.TIMEOUT, errors.RATE_LIMIT, errors.NETWORK, errors.SERVER):
            self.assertIn(kind, errors.RETRYABLE)
        self.assertNotIn(errors.AUTH, errors.RETRYABLE)


class ConfigRepositoryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = model_config.ModelConfigRepository(Path(tmp.name))

    def save(self, **overrides):
        payload = {
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-chat",
            "roles": ["companion"],
        }
        payload.update(overrides)
        return self.repo.upsert(payload)

    def test_save_and_read_back(self):
        result = self.save()
        self.assertTrue(result["ok"], result)
        rows = self.repo.list()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "deepseek-chat")
        self.assertEqual(self.repo.defaults()["companion"], rows[0]["id"])
        self.assertFalse((Path(self.repo.path).parent / "models.json").read_text(
            encoding="utf-8"
        ).count("api_key"))

    def test_http_is_only_allowed_for_localhost(self):
        self.assertFalse(self.save(base_url="http://example.com/v1")["ok"])
        self.assertTrue(self.save(base_url="http://127.0.0.1:11434/v1")["ok"])

    def test_url_with_credentials_or_query_is_refused(self):
        self.assertFalse(self.save(base_url="https://user:pass@example.com/v1")["ok"])
        self.assertFalse(self.save(base_url="https://example.com/v1?key=1")["ok"])

    def test_model_and_provider_are_required(self):
        self.assertFalse(self.save(model="")["ok"])
        self.assertFalse(self.save(provider="nope")["ok"])

    def test_roles_are_deduped_and_required(self):
        result = self.save(roles=["companion", "task", "companion"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["config"]["roles"], ["companion", "task"])
        self.assertFalse(self.save(roles=[])["ok"])

    def test_defaults_and_disable(self):
        first = self.save(model="deepseek-chat")["config"]
        second = self.save(model="deepseek-reasoner")["config"]
        self.assertTrue(self.repo.set_default("companion", second["id"])["ok"])
        self.assertEqual(self.repo.default("companion")["id"], second["id"])
        self.assertTrue(self.repo.set_enabled(second["id"], False)["ok"])
        # 默认项被禁用时退回第一个可用配置，不让聊天断掉。
        self.assertEqual(self.repo.default("companion")["id"], first["id"])

    def test_delete_clears_defaults_and_returns_secret(self):
        row = self.save()["config"]
        result = self.repo.delete(row["id"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["secret_id"], row["secret_id"])
        self.assertEqual(self.repo.defaults()["companion"], "")
        self.assertEqual(self.repo.list(), [])

    def test_missing_file_reads_as_empty(self):
        self.assertEqual(self.repo.list(), [])
        self.assertEqual(self.repo.default("companion"), None)

    def test_corrupt_file_reads_as_empty(self):
        Path(self.repo.path).write_text("{broken", encoding="utf-8")
        self.assertEqual(self.repo.list(), [])


class EnvMigrationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = FakeConfig(tmp.name)
        self.config.companion_base_url = "https://api.deepseek.com"
        self.config.companion_model = "deepseek-chat"
        self.config.companion_api_key = "sk-env-1234567890"

    def test_env_is_imported_once_and_keys_stay_out_of_json(self):
        created = model_config.import_env_configs(self.config)
        self.assertEqual(len(created), 1)
        row = self.config.model_configs.default("companion")
        self.assertEqual(row["provider"], "deepseek")
        self.assertEqual(self.config.secrets.load(row["secret_id"]), "sk-env-1234567890")
        self.assertNotIn(
            "sk-env-1234567890",
            Path(self.config.model_configs.path).read_text(encoding="utf-8"),
        )
        self.assertEqual(model_config.import_env_configs(self.config), [])

    def test_same_endpoint_for_both_roles_becomes_one_config(self):
        self.config.task_base_url = "https://api.deepseek.com"
        self.config.task_model = "deepseek-chat"
        self.config.task_api_key = "sk-env-1234567890"
        created = model_config.import_env_configs(self.config)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["roles"], ["companion", "task"])

    def test_nothing_configured_still_marks_migrated(self):
        empty = FakeConfig(tempfile.mkdtemp())
        self.assertEqual(model_config.import_env_configs(empty), [])
        self.assertTrue(empty.model_configs.read()["migrated_env"])


class ProviderTests(unittest.TestCase):
    def provider(self, name="deepseek", **kwargs):
        factory = adapter.PROVIDERS[name]
        provider = factory(
            base_url=kwargs.get("base_url", ""),
            api_key=kwargs.get("api_key", "sk-test"),
            model=kwargs.get("model", "deepseek-chat"),
            retries=kwargs.get("retries", 0),
        )
        provider._client = kwargs.get("client")
        return provider

    def test_each_provider_uses_its_official_default_address(self):
        self.assertEqual(adapter.OpenAIProvider.default_base_url, "https://api.openai.com/v1")
        self.assertEqual(adapter.DeepSeekProvider.default_base_url, "https://api.deepseek.com")
        self.assertIn("dashscope.aliyuncs.com", adapter.QwenProvider.default_base_url)
        self.assertEqual(adapter.CustomEndpointProvider.default_base_url, "")

    def test_unknown_env_provider_is_not_silently_replaced(self):
        config = FakeConfig(tempfile.mkdtemp())
        config.llm_provider = "mystery"
        self.assertIsNone(adapter.build_provider(config))

    def test_retryable_error_is_retried_once(self):
        class Boom(Exception):
            status_code = 503

        calls = []

        class Completions:
            def create(self, **kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    raise Boom("server error")
                message = mock.Mock(content="好", tool_calls=None)
                return mock.Mock(choices=[mock.Mock(message=message)], usage=None)

        client = mock.Mock()
        client.chat.completions = Completions()
        provider = self.provider(client=client, retries=1)
        with mock.patch.object(adapter.time, "sleep", return_value=None):
            result = provider.chat([{"role": "user", "content": "hi"}])
        self.assertTrue(result.ok, result)
        self.assertEqual(len(calls), 2)

    def test_auth_error_is_not_retried(self):
        class Boom(Exception):
            status_code = 401

        calls = []

        class Completions:
            def create(self, **kwargs):
                calls.append(kwargs)
                raise Boom("invalid api key")

        client = mock.Mock()
        client.chat.completions = Completions()
        provider = self.provider(client=client, retries=1)
        result = provider.chat([{"role": "user", "content": "hi"}])
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, errors.AUTH)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("sk-test", result.error)

    def test_probe_reports_success_and_latency(self):
        class Completions:
            def create(self, **kwargs):
                return mock.Mock()

        client = mock.Mock()
        client.chat.completions = Completions()
        result = self.provider(client=client).probe()
        self.assertTrue(result["ok"], result)
        self.assertGreaterEqual(result["latency_ms"], 0)

    def test_probe_maps_auth_failure(self):
        class Boom(Exception):
            status_code = 401

        class Completions:
            def create(self, **kwargs):
                raise Boom("invalid api key")

        client = mock.Mock()
        client.chat.completions = Completions()
        result = self.provider(client=client).probe()
        self.assertFalse(result["ok"])
        self.assertEqual(result["kind"], errors.AUTH)
        self.assertEqual(result["status"], 401)

    def test_unconfigured_provider_says_so(self):
        provider = self.provider(client=None)
        result = provider.chat([{"role": "user", "content": "hi"}])
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, errors.UNCONFIGURED)

    def test_stream_closes_the_upstream_on_cancel(self):
        closed = []

        class Stream:
            def __iter__(self):
                for text in ("一", "二", "三"):
                    yield mock.Mock(choices=[mock.Mock(delta=mock.Mock(content=text, tool_calls=None))])

            def close(self):
                closed.append(True)

        class Completions:
            def create(self, **kwargs):
                return Stream()

        client = mock.Mock()
        client.chat.completions = Completions()
        provider = self.provider(client=client)
        stream = provider.stream([{"role": "user", "content": "hi"}])
        self.assertEqual(next(stream).content, "一")
        stream.close()  # 模拟用户点停止：生成器被丢弃
        self.assertTrue(closed, "取消后要主动关掉底层连接")

    def test_list_models_returns_empty_on_failure(self):
        client = mock.Mock()
        client.models.list.side_effect = RuntimeError("nope")
        self.assertEqual(self.provider(client=client).list_models(), [])


if __name__ == "__main__":
    unittest.main()
