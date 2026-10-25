"""V9.1 第一阶段验收：本地能力补充。

这一阶段加的是「不用模型也能用」的那组东西：文本整理、任务待续、自检、备份。
验收只认真实行为：工具真的注册上了、技能真的过校验、备份真的落盘、
恢复没确认真的会被拒、技能真的绕不过权限中心。

全部用临时目录和临时数据库，不碰用户的真实数据。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.safe_tools import candidate  # noqa: E402
from agent.task import TaskRecord, TaskState  # noqa: E402
from agent.task_store import TaskStore  # noqa: E402
from database.db import Database  # noqa: E402
from memory.manager import MemoryManager  # noqa: E402
from memory.repository import MemoryRepository  # noqa: E402
from security.audit import AuditLog  # noqa: E402
from security.config import Config  # noqa: E402
from skills.manager import SkillManager  # noqa: E402
from tools.builtin import register_default_tools  # noqa: E402
from tools.local import register_local_tools  # noqa: E402
from tools.runtimes import register_runtime_tools  # noqa: E402
from tools.screen import register_screen_tools  # noqa: E402
from tools.voice import register_voice_tools  # noqa: E402
from tools.local_extra import (  # noqa: E402
    clean_text,
    count_text,
    dedupe_lines,
    register_extra_tools,
)
from tools.permission import PermissionManager  # noqa: E402
from tools.registry import ToolExecutor, ToolRegistry  # noqa: E402
from tools.knowledge import register_knowledge_tools  # noqa: E402
from knowledge.service import KnowledgeService  # noqa: E402
from devices.stack import build_stack  # noqa: E402
from tools.device_actions import register_device_tools  # noqa: E402

NEW_TOOLS = (
    "text_process",
    "task_unfinished",
    "run_diagnostics",
    "backup_list",
    "backup_create",
    "restore_backup",
)

NEW_SKILLS = (
    "local-text-processing",
    "local-task-recovery",
    "local-diagnostics",
    "local-backup",
)


class Base(unittest.TestCase):
    """一套临时环境：临时根目录、临时库、注册好的工具、真装上的技能。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="deskpet-p88-")
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"DESKPET_ROOT": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

        self.config = Config(load_env=False)
        try:
            self.config.ensure_dirs()
        except Exception:
            pass
        self.database = Database(self.config.db_path)
        self.database.migrate()
        self.audit = AuditLog(self.database)
        self.registry = ToolRegistry()
        self.registered = []
        register_default_tools(self.registry, self.config, self.database, workspace=True)
        register_local_tools(self.registry, database=self.database, config=self.config)
        register_screen_tools(self.registry, self.config)
        register_voice_tools(self.registry, self.config)
        register_runtime_tools(self.registry, self.config)
        # V9.1：知识库工具。技能声明了它们，不注册会被校验打回。
        register_knowledge_tools(
            self.registry,
            service=KnowledgeService(database=self.database, config=self.config),
        )
        register_extra_tools(
            self.registry,
            database=self.database,
            config=self.config,
            task_store=TaskStore(self.database),
            grants=lambda: [],
        )
        # V11：设备工具。设备技能声明了它们，不注册会被校验打回。
        register_device_tools(self.registry, build_stack())
        self.repository = MemoryRepository(self.database)
        self.memory = MemoryManager(self.repository)

    def executor(self, approve=None):
        callback = (lambda *a, **k: True) if approve else None
        permissions = PermissionManager(confirm_callback=callback, audit=self.audit)
        return ToolExecutor(self.registry, permissions, audit=self.audit)

    def manager(self, approve=None):
        return SkillManager.build(
            config=self.config,
            tools=self.executor(approve),
            memory=self.memory,
            database=self.database,
        )


class PureFunctionTests(unittest.TestCase):
    """文本处理的规则要能复算，同样的输入永远同样的输出。"""

    def test_clean_strips_bom_crlf_and_blank_runs(self):
        dirty = "\ufeff一\r\n二   \n\n\n\n三  "
        self.assertEqual(clean_text(dirty), "一\n二\n\n三")

    def test_clean_is_idempotent(self):
        once = clean_text("甲  \n\n\n\n乙")
        self.assertEqual(clean_text(once), once)

    def test_dedupe_keeps_first_and_counts_drops(self):
        body, dropped = dedupe_lines("甲\n乙\n甲\n丙\n乙")
        self.assertEqual(body, "甲\n乙\n丙")
        self.assertEqual(dropped, 2)

    def test_dedupe_leaves_blank_lines_alone(self):
        body, dropped = dedupe_lines("甲\n\n乙\n\n甲")
        self.assertEqual(dropped, 1)
        self.assertIn("\n\n", body)

    def test_count_text_counts_cjk_per_character(self):
        info = count_text("你好 world\n\n再见")
        self.assertEqual(info["chars"], len("你好 world\n\n再见"))
        self.assertEqual(info["blank"], 1)
        self.assertEqual(info["lines"], 3)
        self.assertEqual(info["words"], 5)


class ToolRegistrationTests(Base):
    """先把「装上了没有、等级对不对」钉住。"""

    def test_new_tools_are_registered(self):
        for name in NEW_TOOLS:
            self.assertIsNotNone(self.registry.get(name), name)

    def test_levels_match_what_they_do(self):
        self.assertEqual(self.registry.get("text_process").permission, "READ")
        self.assertEqual(self.registry.get("task_unfinished").permission, "READ")
        self.assertEqual(self.registry.get("run_diagnostics").permission, "READ")
        self.assertEqual(self.registry.get("backup_list").permission, "READ")
        self.assertEqual(self.registry.get("backup_create").permission, "LOW")
        self.assertEqual(self.registry.get("restore_backup").permission, "HIGH")

    def test_readonly_tools_say_so(self):
        for name in ("text_process", "task_unfinished", "run_diagnostics", "backup_list"):
            self.assertTrue(self.registry.get(name).is_readonly, name)
        self.assertFalse(self.registry.get("backup_create").is_readonly)
        self.assertFalse(self.registry.get("restore_backup").is_readonly)


class TextToolTests(Base):
    def run_tool(self, **kwargs):
        return self.executor().execute("text_process", kwargs)

    def test_clean_returns_the_tidied_text(self):
        out = self.run_tool(text="甲  \n\n\n\n乙", what="clean")
        self.assertTrue(out.ok)
        self.assertEqual(out.result, "甲\n\n乙")

    def test_stats_reports_real_numbers(self):
        out = self.run_tool(text="一\n二\n三", what="stats")
        self.assertTrue(out.ok)
        self.assertIn("行 3", out.result)

    def test_unknown_action_is_refused_not_swapped(self):
        out = self.run_tool(text="甲", what="大扫除")
        self.assertFalse(out.ok)
        self.assertIn("不认识", out.error)


class TaskRecoveryTests(Base):
    def test_no_tasks_reads_as_nothing_pending(self):
        out = self.executor().execute("task_unfinished", {})
        self.assertTrue(out.ok)
        self.assertIn("没有没做完的任务", out.result)

    def test_unfinished_task_is_listed_with_its_step(self):
        store = TaskStore(self.database)
        record = TaskRecord(goal="整理项目笔记", state=TaskState.RUNNING.value)
        record.plan = [{"title": "一"}, {"title": "二"}]
        record.current_step = 1
        store.save(record)
        store.replace_steps(record.id, [])

        out = self.executor().execute("task_unfinished", {"limit": 5})
        self.assertTrue(out.ok)
        self.assertIn("整理项目笔记", out.result)
        self.assertIn("第 1/2 步", out.result)

    def test_listing_never_changes_the_task_state(self):
        store = TaskStore(self.database)
        record = TaskRecord(goal="别动我", state=TaskState.RUNNING.value)
        store.save(record)
        self.executor().execute("task_unfinished", {})
        again = TaskStore(self.database).get(record.id)
        self.assertEqual(again.state, TaskState.RUNNING.value)

    def test_missing_task_store_says_so(self):
        registry = ToolRegistry()
        register_extra_tools(registry, database=self.database, config=self.config)
        executor = ToolExecutor(
            registry, PermissionManager(audit=self.audit), audit=self.audit
        )
        out = executor.execute("task_unfinished", {})
        self.assertFalse(out.ok)
        self.assertIn("没接上任务记录", out.error)


class DiagnosticsTests(Base):
    def test_reports_real_paths_and_writability(self):
        out = self.executor().execute("run_diagnostics", {})
        self.assertTrue(out.ok)
        self.assertIn(str(self.config.data_dir), out.result)
        self.assertIn("能写", out.result)

    def test_reports_unconfigured_models_without_alarm(self):
        out = self.executor().execute("run_diagnostics", {})
        self.assertIn("陪伴模型没配", out.result)
        self.assertIn("照常", out.result)

    def test_database_reported_as_openable(self):
        out = self.executor().execute("run_diagnostics", {})
        self.assertIn("能打开", out.result)

    def test_survives_a_broken_database(self):
        registry = ToolRegistry()
        register_extra_tools(registry, database=None, config=self.config)
        executor = ToolExecutor(
            registry, PermissionManager(audit=self.audit), audit=self.audit
        )
        out = executor.execute("run_diagnostics", {})
        self.assertTrue(out.ok)
        self.assertIn("没接上连接", out.result)


class BackupTests(Base):
    def test_list_says_there_are_none_yet(self):
        out = self.executor().execute("backup_list", {})
        self.assertTrue(out.ok)
        self.assertIn("还没有备份", out.result)

    def test_create_really_writes_a_file(self):
        out = self.executor().execute("backup_create", {})
        self.assertTrue(out.ok, out.error)
        written = Path(out.result.split("：", 1)[1].strip())
        self.assertTrue(written.is_file())
        self.assertTrue(written.name.endswith(".sqlite3"))

    def test_list_shows_the_new_backup(self):
        self.executor(approve=True).execute("backup_create", {})
        out = self.executor().execute("backup_list", {})
        self.assertIn("备份", out.result)
        self.assertNotIn("还没有备份", out.result)

    def test_create_leaves_the_original_database_alone(self):
        self.database.execute(
            "INSERT OR REPLACE INTO preferences (key, value) VALUES (?, ?)",
            ("p88_probe", "keep-me"),
        )
        self.executor().execute("backup_create", {})
        row = self.database.query_one(
            "SELECT value FROM preferences WHERE key = ?", ("p88_probe",)
        )
        self.assertEqual(row["value"], "keep-me")

    def test_restore_needs_an_explicit_confirmation(self):
        out = self.executor(approve=True).execute(
            "restore_backup", {"path": self.config.db_path}
        )
        self.assertFalse(out.ok)
        self.assertIn("确认", out.error)

    def test_restore_refuses_a_file_that_is_not_a_backup(self):
        junk = Path(self.tmp.name) / "junk.sqlite3"
        junk.write_text("这不是数据库", encoding="utf-8")
        out = self.executor(approve=True).execute(
            "restore_backup", {"path": str(junk), "confirm": True}
        )
        self.assertFalse(out.ok)
        self.assertIn("不能用", out.error)

    def test_restore_without_a_confirm_channel_never_runs(self):
        backup = self.executor().execute("backup_create", {})
        self.assertTrue(backup.ok)
        path = backup.result.split("：", 1)[1].strip()
        out = self.executor().execute(
            "restore_backup", {"path": path, "confirm": True}
        )
        self.assertFalse(out.ok)
        self.assertTrue(out.needs_confirmation)

    def test_confirmed_restore_keeps_a_copy_of_the_old_database(self):
        backup = self.executor(approve=True).execute("backup_create", {})
        path = backup.result.split("：", 1)[1].strip()
        out = self.executor(approve=True).execute(
            "restore_backup", {"path": path, "confirm": True}
        )
        self.assertTrue(out.ok, out.error)
        self.assertIn("恢复好了", out.result)
        copies = list(Path(self.config.db_path).parent.glob("*before-restore*"))
        self.assertTrue(copies, "覆盖前的那一份没有留下")


class OfflineDirectRunTests(Base):
    """没有模型时，只读工具要能被程序直接跑起来。"""

    def test_unfinished_tasks_is_directly_runnable(self):
        tool = candidate(self.registry, "还有没做完的任务吗")
        self.assertIsNotNone(tool)
        self.assertEqual(tool.name, "task_unfinished")

    def test_diagnostics_is_directly_runnable(self):
        tool = candidate(self.registry, "自检")
        self.assertIsNotNone(tool)
        self.assertEqual(tool.name, "run_diagnostics")

    def test_restore_is_never_directly_runnable(self):
        self.assertIsNone(candidate(self.registry, "恢复备份"))

    def test_backup_create_is_not_directly_runnable(self):
        self.assertIsNone(candidate(self.registry, "备份"))


class SkillLibraryTests(Base):
    def test_new_skills_are_discovered(self):
        names = self.manager().names()
        for name in NEW_SKILLS:
            self.assertIn(name, names)

    def test_new_skills_pass_validation(self):
        manager = self.manager()
        for name in NEW_SKILLS:
            report = manager.report_of(name)
            self.assertIsNotNone(report, name)
            self.assertTrue(report.ok, "%s: %s" % (name, report.errors))

    def test_every_shipped_skill_still_validates(self):
        manager = self.manager()
        broken = {
            name: manager.report_of(name).errors
            for name in manager.names()
            if not manager.report_of(name).ok
        }
        self.assertEqual(broken, {})

    def test_skills_declare_only_tools_that_exist(self):
        manager = self.manager()
        for name in NEW_SKILLS:
            declared = list(getattr(manager.get(name), "tools", ()) or ())
            self.assertTrue(declared, name)
            for tool in declared:
                self.assertIsNotNone(self.registry.get(tool), "%s -> %s" % (name, tool))


class SkillRunTests(Base):
    def test_text_processing_runs_end_to_end(self):
        result = self.manager().run(
            "local-text-processing", request="整理一下", inputs={"text": "甲  \n\n\n乙"}
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.summary, "甲\n\n乙")
        self.assertTrue(result.verified)

    def test_text_processing_refuses_empty_input(self):
        result = self.manager().run("local-text-processing", inputs={"text": "   "})
        self.assertFalse(result.ok)
        self.assertIn("正文", result.error)

    def test_task_recovery_runs_end_to_end(self):
        result = self.manager().run("local-task-recovery", request="上次的任务做完了吗")
        self.assertTrue(result.ok, result.error)
        self.assertIn("没有没做完的任务", result.summary)

    def test_diagnostics_skill_runs_end_to_end(self):
        result = self.manager().run("local-diagnostics", request="自检")
        self.assertTrue(result.ok, result.error)
        self.assertIn("数据目录", result.summary)

    def test_backup_skill_lists_without_asking_anything(self):
        result = self.manager().run("local-backup", request="有哪些备份")
        self.assertTrue(result.ok, result.error)
        self.assertIn("还没有备份", result.summary)


class SkillCannotEscalateTests(Base):
    """技能不给工具提权：恢复照样被权限中心拦住。"""

    def test_restore_step_is_blocked_without_approval(self):
        manager = self.manager()
        result = manager.run(
            "local-backup",
            request="恢复备份",
            inputs={"action": "restore", "path": self.config.db_path, "confirm": "确认"},
        )
        self.assertFalse(result.ok)
        self.assertIn("权限", result.error)

    def test_restore_without_confirm_wording_never_calls_the_tool(self):
        result = self.manager(approve=True).run(
            "local-backup",
            request="恢复备份",
            inputs={"action": "restore", "path": self.config.db_path},
        )
        self.assertFalse(result.ok)
        self.assertIn("确认", result.error)

    def test_extra_tools_do_not_touch_user_data_root(self):
        # 临时根目录之外没有写任何东西：备份只落在临时库旁边。
        self.executor().execute("backup_create", {})
        backups = Path(self.config.db_path).parent / "backups"
        self.assertTrue(backups.is_dir())
        self.assertTrue(str(backups).startswith(self.tmp.name))


if __name__ == "__main__":
    unittest.main()
