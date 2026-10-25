"""P7 数据、隐私与成长：记忆备份/导入/删除、隐私与权限中心、真实时间线、习惯模型。"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT))

from database.db import MIGRATIONS, Database
from memory.backup import (
    EXPORT_KINDS,
    NEEDS_CONFIRM,
    backup_database,
    clear_all,
    clear_kind,
    delete_memory,
    export_payload,
    export_to_file,
    import_from_file,
    import_payload,
    restore_database,
)
from memory.habit import MIN_SAMPLES, HabitModel
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from memory.timeline import MILESTONES, Timeline
from security.config import Config
from security.privacy import CONTROLS, PERMISSION_ROWS, PrivacyCenter

NEW_FILES = (
    "memory/backup.py",
    "memory/timeline.py",
    "memory/habit.py",
    "security/privacy.py",
)


class FakeMic:
    def __init__(self, on=False):
        self.on = on

    def listening(self):
        return self.on


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.memory, self.db = self._make_memory(self.root)

    @staticmethod
    def _make_memory(folder, name="deskpet.sqlite3"):
        Path(folder).mkdir(parents=True, exist_ok=True)
        db = Database(Path(folder) / name)
        db.migrate()
        return MemoryManager(MemoryRepository(db)), db

    def count_memories(self):
        row = self.db.query_one("SELECT COUNT(*) AS n FROM memories")
        return int(row["n"]) if row else 0


class ExportTests(TempCase):
    def test_payload_only_has_user_visible_parts(self):
        payload = export_payload(self.memory)
        self.assertEqual(
            set(payload),
            {"version", "exported_at", "memories", "preferences", "relationship", "diary"},
        )
        blob = json.dumps(payload, ensure_ascii=False).lower()
        for hidden in ("audit", "token_usage", "schema_migrations", "sqlite_master"):
            self.assertNotIn(hidden, blob)

    def test_export_contains_real_memories(self):
        self.memory.remember("你喜欢用 Python", kind="long_term")
        payload = export_payload(self.memory)
        self.assertEqual(len(payload["memories"]), 1)
        self.assertEqual(payload["memories"][0]["content"], "你喜欢用 Python")
        self.assertEqual(payload["memories"][0]["kind"], "long_term")

    def test_export_skips_kinds_outside_the_list(self):
        self.memory.remember("正规记忆", kind="long_term")
        # remember() 会把没登记的层收进长期记忆，所以这里直接写库，
        # 专门验导出这一层自己的允许清单管不管用。
        self.memory.repository.add_memory("别的层", "not_exported")
        payload = export_payload(self.memory)
        self.assertEqual([row["content"] for row in payload["memories"]], ["正规记忆"])

    def test_export_carries_preferences(self):
        self.memory.set_preference("quiet_hours", "23-7")
        payload = export_payload(self.memory)
        self.assertIn({"key": "quiet_hours", "value": "23-7"}, payload["preferences"])

    def test_export_to_file_writes_json(self):
        self.memory.remember("导出测试", kind="project")
        target = self.root / "out" / "memory.json"
        result = export_to_file(self.memory, target)
        self.assertTrue(result["ok"])
        self.assertTrue(target.is_file())
        reloaded = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(reloaded["memories"][0]["content"], "导出测试")
        self.assertEqual(result["memories"], 1)

    def test_export_to_bad_path_reports_failure(self):
        result = export_to_file(self.memory, self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])

    def test_export_survives_a_bare_object(self):
        payload = export_payload(SimpleNamespace())
        self.assertEqual(payload["memories"], [])
        self.assertEqual(payload["preferences"], [])
        self.assertEqual(payload["relationship"], {})
        self.assertEqual(payload["diary"], [])

    def test_export_kinds_constants_are_the_memory_layers(self):
        self.assertEqual(
            set(EXPORT_KINDS), {"long_term", "project", "relationship", "feedback"}
        )


class ImportTests(TempCase):
    def payload(self, **overrides):
        data = {
            "version": 1,
            "memories": [],
            "preferences": [],
            "relationship": {},
            "diary": [],
        }
        data.update(overrides)
        return data

    def test_import_adds_only_missing(self):
        self.memory.remember("你喜欢 Python", kind="long_term")
        before = self.count_memories()
        result = import_payload(
            self.memory,
            self.payload(
                memories=[
                    {"kind": "long_term", "content": "你喜欢 Python"},
                    {"kind": "long_term", "content": "你住在杭州"},
                ]
            ),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(self.count_memories(), before + 1)

    def test_import_never_deletes(self):
        self.memory.remember("原有记忆", kind="long_term")
        before = self.count_memories()
        import_payload(self.memory, self.payload(memories=[{"kind": "long_term", "content": "新记忆"}]))
        self.assertGreaterEqual(self.count_memories(), before)

    def test_import_skips_unknown_kind(self):
        result = import_payload(
            self.memory, self.payload(memories=[{"kind": "spaceship", "content": "x"}])
        )
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["skipped"], 1)

    def test_import_skips_blank_or_broken_rows(self):
        result = import_payload(
            self.memory,
            self.payload(memories=[{"kind": "long_term", "content": "  "}, "垃圾", 42]),
        )
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["skipped"], 3)

    def test_import_preferences_do_not_overwrite(self):
        self.memory.set_preference("quiet_hours", "23-7")
        result = import_payload(
            self.memory,
            self.payload(
                preferences=[
                    {"key": "quiet_hours", "value": "1-2"},
                    {"key": "new_key", "value": "v"},
                ]
            ),
        )
        self.assertEqual(self.memory.get_preference("quiet_hours"), "23-7")
        self.assertEqual(self.memory.get_preference("new_key"), "v")
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["skipped"], 1)

    def test_import_rejects_non_object(self):
        result = import_payload(self.memory, "not a dict")
        self.assertFalse(result["ok"])

    def test_import_from_file_missing(self):
        result = import_from_file(self.memory, self.root / "nope.json")
        self.assertFalse(result["ok"])
        self.assertEqual(result["added"], 0)

    def test_roundtrip_into_a_fresh_database(self):
        self.memory.remember("记住的第一件事", kind="long_term")
        self.memory.remember("记住的第二件事", kind="project")
        self.memory.set_preference("persona.name", "小桌")
        target = self.root / "export.json"
        export_to_file(self.memory, target)

        other, other_db = self._make_memory(self.root / "other")
        result = import_from_file(other, target)
        self.assertTrue(result["ok"])
        self.assertEqual(result["added"], 3)
        kinds = [row["kind"] for row in other.repository.candidate_memories(kinds=EXPORT_KINDS)]
        self.assertEqual(sorted(kinds), ["long_term", "project"])


class BackupTests(TempCase):
    def test_backup_creates_a_file(self):
        self.memory.remember("备份前", kind="long_term")
        result = backup_database(self.db.path)
        self.assertTrue(result["ok"])
        self.assertTrue(Path(result["path"]).is_file())
        self.assertEqual(Path(result["path"]).parent, Path(self.db.path).parent / "backups")

    def test_backup_without_a_database_reports_failure(self):
        result = backup_database(self.root / "nope.sqlite3")
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])

    def test_backup_can_be_read_back(self):
        self.memory.remember("一", kind="long_term")
        self.memory.remember("二", kind="long_term")
        path = backup_database(self.db.path)["path"]
        copy = Database(path)
        row = copy.query_one("SELECT COUNT(*) AS n FROM memories")
        self.assertEqual(int(row["n"]), 2)

    def test_restore_keeps_a_safety_copy(self):
        self.memory.remember("只有这一条", kind="long_term")
        path = backup_database(self.db.path)["path"]
        self.memory.remember("备份之后加的", kind="long_term")
        self.assertEqual(self.count_memories(), 2)

        result = restore_database(self.db.path, path)
        self.assertTrue(result["ok"])
        self.assertTrue(Path(result["safety"]).is_file())
        self.assertEqual(self.count_memories(), 1)

    def test_restore_missing_backup_reports_failure(self):
        result = restore_database(self.db.path, self.root / "nope.sqlite3")
        self.assertFalse(result["ok"])
        self.assertEqual(result["safety"], "")


class DeletionTests(TempCase):
    def test_delete_needs_confirmation(self):
        number = self.memory.remember("会被误删吗", kind="long_term")
        result = delete_memory(self.memory, number)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], NEEDS_CONFIRM)
        self.assertEqual(self.count_memories(), 1)

    def test_delete_removes_one_with_confirmation(self):
        number = self.memory.remember("删掉我", kind="long_term")
        result = delete_memory(self.memory, number, confirm=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["removed"], 1)
        self.assertEqual(self.count_memories(), 0)

    def test_delete_bad_id_is_reported(self):
        result = delete_memory(self.memory, "abc", confirm=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["removed"], 0)

    def test_delete_unknown_id_removes_nothing(self):
        result = delete_memory(self.memory, 9999, confirm=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["removed"], 0)

    def test_clear_kind_needs_confirmation(self):
        self.memory.remember("项目记忆", kind="project")
        result = clear_kind(self.memory, "project")
        self.assertFalse(result["ok"])
        self.assertEqual(self.count_memories(), 1)

    def test_clear_kind_removes_only_that_kind(self):
        self.memory.remember("项目记忆", kind="project")
        self.memory.remember("长期记忆", kind="long_term")
        result = clear_kind(self.memory, "project", confirm=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["removed"], 1)
        left = self.db.query_one("SELECT kind FROM memories")
        self.assertEqual(left["kind"], "long_term")

    def test_clear_all_needs_confirmation(self):
        self.memory.remember("一", kind="long_term")
        result = clear_all(self.memory)
        self.assertFalse(result["ok"])
        self.assertEqual(self.count_memories(), 1)

    def test_clear_all_removes_everything_with_confirmation(self):
        self.memory.remember("一", kind="long_term")
        self.memory.remember("二", kind="project")
        result = clear_all(self.memory, confirm=True)
        self.assertTrue(result["ok"])
        self.assertEqual(self.count_memories(), 0)

    def test_clear_kind_without_a_name_is_rejected(self):
        result = clear_kind(self.memory, "  ", confirm=True)
        self.assertFalse(result["ok"])


class PrivacyTests(TempCase):
    def test_defaults_match_current_behaviour(self):
        center = PrivacyCenter(preferences=self.memory)
        self.assertFalse(center.allows("listen"))
        self.assertFalse(center.allows("see"))
        self.assertTrue(center.allows("read_screen"))
        self.assertTrue(center.allows("proactive"))
        self.assertTrue(center.allows("run_tasks"))
        self.assertFalse(center.allows("files"))
        self.assertFalse(center.allows("hardware"))

    def test_set_persists_in_preferences(self):
        center = PrivacyCenter(preferences=self.memory)
        self.assertTrue(center.set("listen", True))
        self.assertTrue(center.allows("listen"))
        self.assertEqual(self.memory.get_preference("privacy.listen"), "1")

    def test_proactive_reuses_the_existing_switch(self):
        center = PrivacyCenter(preferences=self.memory)
        center.set("proactive", False)
        self.assertEqual(self.memory.get_preference("proactive_enabled"), "0")
        self.assertFalse(center.allows("proactive"))

    def test_describe_has_the_expected_keys(self):
        view = PrivacyCenter(preferences=self.memory).describe()
        self.assertEqual(
            set(view),
            {
                "麦克风",
                "系统音频",
                "摄像头",
                "屏幕感知",
                "工作上下文",
                "屏幕内容给模型",
                "字幕云端翻译",
                "网络模型",
                "本地记忆",
            },
        )
        self.assertEqual(view["网络模型"], "未连接")
        self.assertEqual(view["本地记忆"], "启用")
        self.assertEqual(view["系统音频"], "关闭")
        self.assertEqual(view["字幕云端翻译"], "不允许")
        # 看你在用什么软件这一项默认是关的，用户自己开之前不许偷偷读。
        self.assertEqual(view["工作上下文"], "关闭")

    def test_permission_center_covers_every_category(self):
        rows = PrivacyCenter(preferences=self.memory).permission_center()
        self.assertEqual(len(rows), len(PERMISSION_ROWS))
        self.assertEqual([label for label, _ in PERMISSION_ROWS], list(rows))
        self.assertIn("截图分析", rows)
        self.assertFalse(rows["截图分析"])

    def test_mic_shows_on_only_when_control_and_mic_agree(self):
        center = PrivacyCenter(preferences=self.memory, mic=FakeMic(on=True))
        self.assertEqual(center.describe()["麦克风"], "关闭")
        center.set("listen", True)
        self.assertEqual(center.describe()["麦克风"], "开启")

    def test_network_model_follows_config(self):
        config = Config(load_env=False)
        config.llm_model = "some-model"
        center = PrivacyCenter(preferences=self.memory, config=config)
        self.assertEqual(center.describe()["网络模型"], "已连接")
        self.assertTrue(center.permission_center()["网络模型"])

    def test_unknown_control_is_ignored(self):
        center = PrivacyCenter(preferences=self.memory)
        self.assertFalse(center.allows("nope"))
        self.assertFalse(center.set("nope", True))

    def test_without_preferences_is_safe(self):
        center = PrivacyCenter()
        self.assertFalse(center.set("listen", True))
        self.assertFalse(center.allows("listen"))
        self.assertTrue(center.allows("read_screen"))
        self.assertEqual(center.describe()["本地记忆"], "不可用")

    def test_controls_list_covers_user_choices(self):
        keys = [row["key"] for row in PrivacyCenter(preferences=self.memory).controls()]
        self.assertEqual(len(keys), len(CONTROLS))
        self.assertIn("listen", keys)
        self.assertIn("hardware", keys)


class TimelineTests(TempCase):
    def test_mark_records_only_once(self):
        line = self.memory.timeline
        self.assertTrue(line.mark("first_start"))
        self.assertFalse(line.mark("first_start"))
        self.assertTrue(line.has("first_start"))

    def test_unknown_milestone_is_ignored(self):
        self.assertFalse(self.memory.timeline.mark("nope"))

    def test_sync_reads_first_message(self):
        self.memory.record("s1", "user", "你好")
        self.memory.timeline.sync(self.memory)
        self.assertTrue(self.memory.timeline.has("first_chat"))

    def test_sync_reads_first_feedback(self):
        self.memory.remember("以后别这么冷淡", kind="feedback")
        self.memory.timeline.sync(self.memory)
        self.assertTrue(self.memory.timeline.has("first_feedback"))

    def test_describe_lists_the_milestones(self):
        self.memory.timeline.mark("first_start")
        self.memory.timeline.mark("first_voice")
        view = self.memory.timeline.describe()
        self.assertEqual(view["count"], 2)
        self.assertIn("first_start", view["milestones"])

    def test_no_growth_without_real_data(self):
        self.memory.timeline.sync(self.memory)
        self.assertFalse(self.memory.timeline.has("first_chat"))
        self.assertEqual(self.memory.timeline.describe()["count"], 0)

    def test_without_a_database_is_safe(self):
        line = Timeline()
        self.assertFalse(line.mark("first_start"))
        self.assertEqual(line.entries(), [])
        self.assertEqual(line.describe()["count"], 0)

    def test_milestones_include_the_firsts_from_the_plan(self):
        for key in ("first_chat", "first_task", "first_voice", "first_live2d", "first_device"):
            self.assertIn(key, MILESTONES)


class HabitTests(TempCase):
    STAMP = "2026-01-01 13:00:00"

    def seed(self, count, stamp=None):
        stamp = stamp or self.STAMP
        for index in range(count):
            self.db.insert(
                "INSERT INTO messages (session_id, role, content, created_at)"
                " VALUES (?, ?, ?, ?)",
                ("s1", "user", "第%d句" % index, stamp),
            )

    def local_hour(self, stamp=None):
        stamp = stamp or self.STAMP
        row = self.db.query_one(
            "SELECT CAST(strftime('%H', ?, 'localtime') AS INTEGER) AS h", (stamp,)
        )
        return int(row["h"])

    def model(self):
        return HabitModel(
            database=self.db, preferences=self.memory, min_samples=MIN_SAMPLES
        )

    def test_needs_enough_samples(self):
        self.seed(3)
        result = self.model().refresh()
        self.assertEqual(result["samples"], 3)
        self.assertEqual(result["active_hours"], [])
        self.assertEqual(result["confidence"], 0.0)

    def test_infers_active_hours_from_real_timestamps(self):
        self.seed(MIN_SAMPLES)
        result = self.model().refresh()
        self.assertEqual(result["samples"], MIN_SAMPLES)
        self.assertEqual(result["active_hours"], [self.local_hour()])
        self.assertGreater(result["confidence"], 0.0)

    def test_refresh_is_stored_as_inferred(self):
        self.seed(MIN_SAMPLES)
        self.model().refresh()
        raw = self.memory.get_preference("habit.inferred")
        self.assertTrue(raw)
        self.assertIn("active_hours", json.loads(raw))

    def test_note_mentions_the_window(self):
        self.seed(MIN_SAMPLES)
        habit = self.model()
        habit.refresh()
        self.assertIn("推测", habit.note())

    def test_note_is_empty_without_samples(self):
        self.assertEqual(self.model().note(), "")

    def test_proactiveness_is_normal_without_feedback(self):
        view = self.model().proactiveness()
        self.assertEqual(view["level"], "normal")
        self.assertFalse(view["applied"])

    def test_negative_feedback_lowers_proactiveness(self):
        self.memory.remember("以后别烦我", kind="feedback")
        self.memory.remember("太吵了，安静点", kind="feedback")
        self.assertEqual(self.model().proactiveness()["level"], "low")

    def test_positive_feedback_raises_proactiveness(self):
        for text in ("多说点，我喜欢", "继续，很好", "可以多聊聊"):
            self.memory.remember(text, kind="feedback")
        self.assertEqual(self.model().proactiveness()["level"], "high")

    def test_without_a_database_is_safe(self):
        habit = HabitModel()
        self.assertEqual(habit.observe(), [])
        self.assertEqual(habit.refresh()["active_hours"], [])
        self.assertEqual(habit.note(), "")

    def test_describe_is_a_read_only_view(self):
        self.seed(MIN_SAMPLES)
        habit = self.model()
        habit.refresh()
        view = habit.describe()
        self.assertEqual(view["samples"], MIN_SAMPLES)
        self.assertIn("proactiveness", view)


class MigrationTests(unittest.TestCase):
    def test_timeline_migration_exists(self):
        versions = [version for version, _, _ in MIGRATIONS]
        self.assertIn(8, versions)

    def test_migration_preserves_existing_data(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "deskpet.sqlite3")
            applied = db.migrate()
            self.assertIn(8, applied)
            db.insert("INSERT INTO memories (kind, content) VALUES ('long_term', '老记忆')")
            again = db.migrate()
            self.assertEqual(again, [])
            row = db.query_one("SELECT content FROM memories")
            self.assertEqual(row["content"], "老记忆")


class BoundaryTests(unittest.TestCase):
    def test_new_modules_have_no_shell_or_eval(self):
        for name in NEW_FILES:
            text = (ROOT / name).read_text(encoding="utf-8")
            for needle in ("os.system", "subprocess", "eval(", "exec(", "pickle.load"):
                self.assertNotIn(needle, text, "%s 出现在 %s" % (needle, name))

    def test_backup_module_needs_only_the_standard_library(self):
        text = (ROOT / "memory" / "backup.py").read_text(encoding="utf-8")
        imported = {
            line.strip()
            for line in text.splitlines()
            if line.startswith("import ") or line.startswith("from ")
        }
        self.assertEqual(
            imported,
            {
                "import json",
                "import shutil",
                "import sqlite3",
                "import time",
                "from pathlib import Path",
            },
        )

    def test_privacy_module_has_no_imports(self):
        text = (ROOT / "security" / "privacy.py").read_text(encoding="utf-8")
        imported = {
            line.strip()
            for line in text.splitlines()
            if line.startswith("import ") or line.startswith("from ")
        }
        self.assertEqual(imported, set())

    def test_agent_core_does_not_know_about_these_modules(self):
        text = (ROOT / "agent" / "core.py").read_text(encoding="utf-8").lower()
        for word in ("privacy", "timeline", "habit", "backup"):
            self.assertNotIn(word, text)

    def test_deletion_is_never_automatic(self):
        # 不主动确认时，删除类操作一律拒绝。
        self.assertFalse(NEEDS_CONFIRM == "")
        for confirm_default in (False,):
            result = clear_all(SimpleNamespace(), confirm=confirm_default)
            self.assertFalse(result["ok"])

    def test_import_leaves_the_database_alone_for_bad_input(self):
        memory, _ = TempCase._make_memory(tempfile.mkdtemp())
        before = memory.repository.db.query_one("SELECT COUNT(*) AS n FROM memories")["n"]
        import_payload(memory, {"memories": "nope", "preferences": 5})
        after = memory.repository.db.query_one("SELECT COUNT(*) AS n FROM memories")["n"]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
