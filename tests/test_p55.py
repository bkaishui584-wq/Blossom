"""阶段 P55 验收：Local First 第一阶段。

覆盖四件事：

1. 没有 API Key、没有网络时，时间、系统状态、计时器、待办这些本地工具照样能用。
2. 能力注册表能说清「谁不用模型、谁要联网、现在能不能用」。
3. 权限中心：范围授权、单次授权、每次确认、拒绝、撤销、路径穿越、危险等级。
4. 待办表是增量迁移，旧数据不动。
"""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.capabilities import (  # noqa: E402
    REQUIRES_LOCAL,
    REQUIRES_MODEL,
    REQUIRES_NETWORK,
    CapabilityRouter,
    LocalCapability,
    LocalCapabilityRegistry,
)
from agent.context import ContextManager  # noqa: E402
from agent.core import AgentCore  # noqa: E402
from database.db import Database  # noqa: E402
from memory.manager import MemoryManager  # noqa: E402
from memory.repository import MemoryRepository  # noqa: E402
from memory.todo import TodoStore  # noqa: E402
from model.base import ChatDelta, ChatResult, ModelProvider, Usage  # noqa: E402
from security.config import Config  # noqa: E402
from tools.local import register_local_tools, uptime_text  # noqa: E402
from tools.permission import (  # noqa: E402
    GrantStore,
    PermissionLevel,
    PermissionManager,
    request_scope,
)
from tools.registry import ToolRegistry, ToolExecutor  # noqa: E402
from tools.builtin import weather_tool, news_tool  # noqa: E402


class StubProvider(ModelProvider):
    name = "stub"

    def is_available(self):
        return True

    def chat(self, messages, tools=None, temperature=0.7):
        return ChatResult(
            ok=True, content="在的。", usage=Usage(prompt_tokens=3, completion_tokens=2), model="stub"
        )

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


class SilentAudit:
    def __init__(self):
        self.rows = []

    def record(self, **kwargs):
        self.rows.append(kwargs)


class FakeScheduler:
    """只记账，不起线程。"""

    def __init__(self):
        self.calls = []

    def schedule_once(self, seconds, **kwargs):
        self.calls.append((seconds, kwargs))
        return None


def temp_db():
    tmp = tempfile.TemporaryDirectory()
    database = Database(Path(tmp.name) / "deskpet.sqlite3")
    database.migrate()
    return tmp, database


def build_registry(database=None, timer=None):
    registry = ToolRegistry()
    register_local_tools(registry, database=database, timer=timer)
    return registry


def executor(registry, grants=None, confirm=None, audit=None):
    permissions = PermissionManager(grants=grants, confirm_callback=confirm, audit=audit)
    return ToolExecutor(registry, permissions)


class OfflineLocalToolsTests(unittest.TestCase):
    """断网、没模型时，本地工具必须照常能用。"""

    def setUp(self):
        self.tmp, self.db = temp_db()
        self.addCleanup(self.tmp.cleanup)
        self.registry = build_registry(self.db, FakeScheduler())
        self.exec = executor(self.registry)

    def test_time_reads_the_local_clock(self):
        out = self.exec.execute("local_time", {"what": "date"})
        self.assertTrue(out.ok, out.error)
        self.assertIn(datetime.now().strftime("%Y-%m-%d"), out.result)

    def test_system_status_says_something_real(self):
        out = self.exec.execute("system_status", {})
        self.assertTrue(out.ok, out.error)
        self.assertTrue("开机" in out.result or "读不到" in out.result)

    def test_battery_and_memory_never_claim_a_number_they_lack(self):
        out = self.exec.execute("system_status", {})
        self.assertTrue("电量" in out.result or "电池" in out.result)
        self.assertTrue("内存" in out.result or "读不到" in out.result)

    def test_uptime_text_admits_when_it_cannot_read(self):
        with mock.patch("tools.local.uptime_seconds", return_value=None):
            self.assertEqual(uptime_text(), "读不到开机时长。")
        self.assertEqual(uptime_text(0), "开机 0 分钟。")

    def test_everything_needed_is_local_only(self):
        rows = {row["name"]: row for row in self.registry.catalog()}
        for name in ("local_time", "system_status", "timer_start", "todo_add", "todo_list", "todo_done"):
            self.assertEqual(rows[name]["requires"], REQUIRES_LOCAL, name)

    def test_no_model_and_no_key_still_runs(self):
        config = Config(load_env=False)
        self.assertFalse(config.has_companion() and config.has_task())

    def test_timer_without_a_scheduler_says_it_cannot(self):
        registry = build_registry(self.db, timer=None)
        out = executor(registry).execute("timer_start", {"minutes": 1})
        self.assertFalse(out.ok)
        self.assertIn("桌面程序", out.error)

    def test_timer_asks_for_a_length(self):
        out = self.exec.execute("timer_start", {"subject": "喝水"})
        self.assertFalse(out.ok)
        self.assertIn("多久", out.error)

class TimerSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.db = temp_db()
        self.addCleanup(self.tmp.cleanup)
        self.scheduler = FakeScheduler()
        self.exec = executor(build_registry(self.db, self.scheduler))

    def test_a_countdown_lands_on_the_scheduler(self):
        out = self.exec.execute("timer_start", {"minutes": 5, "subject": "喝水"})
        self.assertTrue(out.ok, out.error)
        self.assertEqual([call[0] for call in self.scheduler.calls], [300])
        self.assertIn("喝水", self.scheduler.calls[0][1]["local_text"])

    def test_seconds_and_minutes_add_up(self):
        self.exec.execute("timer_start", {"minutes": 1, "seconds": 30})
        self.assertEqual([call[0] for call in self.scheduler.calls], [90])

    def test_pomodoro_schedules_both_ends(self):
        out = self.exec.execute("pomodoro", {"minutes": 25, "break_minutes": 5})
        self.assertTrue(out.ok, out.error)
        self.assertEqual([call[0] for call in self.scheduler.calls], [1500, 1800])

    def test_pomodoro_without_a_break_only_schedules_the_focus(self):
        self.exec.execute("pomodoro", {"minutes": 20, "break_minutes": 0})
        self.assertEqual([call[0] for call in self.scheduler.calls], [1200])


class TodoTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.db = temp_db()
        self.addCleanup(self.tmp.cleanup)
        self.exec = executor(build_registry(self.db))

    def test_a_todo_goes_in_and_comes_back(self):
        added = self.exec.execute("todo_add", {"title": "写周报", "due": "2026-10-12 18:00"})
        self.assertTrue(added.ok, added.error)
        listed = self.exec.execute("todo_list", {})
        self.assertIn("写周报", listed.result)
        self.assertIn("2026-10-12 18:00", listed.result)

    def test_a_todo_without_a_deadline_is_allowed(self):
        added = self.exec.execute("todo_add", {"title": "买牛奶"})
        self.assertTrue(added.ok, added.error)
        self.assertIn("没定时间", self.exec.execute("todo_list", {}).result)

    def test_a_bad_time_is_refused_instead_of_guessed(self):
        out = self.exec.execute("todo_add", {"title": "写周报", "due": "下周三"})
        self.assertFalse(out.ok)
        self.assertIn("时间", out.error)

    def test_an_empty_todo_is_refused(self):
        self.assertFalse(self.exec.execute("todo_add", {"title": "   "}).ok)

    def test_done_marks_the_state_and_keeps_the_row(self):
        self.exec.execute("todo_add", {"title": "写周报"})
        done = self.exec.execute("todo_done", {"todo_id": 1})
        self.assertTrue(done.ok, done.error)
        self.assertEqual(self.db.query_one("SELECT state FROM todos WHERE id = 1")["state"], "done")
        self.assertNotIn("写周报", self.exec.execute("todo_list", {}).result)

    def test_done_twice_says_it_was_already_done(self):
        self.exec.execute("todo_add", {"title": "写周报"})
        self.exec.execute("todo_done", {"todo_id": 1})
        again = self.exec.execute("todo_done", {"todo_id": 1})
        self.assertTrue(again.ok)
        self.assertIn("本来就是", again.result)

    def test_done_on_a_missing_row_is_refused(self):
        out = self.exec.execute("todo_done", {"todo_id": 99})
        self.assertFalse(out.ok)
        self.assertIn("没有编号", out.error)

    def test_earlier_deadlines_come_first(self):
        self.exec.execute("todo_add", {"title": "晚一点", "due": "2026-10-20 09:00"})
        self.exec.execute("todo_add", {"title": "早一点", "due": "2026-10-12 09:00"})
        self.exec.execute("todo_add", {"title": "没有期限"})
        rows = self.db.query("SELECT title FROM todos ORDER BY due_at ASC, id ASC")
        listed = self.exec.execute("todo_list", {}).result
        self.assertLess(listed.index("早一点"), listed.index("晚一点"))
        self.assertLess(listed.index("晚一点"), listed.index("没有期限"))
        self.assertEqual(len(rows), 3)

    def test_overdue_is_marked_against_the_injected_clock(self):
        clock = datetime(2026, 10, 12, 9, 0)
        store = TodoStore(self.db, clock=lambda: clock)
        store.add("过期的", due_at="2026-10-12 08:00")
        rows = store.list()["data"]["todos"]
        self.assertTrue(rows[0]["overdue"])

    def test_due_lists_only_what_is_due_and_open(self):
        clock = datetime(2026, 10, 12, 9, 0)
        store = TodoStore(self.db, clock=lambda: clock)
        store.add("到点了", due_at="2026-10-12 08:00")
        store.add("还没到", due_at="2026-10-12 20:00")
        store.add("没时间")
        self.assertEqual([row["title"] for row in store.due()], ["到点了"])
        store.done(1)
        self.assertEqual(store.due(), [])

    def test_open_count_tracks_the_open_ones(self):
        store = TodoStore(self.db)
        store.add("一")
        store.add("二")
        store.done(1)
        self.assertEqual(store.open_count(), 1)

    def test_remove_only_touches_the_named_row(self):
        store = TodoStore(self.db)
        store.add("一")
        store.add("二")
        self.assertTrue(store.remove(1)["ok"])
        self.assertEqual([row["title"] for row in store.list()["data"]["todos"]], ["二"])


class MigrationTests(unittest.TestCase):
    """待办表是追加的迁移，旧库里的数据必须原样还在。"""

    def test_a_legacy_database_keeps_its_rows(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        database = Database(Path(tmp.name) / "old.sqlite3")
        database.migrate()
        database.insert(
            "INSERT INTO messages (session_id, role, content) VALUES ('s1', 'user', '旧消息')"
        )
        database.insert(
            "INSERT INTO memories (kind, content) VALUES ('fact', '用户在上海')"
        )
        # 退回只应用了 1..12 的状态，模拟升级前的老库。
        database.execute("DROP TABLE todos")
        database.execute("DELETE FROM schema_migrations WHERE version = 13")

        applied = database.migrate()
        self.assertIn(13, applied)
        self.assertEqual(
            database.query_one("SELECT content FROM messages")["content"], "旧消息"
        )
        self.assertEqual(
            database.query_one("SELECT content FROM memories")["content"], "用户在上海"
        )
        self.assertIn("todos", database.health()["tables"])

    def test_migrating_twice_changes_nothing(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        database = Database(Path(tmp.name) / "again.sqlite3")
        self.assertTrue(database.migrate())
        self.assertEqual(database.migrate(), [])


class CapabilityRegistryTests(unittest.TestCase):
    def registry(self, probes=None):
        return LocalCapabilityRegistry(
            probes=probes,
            skills=[
                LocalCapability("time", "报时间", lambda text, ctx: "现在 9 点", ("时间",)),
                LocalCapability(
                    "weather",
                    "报天气",
                    lambda text, ctx: None,
                    ("天气",),
                    requires=REQUIRES_NETWORK,
                ),
                LocalCapability(
                    "chat",
                    "聊天",
                    lambda text, ctx: None,
                    ("聊聊",),
                    requires=REQUIRES_MODEL,
                ),
            ],
        )

    def test_the_catalog_names_what_each_capability_needs(self):
        rows = {row["name"]: row for row in self.registry().catalog()}
        self.assertEqual(rows["time"]["requires"], REQUIRES_LOCAL)
        self.assertEqual(rows["weather"]["requires_label"], "需要网络")
        self.assertEqual(rows["chat"]["requires_label"], "需要模型")

    def test_a_probe_decides_whether_it_is_ready(self):
        registry = self.registry(probes={"weather": lambda: (False, "没配天气数据源")})
        rows = {row["name"]: row for row in registry.catalog()}
        self.assertFalse(rows["weather"]["ready"])
        self.assertEqual(rows["weather"]["reason"], "没配天气数据源")

    def test_a_broken_probe_counts_as_not_ready(self):
        def boom():
            raise RuntimeError("坏了")

        rows = {row["name"]: row for row in self.registry(probes={"time": boom}).catalog()}
        self.assertFalse(rows["time"]["ready"])

    def test_offline_names_are_the_local_ones(self):
        self.assertEqual(self.registry().offline_names(), ["time"])

    def test_it_still_routes_like_the_plain_router(self):
        registry = self.registry()
        self.assertIsInstance(registry, CapabilityRouter)
        answer = registry.resolve("现在时间")
        self.assertEqual(answer.capability, "time")

    def test_add_carries_the_dependency(self):
        registry = self.registry()
        registry.add(LocalCapability("news", "报新闻", lambda t, c: None, ("新闻",)), REQUIRES_NETWORK)
        rows = {row["name"]: row for row in registry.catalog()}
        self.assertEqual(rows["news"]["requires"], REQUIRES_NETWORK)

    def test_the_agent_uses_the_registry_by_default(self):
        tmp, database = temp_db()
        self.addCleanup(tmp.cleanup)
        memory = MemoryManager(MemoryRepository(database))
        agent = AgentCore(StubProvider(), memory, ContextManager(token_budget=800))
        self.assertIsInstance(agent.capabilities, LocalCapabilityRegistry)
        names = {row["name"] for row in agent.capabilities.catalog()}
        self.assertIn("timer", names)
        self.assertIn("time", names)

    def test_a_network_tool_is_marked_as_such(self):
        class Service:
            def current(self, city=""):
                return {"ok": True, "data": {"city": city}}

            def latest(self, topic="", limit=3):
                return {"ok": True, "data": {"items": []}}

        self.assertEqual(weather_tool(Service()).requires, REQUIRES_NETWORK)
        self.assertEqual(news_tool(Service()).requires, REQUIRES_NETWORK)


class ScopeTests(unittest.TestCase):
    def test_a_path_argument_becomes_a_path_scope(self):
        self.assertEqual(request_scope({"path": "D:/work/a.txt"}), "path:D:/work/a.txt")
        self.assertEqual(request_scope({"cwd": "D:/work"}), "path:D:/work")

    def test_an_empty_path_argument_is_not_a_scope(self):
        self.assertEqual(request_scope({"path": "   "}), "")
        self.assertEqual(request_scope({"path": 7}), "")

    def test_a_command_argument_becomes_a_command_scope(self):
        self.assertEqual(
            request_scope({"command": "python -m unittest"}), "command:python -m unittest"
        )

    def test_no_arguments_means_no_scope(self):
        self.assertEqual(request_scope(None), "")
        self.assertEqual(request_scope({"other": "x"}), "")


class GrantStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "permissions.json"

    def test_permanent_grants_survive_a_reload(self):
        store = GrantStore(self.path)
        store.set("write_file", "always", scope="path:D:/work")
        again = GrantStore(self.path)
        grant = again.match("write_file", "path:D:/work/a.txt")
        self.assertIsNotNone(grant)
        self.assertEqual(grant.mode, "always")

    def test_a_grant_covers_its_own_folder_only(self):
        store = GrantStore(self.path)
        store.set("write_file", "always", scope="path:D:/work")
        self.assertIsNotNone(store.match("write_file", "path:D:/work/notes/a.txt"))
        self.assertIsNone(store.match("write_file", "path:D:/work-other/a.txt"))
        self.assertIsNone(store.match("write_file", "path:D:/elsewhere/a.txt"))

    def test_a_grant_without_a_scope_covers_the_tool(self):
        store = GrantStore(self.path)
        store.set("todo_add", "always")
        self.assertIsNotNone(store.match("todo_add", ""))
        self.assertIsNone(store.match("todo_done", ""))

    def test_a_command_grant_matches_the_same_command_only(self):
        store = GrantStore(self.path)
        store.set("run_tests", "always", scope="command:python")
        self.assertIsNotNone(store.match("run_tests", "command:python -m unittest"))
        self.assertIsNone(store.match("run_tests", "command:powershell -c rm"))

    def test_revoking_one_scope_leaves_the_other(self):
        store = GrantStore(self.path)
        store.set("write_file", "always", scope="path:D:/work")
        store.set("write_file", "always", scope="path:D:/notes")
        self.assertEqual(store.revoke("write_file", "path:D:/work"), 1)
        self.assertIsNone(store.match("write_file", "path:D:/work/a.txt"))
        self.assertIsNotNone(store.match("write_file", "path:D:/notes/a.txt"))

    def test_revoking_the_tool_clears_every_scope(self):
        store = GrantStore(self.path)
        store.set("write_file", "always", scope="path:D:/work")
        store.set("write_file", "deny", scope="path:D:/system")
        self.assertEqual(store.revoke("write_file"), 2)
        self.assertEqual(store.all(), [])

    def test_one_shot_grants_are_not_written_to_disk(self):
        store = GrantStore(self.path)
        store.set("write_file", "once", scope="path:D:/work")
        self.assertFalse(self.path.exists())
        self.assertEqual(GrantStore(self.path).all(), [])

    def test_an_unknown_mode_is_refused(self):
        self.assertIsNone(GrantStore(self.path).set("write_file", "sometimes"))

    def test_a_broken_file_does_not_take_the_app_down(self):
        self.path.write_text("{ not json", encoding="utf-8")
        self.assertEqual(GrantStore(self.path).all(), [])


class PermissionCenterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.grants = GrantStore(Path(self.tmp.name) / "permissions.json")
        self.audit = SilentAudit()
        self.asked = []

    def manager(self, confirm=None):
        return PermissionManager(
            grants=self.grants, audit=self.audit, confirm_callback=confirm
        )

    def approve(self, name, level, reason):
        self.asked.append((name, level))
        return True

    def test_reading_is_still_automatic(self):
        self.assertTrue(self.manager().check("local_time", PermissionLevel.READ).allowed)

    def test_a_path_escape_is_refused_even_with_a_grant(self):
        self.grants.set("write_file", "always", scope="path:D:/work")
        decision = self.manager().check(
            "write_file", PermissionLevel.HIGH, arguments={"path": "D:/work/../../Windows/a.txt"}
        )
        self.assertFalse(decision.allowed)
        self.assertIn("越权", decision.reason)

    def test_a_permanent_grant_covers_its_scope_without_asking(self):
        self.grants.set("write_file", "always", scope="path:D:/work")
        decision = self.manager(confirm=self.approve).check(
            "write_file", PermissionLevel.HIGH, arguments={"path": "D:/work/notes/a.txt"}
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(self.asked, [])
        self.assertIn("已授权", decision.reason)

    def test_outside_the_granted_scope_it_asks_again(self):
        self.grants.set("write_file", "always", scope="path:D:/work")
        decision = self.manager(confirm=self.approve).check(
            "write_file", PermissionLevel.HIGH, arguments={"path": "D:/elsewhere/a.txt"}
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(len(self.asked), 1)

    def test_a_granted_command_does_not_cover_another_command(self):
        self.grants.set("run_tests", "always", scope="command:python")
        decision = self.manager(confirm=self.approve).check(
            "run_tests", PermissionLevel.HIGH, arguments={"command": "powershell -c rmdir"}
        )
        self.assertEqual(len(self.asked), 1)
        self.assertTrue(decision.allowed)

    def test_revoking_takes_effect_at_once(self):
        self.grants.set("write_file", "always", scope="path:D:/work")
        manager = self.manager(confirm=self.approve)
        self.assertTrue(
            manager.check(
                "write_file", PermissionLevel.HIGH, arguments={"path": "D:/work/a.txt"}
            ).allowed
        )
        self.grants.revoke("write_file")
        manager.check("write_file", PermissionLevel.HIGH, arguments={"path": "D:/work/a.txt"})
        self.assertEqual(len(self.asked), 1)

    def test_a_denied_tool_stays_denied_without_asking(self):
        self.grants.set("todo_add", "deny")
        decision = self.manager(confirm=self.approve).check("todo_add", PermissionLevel.LOW)
        self.assertFalse(decision.allowed)
        self.assertEqual(self.asked, [])

    def test_a_one_shot_grant_is_used_up(self):
        self.grants.set("write_file", "once", scope="path:D:/work")
        manager = self.manager(confirm=self.approve)
        first = manager.check(
            "write_file", PermissionLevel.HIGH, arguments={"path": "D:/work/a.txt"}
        )
        self.assertTrue(first.allowed)
        second = manager.check(
            "write_file", PermissionLevel.HIGH, arguments={"path": "D:/work/b.txt"}
        )
        self.assertTrue(second.allowed)
        self.assertEqual(len(self.asked), 1)

    def test_each_time_makes_a_low_risk_tool_ask(self):
        self.grants.set("todo_add", "each")
        decision = self.manager(confirm=self.approve).check("todo_add", PermissionLevel.LOW)
        self.assertTrue(decision.allowed)
        self.assertEqual(len(self.asked), 1)

    def test_each_time_without_a_confirm_channel_is_refused(self):
        self.grants.set("todo_add", "each")
        decision = self.manager().check("todo_add", PermissionLevel.LOW)
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.needs_confirmation)

    def test_a_critical_tool_is_not_unlocked_by_a_grant(self):
        self.grants.set("run_command", "always")
        decision = self.manager(confirm=self.approve).check(
            "run_command", PermissionLevel.CRITICAL
        )
        self.assertFalse(decision.allowed)
        self.assertIn("禁止", decision.reason)

    def test_every_decision_is_written_to_the_audit(self):
        self.manager().check("local_time", PermissionLevel.READ)
        self.manager().check("write_file", PermissionLevel.HIGH, arguments={"path": "D:/a.txt"})
        kinds = [row["kind"] for row in self.audit.rows]
        self.assertEqual(kinds, ["permission", "permission"])
        self.assertEqual(self.audit.rows[0]["decision"], "allow")
        self.assertEqual(self.audit.rows[1]["decision"], "deny")

    def test_high_risk_without_confirmation_is_refused(self):
        decision = self.manager().check("write_file", PermissionLevel.HIGH)
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.needs_confirmation)


class ExecutorPermissionTests(unittest.TestCase):
    """权限要在真正执行的那一层再查一遍，不能只在界面上查。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name) / "work"
        self.work.mkdir()
        self.outside = Path(self.tmp.name) / "outside.txt"
        self.grants = GrantStore(Path(self.tmp.name) / "permissions.json")
        self.db_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.db_dir.cleanup)
        self.db = Database(Path(self.db_dir.name) / "deskpet.sqlite3")
        self.db.migrate()

    def registry(self):
        registry = ToolRegistry()
        register_local_tools(registry, database=self.db)

        def write_file(path, content=""):
            target = Path(path)
            target.write_text(content, encoding="utf-8")
            return str(target)

        from tools.registry import Tool

        registry.register(
            Tool(
                name="write_file",
                description="写文件",
                schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
                permission="HIGH",
                handler=write_file,
                readonly=False,
            )
        )
        return registry

    def test_a_granted_path_goes_through(self):
        target = self.work / "notes.txt"
        self.grants.set("write_file", "always", scope="path:" + str(self.work))
        ex = executor(self.registry(), grants=self.grants, confirm=lambda *a: True)
        out = ex.execute("write_file", {"path": str(target), "content": "hi"})
        self.assertTrue(out.ok, out.error)
        self.assertEqual(target.read_text(encoding="utf-8"), "hi")

    def test_an_ungranted_path_needs_a_yes(self):
        asked = []
        ex = executor(
            self.registry(),
            grants=self.grants,
            confirm=lambda name, level, reason: asked.append(name) or False,
        )
        out = ex.execute("write_file", {"path": str(self.outside), "content": "hi"})
        self.assertFalse(out.ok)
        self.assertFalse(self.outside.exists())
        self.assertEqual(asked, ["write_file"])

    def test_a_path_escape_never_reaches_the_handler(self):
        ex = executor(self.registry(), grants=self.grants, confirm=lambda *a: True)
        out = ex.execute("write_file", {"path": str(self.work / ".." / "outside.txt")})
        self.assertFalse(out.ok)
        self.assertIn("越权", out.error)

    def test_the_local_tools_stay_available_without_a_model(self):
        ex = executor(self.registry(), grants=self.grants)
        self.assertTrue(ex.execute("local_time", {}).ok)
        self.assertTrue(ex.execute("todo_list", {}).ok)


if __name__ == "__main__":
    unittest.main()
