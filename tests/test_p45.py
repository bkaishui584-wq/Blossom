"""P45 阶段 C：角色包契约、动作注册与映射、重载与降级。

这一层要保证的事：
    角色包写什么就真读什么，两种写法都收，认不出的名字只报告不改文件；
    动作能对到模型真实声明的名字上就报可用，对不上就说不可用，绝不拿别的动作顶替；
    换角色、重扫、重载失败都保留原来的选择，桌宠不会空着；
    没装角色包时行为与以前完全一致。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from avatar.live2d import (
    ANIMATION_MOTIONS,
    EMOTION_MOTIONS,
    Live2DHost,
    Live2DModel,
    Live2DProvider,
    NullLive2DHost,
    build_provider,
)
from characters import actions
from characters.manager import CharacterManager
from persona.package import CharacterPackage
from security.config import Config

REFS = {
    "Moc": "hero.moc3",
    "Textures": ["hero.2048/texture_00.png"],
    "Expressions": [{"Name": "smile", "File": "exp/smile.exp.json"}],
    "Motions": {
        "Idle": [{"File": "m/idle.motion3.json"}],
        "TapBody": [{"File": "m/tap.motion3.json"}],
        "HappyGroup": [{"File": "m/happy.motion3.json"}],
    },
}
FILES = (
    "hero.moc3",
    "hero.2048/texture_00.png",
    "exp/smile.exp.json",
    "m/idle.motion3.json",
    "m/tap.motion3.json",
    "m/happy.motion3.json",
)


class ListLogger:
    def __init__(self):
        self.lines = []

    def warning(self, *args):
        self.lines.append(" ".join(str(item) for item in args))

    def info(self, *args):
        self.lines.append(" ".join(str(item) for item in args))

    def debug(self, *args):
        pass

    def text(self):
        return " ".join(self.lines)


class FakeHost(Live2DHost):
    name = "fake"

    def __init__(self, available=True):
        self.available = available
        self.calls = []

    def is_available(self):
        return self.available

    def load(self, model):
        self.calls.append(("load", model.id))
        return {"ok": True, "host": self.name}

    def set_expression(self, expression):
        self.calls.append(("expression", expression))
        return {"ok": True}

    def play_motion(self, group, index=0):
        self.calls.append(("motion", group))
        return {"ok": True}

    def snapshot(self):
        return {"host": self.name, "calls": len(self.calls)}


class TempDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write_model(self, name="hero", refs=None, files=None, folder="live2d"):
        base = self.root / folder
        base.mkdir(parents=True, exist_ok=True)
        data = {"Version": 3, "FileReferences": dict(REFS if refs is None else refs)}
        for item in FILES if files is None else files:
            target = base / item
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x")
        path = base / (name + ".model3.json")
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def write_package(self, folder, data):
        target = self.root / "characters" / folder
        target.mkdir(parents=True, exist_ok=True)
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        (target / "character.json").write_text(text, encoding="utf-8")
        return target

    def config(self):
        config = Config(load_env=False)
        base = self.root
        config.data_dir = base
        config.log_dir = base / "logs"
        config.db_path = base / "t.sqlite3"
        config.characters_dir = base / "characters"
        config.live2d_dir = base / "live2d"
        config.devices_dir = base / "devices"
        config.voices_dir = base / "voices"
        config.tray = False
        return config

    def manager(self, selected="", default_model=""):
        return CharacterManager(
            characters_root=str(self.root / "characters"),
            live2d_root=str(self.root / "live2d"),
            selected=selected,
            default_model=default_model,
            logger=ListLogger(),
        )


class ActionTableTests(unittest.TestCase):
    def test_both_writing_styles_are_accepted(self):
        mapping = actions.normalize(
            {
                actions.HAPPY: "HappyGroup",
                actions.TAP: {"motion": "TapBody", "index": 2, "expression": "smile"},
            }
        )
        self.assertEqual(mapping[actions.HAPPY], {"motion": "HappyGroup", "index": 0, "expression": ""})
        self.assertEqual(mapping[actions.TAP]["index"], 2)
        self.assertEqual(mapping[actions.TAP]["expression"], "smile")

    def test_bad_values_are_skipped_not_guessed(self):
        mapping = actions.normalize(
            {
                actions.IDLE: {"motion": "Idle", "index": "很多"},
                actions.SLEEP: 5,
                actions.GREETING: {"motion": ""},
            }
        )
        self.assertEqual(mapping[actions.IDLE]["index"], 0)
        self.assertNotIn(actions.SLEEP, mapping)
        self.assertNotIn(actions.GREETING, mapping)

    def test_unknown_action_names_are_reported_not_renamed(self):
        raw = {actions.HAPPY: "Happy", "跳舞": "Dance", " Idle ": "Idle", "": "x"}
        self.assertEqual(actions.unknown_names(raw), ["跳舞"])
        mapping = actions.normalize(raw)
        self.assertNotIn("跳舞", mapping)
        self.assertIn(actions.IDLE, mapping)

    def test_every_state_of_a_mapping_is_visible(self):
        model = Live2DModel(
            id="m", motions={"Idle": 1, "HappyGroup": 2}, expressions=("smile",)
        )
        mapping = actions.normalize(
            {
                actions.HAPPY: "HappyGroup",
                actions.SAD: {"motion": "SadGroup", "expression": "smile"},
                actions.TAP: "NoSuchGroup",
            }
        )
        happy = actions.resolve(mapping, actions.HAPPY, model)
        self.assertEqual(happy["status"], actions.OK)
        self.assertEqual(happy["motion"], "HappyGroup")
        sad = actions.resolve(mapping, actions.SAD, model)
        self.assertEqual(sad["status"], actions.OK)
        self.assertEqual(sad["expression"], "smile")
        tap = actions.resolve(mapping, actions.TAP, model)
        self.assertEqual(tap["status"], actions.MISSING)
        unset = actions.resolve(mapping, actions.GREETING, model)
        self.assertEqual(unset["status"], actions.NOT_MAPPED)
        nomodel = actions.resolve(mapping, actions.HAPPY, None)
        self.assertEqual(nomodel["status"], actions.NO_MODEL)
        self.assertNotEqual(actions.resolve(mapping, "跳舞", model)["status"], actions.OK)

    def test_report_covers_every_logical_action(self):
        data = actions.report({}, None)
        self.assertEqual(data["total"], len(actions.ACTIONS))
        self.assertEqual(len(data["rows"]), len(actions.ACTIONS))
        self.assertEqual(data["usable"], 0)
        self.assertIn("没有可用动作", actions.summary({}, None))

    def test_the_reserved_actions_are_all_there(self):
        for name in (
            "Idle",
            "Blink",
            "Speak",
            "Think",
            "Happy",
            "Sad",
            "Curious",
            "Sleep",
            "Tap",
            "Drag",
            "Greeting",
            "TaskStart",
            "TaskSuccess",
            "TaskFailure",
        ):
            self.assertIn(name, actions.ACTIONS, name)

    def test_emotions_and_animations_point_at_real_actions(self):
        for name, action in actions.EMOTION_ACTIONS.items():
            self.assertIn(action, actions.ACTIONS, name)
        for name, action in actions.ANIMATION_ACTIONS.items():
            self.assertIn(action, actions.ACTIONS, name)
        self.assertEqual(set(actions.ANIMATION_ACTIONS), set(ANIMATION_MOTIONS))
        self.assertEqual(set(actions.EMOTION_ACTIONS) - set(EMOTION_MOTIONS), set())


class PackageCompatTests(unittest.TestCase):
    def test_old_manifest_without_new_fields_still_reads(self):
        package = CharacterPackage.from_dict(
            {"id": "old", "name": "旧的", "persona": {"style": "短"}}, path="x"
        )
        self.assertEqual(package.version, "")
        self.assertEqual(package.actions, {})
        self.assertEqual(package.voice, {})
        self.assertEqual(package.describe()["actions"], 0)
        self.assertFalse(package.describe()["has_voice"])

    def test_extension_fields_are_read(self):
        package = CharacterPackage.from_dict(
            {
                "id": "new",
                "version": "2.0",
                "actions": {"Happy": "HappyGroup"},
                "voice": {"pack": "new"},
            },
            path="x",
        )
        self.assertEqual(package.version, "2.0")
        self.assertIn("Happy", package.actions)
        self.assertTrue(package.describe()["has_voice"])

    def test_wrong_shaped_sections_do_not_crash(self):
        package = CharacterPackage.from_dict(
            {"id": "x", "actions": ["Happy"], "voice": "pack", "avatar": 3}, path="x"
        )
        self.assertEqual(package.actions, {})
        self.assertEqual(package.voice, {})
        self.assertEqual(package.avatar, {})


class ManagerTests(TempDir):
    def hero_package(self, actions_map=None):
        return self.write_package(
            "hero",
            {
                "id": "hero",
                "name": "英雄",
                "version": "1.0",
                "avatar": {"provider": "live2d", "model": "hero.model3.json", "scale": 1.5},
                "actions": actions_map or {"Idle": "Idle", "Happy": {"motion": "HappyGroup"}},
            },
        )

    def test_nothing_installed_is_not_an_error(self):
        manager = self.manager()
        self.assertEqual(manager.ids(), [])
        self.assertIsNone(manager.model())
        self.assertEqual(manager.action_map(), {})
        result = manager.select("nope")
        self.assertFalse(result["ok"])
        self.assertIn("nope", result["error"])
        self.assertTrue(manager.rows())
        self.assertEqual(manager.describe()["installed"], [])

    def test_package_model_and_scale_are_picked_up(self):
        self.write_model()
        self.hero_package()
        manager = self.manager(selected="hero")
        self.assertEqual(manager.current().name, "英雄")
        self.assertEqual(manager.current().version, "1.0")
        self.assertEqual(manager.current().scale(), 1.5)
        self.assertEqual(manager.model().id, "hero")
        self.assertEqual(manager.action_report()["usable"], 2)

    def test_missing_model_file_is_reported_and_does_not_blank_the_pet(self):
        self.write_model()
        self.write_package("ghost", {"id": "ghost", "avatar": {"model": "ghost.model3.json"}})
        manager = self.manager(selected="ghost")
        problems = "；".join(manager.current().problems)
        self.assertIn("找不到模型文件", problems)
        self.assertTrue(manager.current().blocking())
        self.assertEqual(manager.model().id, "hero")
        text = "\n".join("%s=%s" % row for row in manager.rows())
        self.assertIn("找不到模型文件", text)

    def test_select_failure_keeps_the_previous_character(self):
        self.write_model()
        self.hero_package()
        self.write_package("ghost", {"id": "ghost", "avatar": {"model": "ghost.model3.json"}})
        manager = self.manager()
        self.assertTrue(manager.select("hero")["ok"])
        refused = manager.select("ghost")
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["kept"], "hero")
        self.assertEqual(manager.selected, "hero")

    def test_unknown_action_name_shows_up_as_a_problem(self):
        self.write_model()
        self.hero_package({"Idle": "Idle", "跳舞": "Dance"})
        manager = self.manager(selected="hero")
        self.assertIn("认不出的动作名 跳舞", "；".join(manager.current().problems))
        self.assertNotIn("跳舞", manager.action_map())

    def test_broken_package_is_skipped(self):
        self.write_package("broken", "{这不是合法 json")
        self.write_package("good", {"id": "good", "name": "好的"})
        self.assertEqual(self.manager().ids(), ["good"])

    def test_reload_reports_new_motions(self):
        path = self.write_model(
            refs={"Moc": "hero.moc3", "Motions": {"Idle": [{"File": "m/idle.motion3.json"}]}},
            files=("hero.moc3", "m/idle.motion3.json"),
        )
        self.hero_package({"Happy": "HappyGroup"})
        manager = self.manager(selected="hero")
        self.assertEqual(manager.action_report()["usable"], 0)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["FileReferences"]["Motions"] = REFS["Motions"]
        (self.root / "live2d" / "m" / "happy.motion3.json").parent.mkdir(parents=True, exist_ok=True)
        (self.root / "live2d" / "m" / "happy.motion3.json").write_bytes(b"x")
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        result = manager.reload()
        self.assertTrue(result["ok"])
        self.assertIn("hero", result["changed"])
        self.assertEqual(manager.action_report()["usable"], 1)

    def test_reload_after_the_model_disappears_keeps_the_selection(self):
        path = self.write_model()
        self.hero_package()
        manager = self.manager(selected="hero")
        self.assertTrue(manager.current().model is not None)
        path.unlink()
        result = manager.reload()
        self.assertFalse(result["ok"])
        self.assertEqual(manager.selected, "hero")
        self.assertIsNone(manager.current().model)

    def test_rows_are_truthful_without_packages(self):
        rows = dict(self.manager().rows())
        self.assertEqual(rows["角色包"], "还没放角色包")
        self.assertIn("正在用的形象", rows)


class ProviderActionTests(TempDir):
    def provider(self, action_map=None, model=True):
        host = FakeHost()
        entry = self.write_model() if model else None
        built = Live2DModel.from_file(entry) if entry is not None else None
        provider = Live2DProvider(
            model=built,
            host=host,
            action_map=action_map,
            logger=ListLogger(),
        )
        if built is not None:
            provider.load()
        return provider, host

    def test_action_map_drives_motion_and_expression(self):
        mapping = actions.normalize(
            {actions.HAPPY: {"motion": "HappyGroup", "expression": "smile"}}
        )
        provider, host = self.provider(mapping)
        plan = provider.play_action(actions.HAPPY)
        self.assertEqual(plan["status"], actions.OK)
        self.assertIn(("motion", "HappyGroup"), host.calls)
        self.assertIn(("expression", "smile"), host.calls)
        self.assertEqual(provider.actions_played, 1)
        self.assertEqual(provider.last_action["status"], actions.OK)

    def test_a_missing_resource_is_never_substituted(self):
        mapping = actions.normalize({actions.HAPPY: "Dance"})
        provider, host = self.provider(mapping)
        plan = provider.play_action(actions.HAPPY)
        self.assertEqual(plan["status"], actions.MISSING)
        self.assertNotIn(("motion", "Idle"), host.calls)
        self.assertNotIn(("motion", "TapBody"), host.calls)
        self.assertEqual(provider.missed, 1)
        self.assertEqual(provider.last_action["status"], actions.MISSING)

    def test_emotion_goes_through_the_action_map(self):
        mapping = actions.normalize({actions.HAPPY: "HappyGroup"})
        provider, host = self.provider(mapping)
        provider.set_state({"emotion": "happy"})
        self.assertIn(("motion", "HappyGroup"), host.calls)
        self.assertNotIn(("expression", "smile"), host.calls)

    def test_unmapped_emotion_falls_back_to_the_default_look(self):
        mapping = actions.normalize({actions.TAP: "TapBody"})
        provider, host = self.provider(mapping)
        provider.set_state({"emotion": "happy"})
        self.assertIn(("expression", "smile"), host.calls)
        self.assertEqual(provider.missed, 0)

    def test_behaviour_without_an_action_map_is_unchanged(self):
        provider, host = self.provider()
        provider.set_state({"emotion": "happy"})
        self.assertIn(("expression", "smile"), host.calls)
        provider.play_animation("nod")
        self.assertIn(("motion", "TapBody"), host.calls)

    def test_animation_names_use_the_action_map_when_configured(self):
        mapping = actions.normalize({actions.BLINK: "Idle"})
        provider, host = self.provider(mapping)
        provider.play_animation("blink")
        self.assertIn(("motion", "Idle"), host.calls)
        self.assertEqual(provider.missed, 0)
        provider.play_animation("nod")
        self.assertIn(("motion", "TapBody"), host.calls)

    def test_unknown_action_is_reported_and_not_played(self):
        provider, host = self.provider(actions.normalize({actions.HAPPY: "HappyGroup"}))
        plan = provider.play_action("跳舞")
        self.assertNotEqual(plan["status"], actions.OK)
        self.assertEqual([call for call in host.calls if call[0] != "load"], [])
        self.assertEqual(provider.actions_played, 0)

    def test_action_without_a_model_says_so(self):
        provider, _ = self.provider(actions.normalize({actions.HAPPY: "HappyGroup"}), model=False)
        plan = provider.play_action(actions.HAPPY)
        self.assertEqual(plan["status"], actions.NO_MODEL)
        self.assertEqual(provider.actions_played, 0)

    def test_refresh_swaps_the_model_and_the_action_map(self):
        provider, host = self.provider(actions.normalize({actions.HAPPY: "Dance"}))
        self.assertEqual(provider.play_action(actions.HAPPY)["status"], actions.MISSING)
        self.write_model(
            name="other",
            refs={"Moc": "hero.moc3", "Motions": {"Dance": [{"File": "m/dance.motion3.json"}]}},
            files=("hero.moc3", "m/dance.motion3.json"),
        )
        other = Live2DModel.from_file(self.root / "live2d" / "other.model3.json")
        result = provider.refresh(other, actions.normalize({actions.HAPPY: "Dance"}))
        self.assertTrue(result["ok"])
        plan = provider.play_action(actions.HAPPY)
        self.assertEqual(plan["status"], actions.OK)
        self.assertIn(("motion", "Dance"), host.calls)

    def test_describe_shows_the_real_action_state(self):
        provider, _ = self.provider(actions.normalize({actions.HAPPY: "HappyGroup"}))
        info = provider.describe()
        self.assertIn("1/", info["actions"])
        self.assertIsNone(info["last_action"])
        self.assertEqual(info["actions_played"], 0)


class WiringTests(TempDir):
    def test_build_provider_follows_the_character_manager(self):
        self.write_model()
        self.write_package(
            "hero",
            {
                "id": "hero",
                "avatar": {"model": "hero.model3.json"},
                "actions": {"Happy": "HappyGroup"},
            },
        )
        manager = self.manager(selected="hero")
        provider = build_provider(self.config(), characters=manager)
        self.assertEqual(provider.model.id, "hero")
        self.assertIn(actions.HAPPY, provider.action_map)
        self.assertEqual(provider.action_report()["usable"], 1)

    def test_build_provider_without_a_manager_is_unchanged(self):
        self.write_model()
        config = self.config()
        provider = build_provider(config, host=NullLive2DHost())
        self.assertEqual(provider.model.id, "hero")
        self.assertEqual(provider.action_map, {})

    def test_build_agent_attaches_the_character_manager(self):
        from app.main import build_agent
        from database.db import Database
        from logkit.logger import get_logger

        self.write_model()
        self.write_package(
            "hero",
            {
                "id": "hero",
                "name": "英雄",
                "avatar": {"model": "hero.model3.json"},
                "actions": {"Idle": "Idle"},
            },
        )
        config = self.config()
        config.character = "hero"
        database = Database(config.db_path)
        database.migrate()
        agent, _ = build_agent(config, database, None, get_logger())
        self.assertIsInstance(agent.characters, CharacterManager)
        self.assertEqual(agent.characters.ids(), ["hero"])
        self.assertEqual(agent.avatar.model.id, "hero")
        self.assertEqual(agent.avatar.action_map.get("Idle"), {"motion": "Idle", "index": 0, "expression": ""})
        self.assertEqual(agent.characters.describe()["actions"], 1)


try:
    import tkinter as tk
except Exception:
    tk = None

from ui.desktop import DesktopPet  # noqa: E402


class DesktopRowsTests(TempDir):
    def setUp(self):
        super().setUp()
        if tk is None:
            self.skipTest("tkinter 不可用")
        try:
            self.root_window = tk.Tk()
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        self.root_window.withdraw()
        self.addCleanup(self._destroy)

    def _destroy(self):
        try:
            self.root_window.destroy()
        except Exception:
            pass

    def pet(self):
        from agent.context import ContextManager
        from agent.core import AgentCore
        from database.db import Database
        from memory.manager import MemoryManager
        from memory.repository import MemoryRepository
        from model.base import ChatResult, ModelProvider

        class Silent(ModelProvider):
            name = "silent"

            def is_available(self):
                return True

            def chat(self, messages, tools=None, temperature=0.7):
                return ChatResult(ok=True, content="嗯。", model="silent")

            def stream(self, messages, tools=None, temperature=0.7):
                yield ChatResult(ok=True, content="嗯。", model="silent")

        self.write_model()
        self.write_package("hero", {"id": "hero", "name": "英雄"})
        config = self.config()
        database = Database(config.db_path)
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        agent = AgentCore(Silent(), memory, ContextManager(token_budget=800))
        agent.characters = self.manager()
        agent.avatar = build_provider(config, characters=agent.characters, host=NullLive2DHost())
        return DesktopPet(agent, config, memory=memory, root=self.root_window)

    def test_avatar_rows_show_the_character_packages(self):
        rows = dict(self.pet().avatar_rows())
        self.assertEqual(rows["可选角色"], "英雄")
        self.assertEqual(rows["角色包问题"], "没有")
        self.assertIn("当前显示模型", rows)
        self.assertIn("可用动作", rows)

    def test_developer_rows_show_the_package_state(self):
        rows = dict(self.pet().developer_rows())
        self.assertIn("角色包", rows)
        self.assertIn("hero", rows["角色包"])

    def test_rescan_only_reports_real_changes(self):
        pet = self.pet()
        self.assertEqual(pet._rescan_characters(), (True, "", False))
        self.write_package("second", {"id": "second", "name": "二号"})
        ok, note, changed = pet._rescan_characters()
        self.assertTrue(ok)
        self.assertTrue(changed)
        self.assertIn("有变化", note)

    def test_reload_without_a_renderer_stays_calm(self):
        pet = self.pet()
        self.assertFalse(pet.live2d_reload())
        self.assertIsNone(pet.live2d)

    def test_developer_rows_say_when_nothing_is_installed(self):
        pet = self.pet()
        pet.agent.characters = CharacterManager(
            characters_root=str(self.root / "nope"), live2d_root=str(self.root / "live2d")
        )
        rows = dict(pet.developer_rows())
        self.assertEqual(rows["角色包"], "还没有角色包")


if __name__ == "__main__":
    unittest.main()
