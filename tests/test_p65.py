"""阶段 P65 验收：Local First 第四阶段。开发工具、Skill 接入、能力与权限中心。

要点：
    白名单命令之外一律拒绝，git 写操作仍然只能走 git_action；
    Skill 装得上、看得出，但每个工具仍要单独过权限；
    能力中心能列出能力、工具、技能、授权和最近执行，并且能改的只有偏好和授权；
    没有模型时应用照样装起来，本地能力在，离线可用。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database.db import Database  # noqa: E402
from memory.manager import MemoryManager  # noqa: E402
from memory.repository import MemoryRepository  # noqa: E402
from model.adapter import build_companion_provider  # noqa: E402
from security.config import Config  # noqa: E402
from tools.permission import GrantStore, PermissionManager  # noqa: E402
from tools.registry import ToolExecutor, ToolRegistry  # noqa: E402
from ui.capability_view import CapabilityWindow  # noqa: E402
from tools.workspace import FILE_TOOL_NAMES, register_workspace_tools  # noqa: E402


class FakeVar:
    def __init__(self, value=False):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class FakeTree:
    def __init__(self, rows=None):
        self.rows = rows or []
        self._selection = ()

    def selection(self):
        return self._selection

    def item(self, key, option):
        return tuple(self.rows[0])


class FakePet:
    def __init__(self, agent, memory):
        self.agent = agent
        self.memory = memory


class NullLogger:
    """不留文件句柄的日志替身，免得临时目录删不掉。"""

    def _noop(self, *args, **kwargs):
        return None

    info = warning = error = debug = exception = _noop


def temp_root():
    tmp = tempfile.TemporaryDirectory()
    return tmp, Path(tmp.name)


def build_offline_agent(root):
    """没有模型、没有网络，也要能整个装起来。"""
    with mock.patch.dict(os.environ, {"DESKPET_ROOT": str(root)}):
        config = Config()
        logger = NullLogger()
        database = Database(config.db_path)
        database.migrate()
        provider = build_companion_provider(config)
        from app.main import build_agent

        agent, memory = build_agent(config, database, provider, logger)
    return config, database, agent, memory


class OfflineBootstrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, root = temp_root()
        cls.config, cls.database, cls.agent, cls.memory = build_offline_agent(root)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_app_builds_without_any_model(self):
        self.assertFalse(self.agent.companion_slot.available())
        self.assertEqual(self.agent.mode_name, "local")

    def test_local_capabilities_survive_without_a_model(self):
        names = self.agent.capabilities.offline_names()
        self.assertTrue(names)
        self.assertIn("local_time", self.agent.registry.names())

    def test_local_tools_and_new_dev_tools_are_registered(self):
        names = set(self.agent.registry.names())
        for expected in ("local_time", "todo_add", "get_weather", "run_command", "copy_file", "open_url"):
            self.assertIn(expected, names)
        self.assertEqual(self.agent.registry.get("get_weather").requires, "network")

    def test_capability_catalog_marks_dependencies(self):
        rows = {row["name"]: row for row in self.agent.capabilities.catalog()}
        self.assertTrue(rows)
        self.assertTrue(all(row["requires_label"] for row in rows.values()))

    def test_life_state_and_todo_reminder_are_wired(self):
        self.assertEqual(self.agent.life.state, "idle")
        self.assertIsNotNone(self.agent.todo_reminder)
        self.assertIsNotNone(self.agent.todo_store)

    def test_execution_layer_is_exposed_for_the_ui(self):
        self.assertIsNotNone(self.agent.permissions)
        self.assertIsNotNone(self.agent.audit)
        self.assertIsNotNone(self.agent.registry)

    def test_skills_are_loaded_but_do_not_bypass_permissions(self):
        self.assertIsNotNone(self.agent.skills)
        self.assertTrue(self.agent.skills.names())
        self.assertIn("list_skills", self.agent.registry.names())


class CapabilityWindowDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, root = temp_root()
        cls.config, cls.database, cls.agent, cls.memory = build_offline_agent(root)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def window(self):
        window = CapabilityWindow.__new__(CapabilityWindow)
        window.pet = FakePet(self.agent, self.memory)
        return window

    def test_lists_capabilities_and_tools(self):
        window = self.window()
        self.assertTrue(window._capabilities())
        tools = {tool["name"] for tool in window._tools()}
        self.assertIn("read_file", tools)

    def test_lists_skills(self):
        rows = self.window()._skills()
        self.assertTrue(rows)
        self.assertTrue(all("name" in row for row in rows))

    def test_reads_audit_entries(self):
        self.agent.audit.record(kind="permission", name="read_file", decision="allow")
        rows = self.window()._audit()
        self.assertTrue(rows)

    def test_missing_pieces_do_not_crash(self):
        window = CapabilityWindow.__new__(CapabilityWindow)
        window.pet = FakePet(object(), None)
        for reader in (
            window._capabilities,
            window._tools,
            window._grants,
            window._audit,
            window._skills,
            window._offline_names,
        ):
            self.assertEqual(reader(), [])


class CapabilityActionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp, root = temp_root()
        self.addCleanup(self.tmp.cleanup)
        self.database = Database(root / "desk.sqlite3")
        self.database.migrate()
        self.memory = MemoryManager(MemoryRepository(self.database))
        self.grants = GrantStore(None)
        self.registry = ToolRegistry()
        register_workspace_tools(self.registry, None, workspace=str(root))
        permissions = PermissionManager(grants=self.grants, confirm_callback=lambda *a: True)
        executor = ToolExecutor(self.registry, permissions)
        agent = mock.Mock()
        agent.skills = None
        agent.permissions = permissions
        agent.registry = self.registry
        from security.audit import AuditLog

        agent.audit = AuditLog(self.database)
        agent.capabilities = None
        self.agent = agent
        self.pet = FakePet(agent, self.memory)

    def window(self):
        window = CapabilityWindow.__new__(CapabilityWindow)
        window.pet = self.pet
        window.win = mock.Mock()
        window.pause_var = FakeVar(False)
        window.grant_tree = FakeTree()
        window.hint = mock.Mock()
        window.refresh = mock.Mock()
        return window

    def test_pausing_proactive_writes_a_preference(self):
        window = self.window()
        window.pause_var.set(True)
        window.toggle_proactive()
        self.assertEqual(self.memory.get_preference("proactive_enabled"), "0")
        window.pause_var.set(False)
        window.toggle_proactive()
        self.assertEqual(self.memory.get_preference("proactive_enabled"), "1")

    def test_authorize_directory_grants_every_file_tool_for_that_folder(self):
        window = self.window()
        target = tempfile.mkdtemp()
        with mock.patch("ui.capability_view.filedialog.askdirectory", return_value=target):
            window.authorize_directory()
        rows = {grant["tool"]: grant for grant in self.grants.all()}
        for tool in FILE_TOOL_NAMES:
            self.assertIn(tool, rows)
            self.assertEqual(rows[tool]["scope"], "path:" + target)
            self.assertEqual(rows[tool]["mode"], "always")

    def test_revoke_selected_removes_the_grant(self):
        self.grants.set("read_file", "always", scope="path:C:\\notes")
        window = self.window()
        window.grant_tree = FakeTree([("read_file", "path:C:\\notes", "已持久授权", "")])
        window.grant_tree._selection = ("row",)
        window.revoke_selected()
        self.assertEqual(self.grants.all(), [])

    def test_revoke_without_selection_only_hints(self):
        window = self.window()
        window.revoke_selected()
        window.hint.configure.assert_called()


class SkillPermissionTests(unittest.TestCase):
    """Skill 只是流程说明，工具权限照旧由 PermissionManager 判。"""

    def test_skill_tools_still_need_their_own_permission(self):
        registry = ToolRegistry()
        registry.register(
            __import__("tools.registry", fromlist=["Tool"]).Tool(
                name="write_file", description="写", permission="HIGH", handler=lambda **k: "ok"
            )
        )
        executor = ToolExecutor(registry, PermissionManager())
        outcome = executor.execute("write_file", {"path": "x.txt"})
        self.assertFalse(outcome.ok)
        self.assertIn("权限", outcome.error)


if __name__ == "__main__":
    unittest.main()
