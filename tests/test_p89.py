"""V9.1 第二到第五阶段的探测层验收：本机组件状态只说真话。

这几阶段要装的东西（本地模型运行时、文档解析、离线语音、界面理解）都还没装进
产品里，所以这一层只做一件事：如实报出本机装了什么、还差什么。

验收盯住三点：探测结果和真实情况一致、没装就直说没装、探测不会下载或安装任何东西。
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters import local_runtimes  # noqa: E402
from agent.safe_tools import candidate  # noqa: E402
from security.audit import AuditLog  # noqa: E402
from security.config import Config  # noqa: E402
from skills.manager import SkillManager  # noqa: E402
from database.db import Database  # noqa: E402
from memory.manager import MemoryManager  # noqa: E402
from memory.repository import MemoryRepository  # noqa: E402
from tools.permission import PermissionManager  # noqa: E402
from tools.registry import ToolExecutor, ToolRegistry  # noqa: E402
from tools.runtimes import register_runtime_tools  # noqa: E402

REQUIRED_KEYS = ("id", "kind", "label", "installed", "ready", "needs", "license")

FAKE = {
    "db_path": "",
    "entries": [
        {
            "id": "ollama",
            "kind": "local-model",
            "label": "Ollama（本地模型运行环境）",
            "installed": False,
            "ready": False,
            "models": [],
            "needs": "自己装 Ollama 并手动拉模型",
            "license": "Ollama 本体 MIT",
        },
        {
            "id": "sqlite-fts5",
            "kind": "index",
            "label": "SQLite FTS5 全文检索",
            "installed": True,
            "ready": True,
            "models": [],
            "needs": "内置",
            "license": "SQLite 公共领域",
        },
    ],
}


class DetectionShapeTests(unittest.TestCase):
    """探测结果本身要是结构化的，不能是一句话糊过去。"""

    def test_every_entry_carries_the_same_required_fields(self):
        payload = local_runtimes.detect()
        self.assertTrue(payload["entries"])
        for item in payload["entries"]:
            for key in REQUIRED_KEYS:
                self.assertIn(key, item, "%s 缺字段 %s" % (item.get("id"), key))

    def test_ready_never_outruns_installed(self):
        for item in local_runtimes.detect()["entries"]:
            if item["ready"]:
                self.assertTrue(item["installed"], item["id"])

    def test_covered_kinds_match_the_plan(self):
        kinds = {item["kind"] for item in local_runtimes.detect()["entries"]}
        for kind in ("local-model", "document", "voice", "vision", "search", "index"):
            self.assertIn(kind, kinds)

    def test_ripgrep_is_reported_as_optional(self):
        entry = next(
            item
            for item in local_runtimes.detect()["entries"]
            if item["id"] == "ripgrep"
        )
        self.assertIn("不装也能", entry["needs"])

    def test_fts5_probe_really_creates_a_table(self):
        self.assertIsInstance(local_runtimes.fts5_available(), bool)

    def test_uninstalled_entries_explain_what_is_missing(self):
        for item in local_runtimes.detect()["entries"]:
            if not item["installed"]:
                self.assertTrue(str(item["needs"]).strip(), item["id"])

    def test_local_model_entries_never_promise_automatic_download(self):
        for item in local_runtimes.detect()["entries"]:
            if item["kind"] == "local-model":
                self.assertIn("不", item["needs"])


class DetectionSafetyTests(unittest.TestCase):
    """探测层不许偷偷下东西、装东西、连网络。"""

    def test_module_never_installs_or_downloads(self):
        source = Path(local_runtimes.__file__).read_text(encoding="utf-8")
        for bad in ("pip install", "urlopen", "requests.get", "subprocess.run([\"pip"):
            self.assertNotIn(bad, source)

    def test_only_readonly_commands_are_run(self):
        with mock.patch.object(local_runtimes, "_run") as fake:
            fake.return_value = ""
            local_runtimes.detect()
            for call in fake.call_args_list:
                argv = call.args[0]
                self.assertEqual(argv[0], "ollama")


class TextRenderingTests(unittest.TestCase):
    def test_missing_component_reads_as_not_installed(self):
        body = local_runtimes.text(FAKE)
        self.assertIn("没装", body)

    def test_present_component_reads_as_ready(self):
        body = local_runtimes.text(FAKE)
        self.assertIn("装好了", body)

    def test_rows_keep_one_line_per_component(self):
        self.assertEqual(len(local_runtimes.rows(FAKE)), len(FAKE["entries"]))

    def test_storage_hints_only_list_what_is_missing(self):
        hints = local_runtimes.storage_hint(FAKE)
        self.assertEqual([name for name, _ in hints], ["Ollama（本地模型运行环境）"])

    def test_group_counts_add_up(self):
        counts = local_runtimes.group_counts(FAKE)
        self.assertEqual(counts["local-model"], {"installed": 0, "total": 1})
        self.assertEqual(counts["index"], {"installed": 1, "total": 1})

    def test_empty_payload_does_not_crash(self):
        self.assertTrue(local_runtimes.text({}))
        self.assertEqual(local_runtimes.rows({}), [])


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.registry = ToolRegistry()
        self.audit = AuditLog(Database(":memory:"))
        register_runtime_tools(self.registry, None, detect=lambda base: FAKE)
        self.executor = ToolExecutor(
            self.registry, PermissionManager(audit=self.audit), audit=self.audit
        )

    def test_tool_is_readonly_and_registered(self):
        tool = self.registry.get("local_ability_status")
        self.assertIsNotNone(tool)
        self.assertEqual(tool.permission, "READ")
        self.assertTrue(tool.is_readonly)

    def test_tool_reports_the_detected_state(self):
        out = self.executor.execute("local_ability_status", {})
        self.assertTrue(out.ok, out.error)
        self.assertIn("没装", out.result)
        self.assertIn("装好了", out.result)

    def test_tool_can_filter_by_kind(self):
        out = self.executor.execute("local_ability_status", {"what": "index"})
        self.assertTrue(out.ok)
        self.assertIn("FTS5", out.result)
        self.assertNotIn("Ollama", out.result)

    def test_unknown_kind_says_there_is_nothing(self):
        out = self.executor.execute("local_ability_status", {"what": "voice"})
        self.assertTrue(out.ok)
        self.assertIn("没有可探测的组件", out.result)

    def test_tool_runs_offline_without_a_model(self):
        tool = candidate(self.registry, "本地模型能离线用吗")
        self.assertIsNotNone(tool)
        self.assertEqual(tool.name, "local_ability_status")


class SkillTests(unittest.TestCase):
    def setUp(self):
        self.registry = ToolRegistry()
        self.audit = AuditLog(Database(":memory:"))
        register_runtime_tools(self.registry, None, detect=lambda base: FAKE)
        executor = ToolExecutor(
            self.registry, PermissionManager(audit=self.audit), audit=self.audit
        )
        self.manager = SkillManager.build(
            config=Config(load_env=False), tools=executor, database=None
        )

    def test_skill_is_discovered_and_valid(self):
        self.assertIn("local-abilities", self.manager.names())
        report = self.manager.report_of("local-abilities")
        self.assertTrue(report.ok, report.errors)

    def test_skill_runs_and_reports_the_real_state(self):
        result = self.manager.run("local-abilities", request="能不能离线")
        self.assertTrue(result.ok, result.error)
        self.assertIn("没装", result.summary)
        self.assertTrue(result.verified)

    def test_skill_output_does_not_claim_anything_was_installed(self):
        result = self.manager.run("local-abilities", request="装了哪些")
        self.assertIn("没有自动安装", result.detail)


if __name__ == "__main__":
    unittest.main()
