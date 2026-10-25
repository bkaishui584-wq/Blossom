"""阶段 P63 验收：Local First 第二阶段。天气缓存、授权目录、应用启动、审计过滤。

要点：
    取不到新天气时给旧记录并说清放了多久，绝不编一个实时值；
    文件工具默认只在工作区，用户持久授权目录后才能越出去；
    打开文件和启动应用都过权限，可执行文件不代开；
    审计能按类别、对象、结论过滤。
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database.db import Database  # noqa: E402
from security.audit import AuditLog  # noqa: E402
from services.cache import TTLCache  # noqa: E402
from services.weather import WeatherService  # noqa: E402
from tools.apps import (  # noqa: E402
    launch_app_handler,
    open_path_handler,
    open_url_handler,
    parse_apps,
    register_app_tools,
)
from tools.builtin import weather_line  # noqa: E402
from tools.permission import GrantStore, PermissionManager  # noqa: E402
from tools.registry import ToolExecutor, ToolRegistry  # noqa: E402
from tools.workspace import authorized_roots, register_workspace_tools  # noqa: E402


def make_service(clock, forecast_state):
    def fetch(url):
        if "geocoding" in url:
            return {"results": [{"name": "北京", "latitude": 39.9, "longitude": 116.4}]}
        forecast_state["calls"] += 1
        if forecast_state.get("fail"):
            raise RuntimeError("net down")
        return {
            "current": {"temperature_2m": 21, "weather_code": 0, "wind_speed_10m": 3}
        }

    cache = TTLCache(ttl_seconds=600, clock=lambda: clock["t"])
    return WeatherService(fetch=fetch, cache=cache)


class WeatherCacheTests(unittest.TestCase):
    def setUp(self):
        self.clock = {"t": 1000.0}
        self.state = {"calls": 0, "fail": False}
        self.service = make_service(self.clock, self.state)

    def test_fresh_fetch_is_marked_not_cached(self):
        result = self.service.current("北京")
        self.assertTrue(result["ok"])
        self.assertFalse(result["from_cache"])
        self.assertFalse(result["stale"])
        self.assertEqual(result["data"]["temperature"], 21)

    def test_repeat_within_ttl_comes_from_cache(self):
        self.service.current("北京")
        self.clock["t"] += 60
        again = self.service.current("北京")
        self.assertTrue(again["from_cache"])
        self.assertFalse(again["stale"])
        self.assertGreaterEqual(again["age_seconds"], 60)
        self.assertEqual(self.state["calls"], 1)

    def test_expired_cache_degrades_to_old_data_with_a_real_timestamp(self):
        self.service.current("北京")
        self.clock["t"] += 700
        self.state["fail"] = True
        stale = self.service.current("北京")
        self.assertTrue(stale["ok"])
        self.assertTrue(stale["stale"])
        self.assertTrue(stale["from_cache"])
        self.assertIn("分钟前的记录", stale["note"])
        self.assertEqual(stale["data"]["temperature"], 21)

    def test_no_cache_and_failure_reports_the_error(self):
        self.state["fail"] = True
        failed = self.service.current("上海")
        self.assertFalse(failed["ok"])
        self.assertIn("不可用", failed["error"])
        self.assertNotIn("temperature", failed)

    def test_geocode_failure_also_uses_stale_data(self):
        self.service.current("北京")
        self.clock["t"] += 700
        original = self.service.fetch

        def failing(url):
            if "geocoding" in url:
                raise RuntimeError("dns down")
            return original(url)

        self.service.fetch = failing
        stale = self.service.current("北京")
        self.assertTrue(stale["ok"])
        self.assertTrue(stale["stale"])

    def test_summary_says_how_old_the_record_is(self):
        data = {"city": "北京", "temperature": 21, "weather": "晴", "stale": True, "age_seconds": 720}
        line = weather_line(data)
        self.assertIn("12 分钟前", line)


class AuditFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "a.sqlite3")
        self.db.migrate()
        self.audit = AuditLog(self.db)
        self.audit.record(kind="permission", name="read_file", level="READ", decision="allow")
        self.audit.record(kind="permission", name="write_file", level="HIGH", decision="deny")
        self.audit.record(kind="tool_ok", name="read_file", level="READ", decision="tool_ok")

    def test_filter_by_kind(self):
        rows = self.audit.recent(kind="permission")
        self.assertEqual(len(rows), 2)

    def test_filter_by_name(self):
        rows = self.audit.recent(name="read_file")
        self.assertEqual(len(rows), 2)

    def test_filter_by_decision(self):
        rows = self.audit.recent(decision="deny")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "write_file")

    def test_kinds_lists_every_category_once(self):
        self.assertEqual(self.audit.kinds(), ["permission", "tool_ok"])


class AuthorizedDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: None)
        (self.ws / "hello.txt").write_text("inside", encoding="utf-8")
        self.outside = Path(tempfile.mkdtemp())
        (self.outside / "secret.txt").write_text("outside", encoding="utf-8")
        self.grants = GrantStore(None)

    def build(self):
        registry = ToolRegistry()
        register_workspace_tools(
            registry,
            None,
            workspace=str(self.ws),
            extra_roots=lambda: authorized_roots(self.grants.all()),
        )
        permissions = PermissionManager(
            grants=self.grants, confirm_callback=lambda *args: True
        )
        return ToolExecutor(registry, permissions)

    def test_workspace_reads_still_work(self):
        self.assertTrue(self.build().execute("read_file", {"path": "hello.txt"}).ok)

    def test_outside_path_is_refused_without_a_grant(self):
        outcome = self.build().execute("read_file", {"path": str(self.outside / "secret.txt")})
        self.assertFalse(outcome.ok)
        self.assertIn("已授权目录", outcome.error)

    def test_persistent_grant_opens_exactly_that_directory(self):
        self.grants.set("read_file", "always", scope="path:" + str(self.outside))
        executor = self.build()
        self.assertTrue(
            executor.execute("read_file", {"path": str(self.outside / "secret.txt")}).ok
        )
        other = Path(tempfile.mkdtemp())
        (other / "x.txt").write_text("x", encoding="utf-8")
        refused = executor.execute("read_file", {"path": str(other / "x.txt")})
        self.assertFalse(refused.ok)

    def test_once_grant_does_not_open_a_directory_for_good(self):
        self.grants.set("read_file", "once", scope="path:" + str(self.outside))
        outcome = self.build().execute("read_file", {"path": str(self.outside / "secret.txt")})
        self.assertFalse(outcome.ok)

    def test_denied_grant_keeps_the_directory_closed(self):
        self.grants.set("read_file", "deny", scope="path:" + str(self.outside))
        outcome = self.build().execute("read_file", {"path": str(self.outside / "secret.txt")})
        self.assertFalse(outcome.ok)

    def test_traversal_is_refused(self):
        outcome = self.build().execute("read_file", {"path": "../escape.txt"})
        self.assertFalse(outcome.ok)

    def test_copy_refuses_silent_overwrite(self):
        self.grants.set("copy_file", "always", scope="path:" + str(self.outside))
        self.grants.set("write_file", "always", scope="path:" + str(self.ws))
        self.grants.set("copy_file", "always", scope="path:" + str(self.ws))
        executor = self.build()
        args = {"source": str(self.outside / "secret.txt"), "target": "copy.txt"}
        self.assertTrue(executor.execute("copy_file", args).ok)
        again = executor.execute("copy_file", args)
        self.assertFalse(again.ok)
        self.assertTrue(executor.execute("copy_file", {**args, "overwrite": True}).ok)

    def test_no_permanent_delete_tool_exists(self):
        names = set(self.build().registry.names())
        for banned in ("delete_file", "remove_file", "rm", "unlink"):
            self.assertNotIn(banned, names)

    def test_authorized_roots_ignores_deny_and_once(self):
        self.grants.set("read_file", "deny", scope="path:" + str(self.outside))
        self.assertEqual(authorized_roots(self.grants.all()), [])
        self.grants.set("read_file", "always", scope="path:" + str(self.outside))
        self.assertEqual(authorized_roots(self.grants.all()), [str(self.outside)])


class RunCommandTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        (self.ws / "ok.py").write_text("print('hi')", encoding="utf-8")

    def build(self):
        registry = ToolRegistry()
        register_workspace_tools(registry, None, workspace=str(self.ws))
        return ToolExecutor(registry, PermissionManager(confirm_callback=lambda *a: True))

    def test_non_whitelisted_program_is_refused(self):
        outcome = self.build().execute("run_command", {"command": "git status"})
        self.assertFalse(outcome.ok)
        self.assertIn("白名单", outcome.error)

    def test_python_c_is_refused(self):
        outcome = self.build().execute("run_command", {"command": 'python -c "print(1)"'})
        self.assertFalse(outcome.ok)

    def test_allowed_module_runs_for_real(self):
        outcome = self.build().execute("run_command", {"command": "python -m unittest -h"})
        self.assertTrue(outcome.ok, outcome.error)
        self.assertIn("returncode", outcome.result)

    def test_cwd_outside_roots_is_refused(self):
        outside = tempfile.mkdtemp()
        outcome = self.build().execute(
            "run_command", {"command": "python -m unittest -h", "cwd": outside}
        )
        self.assertFalse(outcome.ok)


class AppToolTests(unittest.TestCase):
    def build_executor(self, apps=None, config=None):
        registry = ToolRegistry()
        register_app_tools(registry, config, apps=apps)
        return ToolExecutor(registry, PermissionManager(confirm_callback=lambda *a: True))

    def test_open_url_needs_a_real_http_target(self):
        executor = self.build_executor()
        bad = executor.execute("open_url", {"url": "file:///etc/passwd"})
        self.assertFalse(bad.ok)
        with mock.patch("tools.apps.os.startfile", create=True) as started:
            good = executor.execute("open_url", {"url": "https://example.com"})
        self.assertTrue(good.ok)
        started.assert_called_once()

    def test_open_url_is_medium_and_needs_confirmation(self):
        registry = ToolRegistry()
        register_app_tools(registry, None, apps={})
        tool = registry.get("open_url")
        self.assertEqual(tool.permission, "MEDIUM")
        locked = ToolExecutor(registry, PermissionManager())
        self.assertFalse(locked.execute("open_url", {"url": "https://example.com"}).ok)

    def test_open_path_refuses_executables(self):
        executor = self.build_executor()
        path = str(Path(tempfile.mkdtemp()) / "boom.exe")
        Path(path).write_text("x", encoding="utf-8")
        outcome = executor.execute("open_path", {"path": path})
        self.assertFalse(outcome.ok)
        self.assertIn("可执行文件", outcome.error)

    def test_open_path_opens_a_directory(self):
        target = tempfile.mkdtemp()
        with mock.patch("tools.apps.os.startfile", create=True) as started:
            outcome = self.build_executor().execute("open_path", {"path": target})
        self.assertTrue(outcome.ok)
        started.assert_called_once()

    def test_launch_app_without_configuration_says_so(self):
        outcome = self.build_executor(apps={}).execute("launch_app", {"name": "记事本"})
        self.assertFalse(outcome.ok)
        self.assertIn("还没有配置", outcome.error)

    def test_launch_app_only_runs_registered_names(self):
        executor = self.build_executor(apps=parse_apps("记事本=notepad;浏览器=msedge"))
        unknown = executor.execute("launch_app", {"name": "不明程序"})
        self.assertFalse(unknown.ok)
        self.assertIn("记事本", unknown.error)
        with mock.patch("tools.apps.subprocess.Popen") as popen:
            ok = executor.execute("launch_app", {"name": "记事本"})
        self.assertTrue(ok.ok)
        popen.assert_called_once()

    def test_parse_apps_skips_broken_entries(self):
        table = parse_apps("记事本=notepad;坏条目;=msedge;浏览器=msedge")
        self.assertEqual(set(table), {"记事本", "浏览器"})

    def test_open_handlers_exist_without_registry(self):
        self.assertTrue(callable(open_url_handler()))
        self.assertTrue(callable(open_path_handler()))
        self.assertTrue(callable(launch_app_handler({})))


if __name__ == "__main__":
    unittest.main()