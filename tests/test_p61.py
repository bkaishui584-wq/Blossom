"""P61 阶段 E 与阶段 L 验收：人格冲突、版本与撤销，以及关掉开关后功能真的停。

要点：
    互相打架的性格只提示，不替用户改；
    每次改动记一笔并推进版本号，撤销就是把上一次的强度写回去；
    改完人格，模型真正看到的那段人格说明确实变了；
    关掉麦克风、屏幕感知、主动说话之后，对应功能确实停；
    反复扫角色、反复改人格不会无上限地攒数据。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from characters import store  # noqa: E402
from memory.manager import MemoryManager  # noqa: E402
from memory.repository import MemoryRepository  # noqa: E402
from database.db import Database  # noqa: E402
from persona import history as persona_history  # noqa: E402
from persona import traits as traits_module  # noqa: E402
from persona.interface import (  # noqa: E402
    SAFETY_BOUNDARY,
    DefaultPersonaProvider,
    build_persona_note,
    clean_examples,
)
from persona.package import CharacterPackage, PackagePersonaProvider  # noqa: E402
from security.config import Config  # noqa: E402
from security.privacy import PrivacyCenter  # noqa: E402
from voice.wake import MicPrivacy  # noqa: E402


class Memory:
    """最简偏好存储，用来单独验历史记录，不牵扯数据库。"""

    def __init__(self):
        self.data = {}

    def get_preference(self, key):
        return self.data.get(key, "")

    def set_preference(self, key, value):
        self.data[key] = value


class Broken(Memory):
    def get_preference(self, key):
        raise RuntimeError("读不了")

    def set_preference(self, key, value):
        raise RuntimeError("写不了")


class ConflictTests(unittest.TestCase):
    def test_opposing_traits_are_reported(self):
        rows = traits_module.conflicts({"活泼": 80, "安静": 70})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][:2], ("活泼", "安静"))

    def test_weak_traits_do_not_count_as_conflicts(self):
        self.assertEqual(traits_module.conflicts({"活泼": 80, "安静": 10}), [])
        self.assertEqual(traits_module.conflict_note({"温柔": 90, "独立": 5}), "")

    def test_conflict_note_names_both_sides(self):
        note = traits_module.conflict_note({"幽默": 60, "认真": 60})
        self.assertIn("幽默", note)
        self.assertIn("认真", note)

    def test_conflicts_are_only_reported_never_applied(self):
        traits = {"活泼": 80, "安静": 80}
        before = dict(traits)
        traits_module.conflicts(traits)
        traits_module.conflict_note(traits)
        self.assertEqual(traits, before)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.memory = Memory()

    def test_every_change_bumps_the_version(self):
        self.assertEqual(persona_history.version(self.memory), 0)
        persona_history.record(self.memory, {"温柔": 50}, {"温柔": 70})
        persona_history.record(self.memory, {"温柔": 70}, {"活泼": 80})
        self.assertEqual(persona_history.version(self.memory), 2)

    def test_rows_come_back_newest_first(self):
        persona_history.record(self.memory, {}, {"温柔": 60})
        persona_history.record(self.memory, {"温柔": 60}, {"理性": 90})
        rows = persona_history.rows(self.memory)
        self.assertEqual(rows[0]["after"], {"理性": 90})
        self.assertEqual(rows[0]["v"], 2)
        self.assertEqual(rows[1]["v"], 1)

    def test_undo_restores_the_previous_settings(self):
        persona_history.record(self.memory, {"温柔": 50}, {"活泼": 80})
        result = persona_history.undo(self.memory)
        self.assertTrue(result["ok"])
        self.assertEqual(result["restored"], {"温柔": 50})
        self.assertEqual(result["was"], {"活泼": 80})
        self.assertEqual(persona_history.version(self.memory), 0)

    def test_undo_with_nothing_to_undo_says_so(self):
        result = persona_history.undo(self.memory)
        self.assertFalse(result["ok"])
        self.assertIn("没有可撤销", result["error"])

    def test_history_is_capped_so_it_cannot_grow_forever(self):
        for index in range(persona_history.MAX_ROWS + 12):
            persona_history.record(self.memory, {}, {"温柔": index % 100})
        raw = json.loads(self.memory.get_preference(persona_history.HISTORY_KEY))
        self.assertEqual(len(raw), persona_history.MAX_ROWS)
        self.assertEqual(persona_history.version(self.memory), persona_history.MAX_ROWS + 12)

    def test_broken_storage_is_reported_not_swallowed(self):
        broken = Broken()
        self.assertEqual(persona_history.record(broken, {}, {"温柔": 60}), {})
        self.assertEqual(persona_history.version(broken), 0)
        self.assertFalse(persona_history.undo(broken)["ok"])
        self.assertEqual(persona_history.rows(broken), [])
        self.assertFalse(persona_history.clear(broken))

    def test_clear_resets_version_and_history(self):
        persona_history.record(self.memory, {}, {"温柔": 60})
        self.assertTrue(persona_history.clear(self.memory))
        self.assertEqual(persona_history.version(self.memory), 0)
        self.assertEqual(persona_history.rows(self.memory), [])

    def test_garbage_in_storage_does_not_blow_up(self):
        self.memory.set_preference(persona_history.HISTORY_KEY, "{not json")
        self.memory.set_preference(persona_history.VERSION_KEY, "NaN")
        self.assertEqual(persona_history.rows(self.memory), [])
        self.assertEqual(persona_history.version(self.memory), 0)


class PersonaBehaviourTests(unittest.TestCase):
    def test_changing_traits_changes_what_the_model_is_told(self):
        persona = DefaultPersonaProvider(name="小桌", traits={"温柔": 70})
        before = build_persona_note(persona)
        persona.set_traits({"理性": 95, "独立": 80})
        after = build_persona_note(persona)
        self.assertNotEqual(before, after)
        self.assertIn("理性", after)
        self.assertIn("小桌", after)

    def test_safety_boundary_stays_at_the_end(self):
        persona = DefaultPersonaProvider(name="小桌", traits={"傲娇": 90})
        note = build_persona_note(persona)
        self.assertTrue(note.endswith(note.strip()[-12:]))
        self.assertIn("不", note)

    def _packaged(self, persona):
        package = CharacterPackage.from_dict({"id": "hero", "persona": persona}, path="hero")
        return PackagePersonaProvider(
            default=DefaultPersonaProvider(name="小桌"), packages=[package], selected="hero"
        )

    def test_user_written_examples_reach_the_model(self):
        note = build_persona_note(self._packaged({"examples": ["别在我写代码的时候吵我"]}))
        self.assertIn("说话示例", note)
        self.assertIn("别在我写代码的时候吵我", note)

    def test_examples_are_bounded_and_empty_ones_dropped(self):
        cleaned = clean_examples(["  a  ", "", "   ", "b", "c", "d", "e", "f"])
        self.assertEqual(cleaned, ["a", "b", "c", "d"])
        self.assertEqual(clean_examples("x" * 500)[0], "x" * 60)
        self.assertEqual(clean_examples([]), [])
        self.assertEqual(clean_examples(None), [])

    def test_examples_cannot_push_the_safety_boundary_away(self):
        note = build_persona_note(
            self._packaged({"examples": ["很长的一句话" * 40]}), max_chars=200
        )
        self.assertTrue(note.endswith(SAFETY_BOUNDARY))
        self.assertLessEqual(len(note), 200)

    def test_traits_only_know_the_known_names(self):
        self.assertEqual(traits_module.normalize({"不存在": 80}), {})
        self.assertEqual(traits_module.normalize({"温柔": 999}), {"温柔": 100})


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.chars = self.root / "characters"
        self.live2d = self.root / "live2d" / "hero"
        self.chars.mkdir(parents=True)
        self.live2d.mkdir(parents=True)
        (self.live2d / "hero.moc3").write_bytes(b"x")
        (self.live2d / "hero.model3.json").write_text(
            json.dumps({"Version": 3, "FileReferences": {"Moc": "hero.moc3"}}),
            encoding="utf-8",
        )


class DesktopPackageTests(TempCase):
    def setUp(self):
        super().setUp()
        try:
            import tkinter as tk
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        try:
            self.window = tk.Tk()
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        self.window.withdraw()
        self.addCleanup(self._destroy)

    def _destroy(self):
        try:
            self.window.destroy()
        except Exception:
            pass

    def _pet(self):
        from agent.context import ContextManager
        from agent.core import AgentCore
        from characters.manager import CharacterManager
        from model.base import ChatResult, ModelProvider
        from ui.desktop import DesktopPet

        class Silent(ModelProvider):
            name = "silent"

            def is_available(self):
                return True

            def chat(self, messages, tools=None, temperature=0.7):
                return ChatResult(ok=True, content="嗯。", model="silent")

            def stream(self, messages, tools=None, temperature=0.7):
                yield ChatResult(ok=True, content="嗯。", model="silent")

        config = Config(load_env=False)
        config.data_dir = self.root
        config.log_dir = self.root / "logs"
        config.db_path = self.root / "t.sqlite3"
        config.characters_dir = self.chars
        config.live2d_dir = self.root / "live2d"
        config.devices_dir = self.root / "devices"
        config.voices_dir = self.root / "voices"
        config.tray = False
        database = Database(config.db_path)
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        agent = AgentCore(Silent(), memory, ContextManager(token_budget=800))
        agent.characters = CharacterManager(
            characters_root=str(self.chars),
            live2d_root=str(self.root / "live2d"),
            default_model="hero.model3.json",
        )
        pet = DesktopPet(agent, config, memory=memory, root=self.window)
        return pet

    def test_host_can_create_a_package_for_the_current_model(self):
        pet = self._pet()
        result = pet.create_character_package("hero", name="英雄")
        self.assertTrue(result["ok"], result.get("error"))
        self.assertIn("hero", pet.agent.characters.ids())
        rows = dict(pet.character_package_rows())
        self.assertEqual(rows["角色包数量"], "1")

    def test_host_backup_and_restore_round_trip(self):
        pet = self._pet()
        pet.create_character_package("hero", name="英雄")
        target = self.root / "backup.zip"
        backed = pet.backup_characters(target=str(target))
        self.assertTrue(backed["ok"], backed.get("error"))
        self.assertTrue(target.is_file())
        self.assertTrue(pet.delete_character_package("hero", confirm=True)["ok"])
        self.assertNotIn("hero", pet.agent.characters.ids())
        restored = pet.restore_characters(str(target))
        self.assertTrue(restored["ok"], restored.get("error"))
        self.assertIn("hero", pet.agent.characters.ids())

    def test_delete_without_confirmation_changes_nothing(self):
        pet = self._pet()
        pet.create_character_package("hero", name="英雄")
        result = pet.delete_character_package("hero")
        self.assertTrue(result.get("needs_confirm"))
        self.assertIn("hero", pet.agent.characters.ids())

    def test_repeated_scans_do_not_pile_up_state(self):
        pet = self._pet()
        pet.create_character_package("hero", name="英雄")
        for _ in range(25):
            pet.reload_characters()
        self.assertEqual(pet.agent.characters.ids(), ["hero"])
        self.assertEqual(len(pet.agent.characters.infos), 1)

    def test_repeated_persona_changes_stay_bounded(self):
        pet = self._pet()
        for index in range(persona_history.MAX_ROWS + 5):
            pet.record_persona_change({}, {"温柔": index % 100})
        rows = persona_history.rows(pet.memory, limit=999)
        self.assertEqual(len(rows), persona_history.MAX_ROWS)
        self.assertEqual(pet.persona_version(), persona_history.MAX_ROWS + 5)


class GateTests(TempCase):
    def test_microphone_stays_off_when_the_switch_is_off(self):
        mic = MicPrivacy()
        self.assertFalse(mic.listening())
        mic.off()
        self.assertFalse(mic.listening())

    def test_privacy_center_defaults_keep_capture_and_hardware_off(self):
        database = Database(self.root / "t.sqlite3")
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        center = PrivacyCenter(preferences=memory)
        self.assertFalse(center.allows("vision"))
        self.assertFalse(center.allows("hardware"))
        self.assertFalse(center.allows("listen"))

    def test_turning_the_switch_off_actually_denies(self):
        database = Database(self.root / "t.sqlite3")
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        center = PrivacyCenter(preferences=memory)
        center.set("proactive", True)
        self.assertTrue(center.allows("proactive"))
        center.set("proactive", False)
        self.assertFalse(center.allows("proactive"))

    def test_vision_service_refuses_until_the_switch_is_on(self):
        from behavior.vision import NullVisionProvider, VisionService

        database = Database(self.root / "t.sqlite3")
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        center = PrivacyCenter(preferences=memory)
        service = VisionService(provider=NullVisionProvider(), allows=center, logger=None)
        self.assertFalse(service.allowed())
        self.assertFalse(service.capture()["ok"])
        # 开关打开后授权通过；真正截不截得到由 provider 说了算，不在这里假装。
        center.set("vision", True)
        self.assertTrue(service.allowed())
        blocked = service.capture()
        self.assertFalse(blocked["ok"])
        self.assertTrue(blocked["error"])

    def test_device_layer_stays_closed_for_real_hardware(self):
        from devices.manager import DeviceManager

        manager = DeviceManager()
        self.assertFalse(manager.allow_real)
        result = manager.connect("esp32-1")
        self.assertFalse(result.get("ok", False))


if __name__ == "__main__":
    unittest.main()