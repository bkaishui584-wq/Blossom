"""P3：工作记录默认关闭、真实采样、报表、删除与本地路由。"""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.capabilities import CapabilityRouter
from database.db import Database
from services.worklog import WorkLogService


class Prefs:
    def __init__(self):
        self.values = {}

    def get_preference(self, key):
        return self.values.get(key)

    def set_preference(self, key, value):
        self.values[key] = value


class Provider:
    name = "fake"

    def __init__(self):
        self.current_value = {"app": "Code.exe", "title": "main.py"}

    def is_available(self):
        return True

    def current(self):
        return dict(self.current_value)


def make_service(tmp, clock):
    db = Database(Path(tmp) / "t.sqlite3")
    db.migrate()
    prefs = Prefs()
    provider = Provider()
    service = WorkLogService(
        db, preferences=prefs, provider=provider, clock=clock, idle_limit=300
    )
    return db, prefs, provider, service


class WorkLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1761000000.0
        self.clock = lambda: self.now
        self.db, self.prefs, self.provider, self.service = make_service(
            self.tmp.name, self.clock
        )

    def enable(self):
        self.assertTrue(self.service.set_enabled(True, consent=True))

    def test_defaults_to_off_and_requires_explicit_consent(self):
        self.assertFalse(self.service.active())
        self.assertFalse(self.service.set_enabled(True))
        self.assertIsNone(self.service.observe(sample={"app": "Code.exe", "title": "x"}))
        self.assertEqual(self.db.query("SELECT * FROM work_intervals"), [])

    def test_same_app_coalesces_and_switching_opens_a_new_interval(self):
        self.enable()
        first = self.service.observe(sample={"app": "Code.exe", "title": "main.py"})
        self.now += 60
        second = self.service.observe(sample={"app": "Code.exe", "title": "main.py"})
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["seconds"], 60)

        self.provider.current_value = {"app": "msedge.exe", "title": "文档"}
        third = self.service.observe(sample={"app": "msedge.exe", "title": "文档"})
        self.assertNotEqual(third["id"], second["id"])
        self.assertEqual(len(self.service.records()), 2)

    def test_pause_stops_new_rows_and_resume_restarts(self):
        self.enable()
        self.service.observe(sample={"app": "Code.exe", "title": "main.py"})
        self.assertTrue(self.service.pause())
        self.now += 120
        self.assertIsNone(self.service.observe(sample={"app": "Code.exe", "title": "main.py"}))
        self.assertEqual(len(self.service.records()), 1)
        self.assertTrue(self.service.resume())
        self.assertIsNotNone(self.service.observe(sample={"app": "Code.exe", "title": "main.py"}))
        self.assertEqual(len(self.service.records()), 2)

    def test_idle_and_private_apps_do_not_keep_recording(self):
        self.enable()
        self.assertIsNone(
            self.service.observe(
                sample={"app": "Code.exe", "title": "main.py"}, idle_seconds=600
            )
        )
        self.service.observe(sample={"app": "WeChat.exe", "title": "私人聊天"})
        row = self.service.records()[0]
        self.assertEqual(row["app"], "WeChat.exe")
        self.assertEqual(row["title"], "")

    def test_excluded_app_is_not_recorded(self):
        self.enable()
        self.assertTrue(self.service.add_exclusion("game.exe"))
        self.assertIsNone(self.service.observe(sample={"app": "game.exe", "title": "游戏"}))
        self.assertEqual(self.service.records(), [])
        self.assertTrue(self.service.remove_exclusion("game.exe"))

    def test_reports_stats_export_and_delete(self):
        self.enable()
        self.service.set_project("DeskPet")
        self.service.observe(sample={"app": "Code.exe", "title": "main.py"})
        self.now += 120
        self.service.observe(sample={"app": "Code.exe", "title": "main.py"})
        day = __import__("datetime").datetime.fromtimestamp(self.now).strftime("%Y-%m-%d")
        stats = self.service.stats(day, day)
        self.assertEqual(stats["rows"][0]["project"], "DeskPet")
        self.assertGreaterEqual(stats["seconds"], 120)
        self.assertIn("DeskPet", self.service.report(day)["text"])

        target = Path(self.tmp.name) / "work.csv"
        self.assertTrue(self.service.export_csv(target, day, day))
        self.assertIn("DeskPet", target.read_text(encoding="utf-8-sig"))

        self.assertGreaterEqual(self.service.delete_day(day), 1)
        self.assertEqual(self.service.records(day), [])

    def test_retention_prunes_old_rows(self):
        self.enable()
        self.service.observe(sample={"app": "Code.exe", "title": "main.py"})
        self.now += 40 * 86400
        self.service.set_retention_days(30)
        self.assertEqual(self.db.query("SELECT * FROM work_intervals"), [])

    def test_work_report_is_a_local_capability(self):
        self.enable()
        self.service.observe(sample={"app": "Code.exe", "title": "main.py"})
        self.now += 60
        self.service.observe(sample={"app": "Code.exe", "title": "main.py"})
        router = CapabilityRouter(memory=self.prefs, worklog=self.service)
        answer = router.resolve("今天工作多久")
        self.assertIsNotNone(answer)
        self.assertEqual(answer.capability, "work")
        self.assertIn("1 分钟", answer.text)

    def test_migration_and_work_navigation_are_wired(self):
        tables = {
            row["name"]
            for row in self.db.query("SELECT name FROM sqlite_master WHERE type='table'")
        }
        self.assertIn("work_intervals", tables)
        self.assertIn("work_audit", tables)
        source = (ROOT / "ui" / "chat_view.py").read_text(encoding="utf-8")
        self.assertIn('("work", "工作记录")', source)
        wiring = (ROOT / "ui" / "chat_wiring.py").read_text(encoding="utf-8")
        self.assertIn('"work": self.open_work', wiring)


if __name__ == "__main__":
    unittest.main()
