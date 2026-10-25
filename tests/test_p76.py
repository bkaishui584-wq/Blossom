"""阶段 P76 验收：V7 阶段 5 的系统助手技能与阶段 6 的分发收尾。

要点：
    系统助手只报本机真实读数，工具失败就说失败；
    它声明什么工具、就只用什么工具，权限照旧走 PermissionManager；
    诊断包里有版本、路径和日志，密钥永远不进包；
    第一次打开会讲清数据在哪、权限怎么给，看过一次就不再打扰。
"""

import os
import sys
import tempfile
import tkinter as tk
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.context import ContextManager
from agent.core import AgentCore
from database.db import Database
from logkit import report as report_module
from logkit.logger import register_secret
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatDelta, ChatResult, ModelProvider, Usage
from security.config import Config
from skills.manager import SkillManager
from tools.builtin import register_default_tools
from tools.local import register_local_tools
from tools.permission import PermissionManager
from tools.registry import ToolExecutor, ToolRegistry
from ui import wizard
from ui.desktop import DesktopPet

SECRET = "hunter2-not-a-real-key"


class StubProvider(ModelProvider):
    name = "stub"

    def is_available(self):
        return True

    def chat(self, messages, tools=None, temperature=0.7):
        return ChatResult(ok=True, content="在的。", usage=Usage(5, 3), model="stub")

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


class SkillEnv(unittest.TestCase):
    """真装配：真工具、真权限、真临时库。"""

    confirm = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {"DESKPET_ROOT": str(self.base)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = Config(load_env=False)
        self.database = Database(self.config.db_path)
        self.database.migrate()
        self.registry = ToolRegistry()
        register_default_tools(self.registry, self.config, self.database, workspace=False)
        register_local_tools(self.registry, database=self.database, config=self.config)
        callback = (lambda *args, **kwargs: True) if self.confirm else None
        self.permissions = PermissionManager(confirm_callback=callback)
        self.executor = ToolExecutor(self.registry, self.permissions)
        self.manager = SkillManager.build(
            config=self.config, tools=self.executor, database=self.database
        )


class SystemAssistantTests(SkillEnv):
    def test_it_ships_with_the_other_skills(self):
        self.assertIn("system-assistant", self.manager.names())
        meta = self.manager.get("system-assistant")
        self.assertTrue(meta.enabled)
        self.assertEqual(meta.permission, "READ")

    def test_it_declares_only_tools_that_really_exist(self):
        meta = self.manager.get("system-assistant")
        self.assertTrue(meta.tools)
        for name in meta.tools:
            self.assertIn(name, self.registry.names(), name)

    def test_it_reads_the_clock(self):
        result = self.manager.run("system-assistant", request="现在几点了")
        self.assertTrue(result.ok, result.to_dict())
        self.assertTrue(result.verified)
        self.assertEqual(result.tools, ["local_time"])
        self.assertTrue(any(ch.isdigit() for ch in result.summary))

    def test_it_reads_the_machine_status(self):
        result = self.manager.run("system-assistant", request="看看电量和内存")
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(result.tools, ["system_status"])

    def test_it_lists_the_todos(self):
        result = self.manager.run("system-assistant", request="我的待办有哪些")
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(result.tools, ["todo_list"])

    def test_an_explicit_action_wins_over_the_wording(self):
        result = self.manager.run(
            "system-assistant", request="几点", inputs={"action": "status"}
        )
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(result.tools, ["system_status"])

    def test_an_unknown_action_still_reads_the_clock(self):
        result = self.manager.run(
            "system-assistant", request="随便看看", inputs={"action": "no-such-action"}
        )
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(result.tools, ["local_time"])

    def test_it_runs_without_any_model(self):
        self.assertFalse(self.config.has_llm())
        self.assertFalse(self.config.has_companion())
        result = self.manager.run("system-assistant", request="现在几点")
        self.assertTrue(result.ok, result.to_dict())

    def test_a_failing_tool_is_not_reported_as_success(self):
        def broken(**_kwargs):
            raise RuntimeError("读不到")

        self.registry.register(
            type(self.registry.get("local_time"))(
                name="local_time",
                description="坏掉的时钟",
                permission="READ",
                handler=broken,
                readonly=True,
            )
        )
        result = self.manager.run("system-assistant", request="现在几点")
        self.assertFalse(result.ok)
        self.assertIn("读不到", result.error)

    def test_it_cannot_bypass_the_permission_layer(self):
        self.registry.get("local_time").permission = "HIGH"
        manager = SkillManager.build(
            config=self.config,
            tools=ToolExecutor(self.registry, PermissionManager(confirm_callback=None)),
            database=self.database,
        )
        result = manager.run("system-assistant", request="现在几点")
        self.assertFalse(result.ok)
        self.assertTrue(result.error)

    def test_unrelated_talk_does_not_match_it(self):
        names = [item.name for item in self.manager.recommend("帮我写一首关于春天的诗")]
        self.assertNotIn("system-assistant", names)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.config = Config(load_env=False)
        self.config.data_dir = self.base
        self.config.log_dir = self.base / "logs"
        self.config.db_path = self.base / "t.sqlite3"

    def test_the_report_names_the_version_and_python(self):
        text = report_module.build_report(self.config)
        self.assertIn("Blossom", text)
        self.assertIn("Python", text)
        self.assertIn("生成时间", text)

    def test_the_report_lists_the_real_paths(self):
        text = report_module.build_report(self.config)
        self.assertIn(str(self.base), text)

    def test_the_report_never_carries_a_key(self):
        self.config.llm_api_key = SECRET
        register_secret(SECRET)
        text = report_module.build_report(self.config)
        self.assertNotIn(SECRET, text)
        self.assertIn("密钥是否已配置：是", text)

    def test_it_says_when_no_key_is_configured(self):
        self.config.llm_api_key = ""
        text = report_module.build_report(self.config)
        self.assertIn("密钥是否已配置：否", text)

    def test_a_missing_config_still_produces_a_report(self):
        text = report_module.build_report(None)
        self.assertIn("Blossom", text)

    def test_export_writes_a_zip_with_the_report(self):
        target = self.base / "out" / "diag.zip"
        result = report_module.export_report(self.config, target=target)
        self.assertTrue(result["ok"], result)
        self.assertTrue(target.is_file())
        with zipfile.ZipFile(target) as bundle:
            self.assertIn("report.txt", bundle.namelist())
            self.assertIn("Blossom", bundle.read("report.txt").decode("utf-8"))

    def test_export_carries_the_logs_and_masks_secrets(self):
        self.config.log_dir.mkdir(parents=True, exist_ok=True)
        (self.config.log_dir / "deskpet.log").write_text(
            "INFO 启动\nINFO api_key=%s\nINFO password=hunter2-not-real\n"
            "INFO token=token-not-real\n" % SECRET,
            encoding="utf-8",
        )
        register_secret(SECRET)
        target = self.base / "diag.zip"
        result = report_module.export_report(self.config, target=target)
        self.assertTrue(result["ok"], result)
        with zipfile.ZipFile(target) as bundle:
            names = bundle.namelist()
            self.assertIn("logs/deskpet.log", names)
            text = bundle.read("logs/deskpet.log").decode("utf-8")
        self.assertNotIn(SECRET, text)
        self.assertNotIn("hunter2-not-real", text)
        self.assertNotIn("token-not-real", text)
        self.assertIn("***", text)

    def test_export_without_logs_still_works(self):
        target = self.base / "diag.zip"
        result = report_module.export_report(self.config, target=target)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["files"], ["report.txt"])

    def test_export_reports_failure_instead_of_pretending(self):
        blocker = self.base / "blocker.txt"
        blocker.write_text("not a folder", encoding="utf-8")
        result = report_module.export_report(self.config, target=blocker / "diag.zip")
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])


class TkTestCase(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        self.root.withdraw()
        self.addCleanup(self._safe_destroy)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.config = Config(load_env=False)
        self.config.data_dir = self.base
        self.config.log_dir = self.base / "logs"
        self.config.db_path = self.base / "t.sqlite3"
        self.config.characters_dir = self.base / "characters"
        self.config.live2d_dir = self.base / "live2d"
        self.config.devices_dir = self.base / "devices"

    def _safe_destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def build_pet(self):
        database = Database(self.config.db_path)
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        agent = AgentCore(StubProvider(), memory, ContextManager(token_budget=800))
        pet = DesktopPet(agent, self.config, memory=memory, root=self.root)
        return pet, memory


class FirstRunTests(TkTestCase):
    def test_a_fresh_install_has_not_seen_it(self):
        pet, _memory = self.build_pet()
        self.assertFalse(wizard.is_done(pet))
        self.assertFalse(wizard.looks_used(pet))

    def test_finishing_is_remembered(self):
        pet, _memory = self.build_pet()
        wizard.mark_done(pet)
        self.assertTrue(wizard.is_done(pet))

    def test_an_old_library_skips_it_quietly(self):
        pet, memory = self.build_pet()
        memory.remember("用户喜欢手冲咖啡", kind="long_term")
        self.assertTrue(wizard.looks_used(pet))
        self.assertIsNone(pet._maybe_first_run())
        self.assertTrue(wizard.is_done(pet))

    def test_the_text_tells_where_the_data_lives(self):
        pet, _memory = self.build_pet()
        text = wizard.body_text(pet, self.config)
        self.assertIn(str(self.base), text)
        self.assertIn("权限", text)
        self.assertIn("模型可以不配", text)

    def test_the_text_carries_no_secret(self):
        pet, _memory = self.build_pet()
        self.config.llm_api_key = SECRET
        text = wizard.body_text(pet, self.config)
        self.assertNotIn(SECRET, text)

    def test_the_window_opens_once(self):
        pet, _memory = self.build_pet()
        first = pet._maybe_first_run()
        self.assertIsInstance(first, wizard.FirstRunWizard)
        self.addCleanup(lambda: first.win.winfo_exists() and first.win.destroy())
        first.finish()
        self.assertIsNone(pet._maybe_first_run())

    def test_a_fresh_install_gets_the_window(self):
        pet, _memory = self.build_pet()
        window = wizard.FirstRunWizard(pet)
        self.addCleanup(lambda: window.win.winfo_exists() and window.win.destroy())
        self.assertTrue(window.win.winfo_exists())
        self.assertTrue(window.open_settings() is True or window.open_settings() is False)
        self.assertTrue(window.finish())

    def test_export_diagnostics_writes_a_file(self):
        pet, _memory = self.build_pet()
        target = self.base / "diag" / "here.zip"
        result = pet.export_diagnostics(target=str(target))
        self.assertTrue(result["ok"], result)
        self.assertTrue(target.is_file())

    def test_export_diagnostics_can_be_cancelled(self):
        pet, _memory = self.build_pet()
        with mock.patch("ui.desktop.filedialog.asksaveasfilename", return_value=""):
            self.assertIsNone(pet.export_diagnostics())


if __name__ == "__main__":
    unittest.main()
