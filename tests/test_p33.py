"""P3-4：多宠物。名册、切换、每只自己的会话。多智能体部分是双模型 + 每只独立会话。"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.capabilities import CapabilityRouter
from app.main import build_agent
from database.db import Database
from logkit.logger import get_logger
from persona.interface import SAFETY_BOUNDARY, build_persona_note
from pets.manager import DEFAULT_PET_ID, PetManager, PetProfile, from_config
from security.config import Config


class FakePersona:
    def __init__(self):
        self.selected = []

    def select(self, package_id):
        self.selected.append(package_id)
        return package_id


class TempDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def characters_dir(self, *names):
        root = Path(self.tmp.name) / "characters"
        for name in names:
            folder = root / name
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "character.json").write_text(
                json.dumps(
                    {
                        "id": name,
                        "name": "小" + name,
                        "description": "测试角色",
                        "persona": {"name": "小" + name, "traits": ["温柔"]},
                        "avatar": {"provider": "live2d", "model": name + ".model3.json"},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        return str(root)

    def two_pets(self):
        roster = PetManager(
            profiles=[
                PetProfile(id="default", name="小桌"),
                PetProfile(id="xiaozhuo", name="小桌", persona_package="xiaozhuo"),
            ]
        )
        return roster


class ProfileTests(unittest.TestCase):
    def test_needs_an_id(self):
        self.assertIsNone(PetProfile.from_dict({}))
        self.assertIsNone(PetProfile.from_dict(None))

    def test_missing_fields_are_fine(self):
        profile = PetProfile.from_dict({"name": "小家伙"})
        self.assertIsNotNone(profile)
        self.assertEqual(profile.id, "小家伙")
        self.assertEqual(profile.persona_package, "")
        self.assertEqual(profile.device, "")
        self.assertEqual(profile.label(), "小家伙")

    def test_describe_is_plain(self):
        profile = PetProfile.from_dict({"id": "a", "persona": "包a", "avatar": "a.model3.json"})
        info = profile.describe()
        self.assertEqual(info["id"], "a")
        self.assertEqual(info["persona_package"], "包a")
        self.assertEqual(info["avatar"], "a.model3.json")


class RosterTests(TempDir):
    def test_empty_roster_still_has_one_pet(self):
        roster = PetManager()
        self.assertEqual(roster.names(), [DEFAULT_PET_ID])
        self.assertEqual(roster.active, DEFAULT_PET_ID)
        self.assertEqual(roster.session_id(), "pet:default")

    def test_register_replaces_same_id_only(self):
        roster = PetManager(profiles=[PetProfile(id="a", name="A"), PetProfile(id="b", name="B")])
        roster.register(PetProfile(id="a", name="新A"))
        self.assertEqual(roster.names(), ["a", "b"])
        self.assertEqual(roster.get("a").name, "新A")
        self.assertEqual(roster.get("b").name, "B")

    def test_bad_profile_is_refused(self):
        roster = PetManager()
        self.assertFalse(roster.register(None)["ok"])
        self.assertFalse(roster.register(PetProfile(id="  "))["ok"])

    def test_unknown_active_falls_back(self):
        roster = PetManager(profiles=[PetProfile(id="a")], active="ghost")
        self.assertEqual(roster.active, "a")

    def test_switching_takes_the_persona_package(self):
        persona = FakePersona()
        roster = self.two_pets()
        roster.persona = persona
        result = roster.select("xiaozhuo")
        self.assertTrue(result["ok"])
        self.assertEqual(roster.active, "xiaozhuo")
        self.assertEqual(persona.selected, ["xiaozhuo"])
        self.assertEqual(roster.session_id(), "pet:xiaozhuo")

    def test_switching_back_uses_the_default_persona(self):
        persona = FakePersona()
        roster = self.two_pets()
        roster.persona = persona
        roster.select("xiaozhuo")
        roster.select(DEFAULT_PET_ID)
        self.assertEqual(persona.selected, ["xiaozhuo", ""])

    def test_unknown_pet_is_refused(self):
        roster = self.two_pets()
        result = roster.select("nobody")
        self.assertFalse(result["ok"])
        self.assertIn("没有叫", result["error"])
        self.assertEqual(roster.active, DEFAULT_PET_ID)
        self.assertEqual(roster.switch_count, 0)

    def test_broken_persona_is_contained(self):
        class Broken:
            def select(self, package_id):
                raise RuntimeError("provider gone")

        roster = self.two_pets()
        roster.persona = Broken()
        self.assertTrue(roster.select("xiaozhuo")["ok"])

    def test_describe_is_for_the_dev_panel(self):
        roster = self.two_pets()
        roster.select("xiaozhuo")
        info = roster.describe()
        self.assertEqual(info["count"], 2)
        self.assertEqual(info["active"], "xiaozhuo")
        self.assertEqual(info["switches"], 1)
        self.assertEqual(len(info["pets"]), 2)


class ConfigTests(TempDir):
    def test_character_packages_become_pets(self):
        config = Config(load_env=False)
        config.characters_dir = self.characters_dir("xiaozhuo", "amoy")
        roster = from_config(config, persona=FakePersona())
        self.assertEqual(roster.names(), sorted([DEFAULT_PET_ID, "amoy", "xiaozhuo"]))
        profile = roster.get("xiaozhuo")
        self.assertEqual(profile.persona_package, "xiaozhuo")
        self.assertEqual(profile.avatar, "xiaozhuo.model3.json")

    def test_active_pet_follows_the_config(self):
        config = Config(load_env=False)
        config.characters_dir = self.characters_dir("xiaozhuo")
        config.character = "xiaozhuo"
        roster = from_config(config, persona=FakePersona())
        self.assertEqual(roster.active, "xiaozhuo")

    def test_missing_directory_is_not_a_problem(self):
        config = Config(load_env=False)
        config.characters_dir = os.path.join(self.tmp.name, "nope")
        roster = from_config(config, persona=None)
        self.assertEqual(roster.names(), [DEFAULT_PET_ID])

    def test_refresh_reapplies_the_current_pet(self):
        persona = FakePersona()
        roster = from_config(Config(load_env=False), persona=persona)
        roster.refresh()
        self.assertEqual(persona.selected, [""])


class CapabilityTests(unittest.TestCase):
    def router(self, roster=None):
        router = CapabilityRouter(memory=None)
        if roster is not None:
            router.pets = roster
        return router

    def test_without_a_roster_it_stays_quiet(self):
        self.assertIsNone(self.router().resolve("有哪些宠物"))

    def test_a_single_pet_says_so(self):
        router = self.router(PetManager())
        answer = router.resolve("有哪些宠物")
        self.assertEqual(answer.capability, "pets")
        self.assertIn("就我一个", answer.text)

    def test_list_is_written_by_the_program(self):
        roster = PetManager(
            profiles=[PetProfile(id="a", name="阿墨"), PetProfile(id="b", name="小盐")]
        )
        answer = self.router(roster).resolve("宠物列表")
        self.assertIn("阿墨", answer.text)
        self.assertIn("小盐", answer.text)

    def test_switching_by_name(self):
        roster = PetManager(
            profiles=[PetProfile(id="a", name="阿墨"), PetProfile(id="b", name="小盐")]
        )
        answer = self.router(roster).resolve("换个宠物 小盐")
        self.assertEqual(roster.active, "b")
        self.assertIn("小盐", answer.text)

    def test_plain_chat_is_not_swallowed(self):
        router = self.router(PetManager())
        self.assertIsNone(router.resolve("今天想去公园走走"))


class AgentTests(TempDir):
    def agent(self, **overrides):
        config = Config(load_env=False)
        config.db_path = os.path.join(self.tmp.name, "deskpet.sqlite3")
        for key, value in overrides.items():
            setattr(config, key, value)
        db = Database(config.db_path)
        db.migrate()
        return build_agent(config, db, None, get_logger()), config

    def test_build_agent_attaches_the_roster(self):
        (agent, _), _ = self.agent()
        roster = agent.capabilities.pets
        self.assertIsInstance(roster, PetManager)
        self.assertEqual(roster.names(), [DEFAULT_PET_ID])
        self.assertTrue(roster.current().label())

    def test_local_capability_answers_without_a_model(self):
        (agent, _), _ = self.agent()
        reply = agent.respond("desktop", "有哪些宠物")
        self.assertTrue(reply.ok)
        self.assertIn("就我一个", reply.content)

    def test_switching_a_pet_never_touches_the_safety_boundary(self):
        (agent, _), _ = self.agent(characters_dir=self.characters_dir("xiaozhuo"))
        roster = agent.capabilities.pets
        self.assertTrue(roster.select("xiaozhuo")["ok"])
        note = build_persona_note(agent.persona)
        self.assertIn(SAFETY_BOUNDARY, note)

    def test_each_pet_has_its_own_session(self):
        (agent, _), _ = self.agent(characters_dir=self.characters_dir("xiaozhuo"))
        roster = agent.capabilities.pets
        first = roster.session_id()
        roster.select("xiaozhuo")
        self.assertNotEqual(first, roster.session_id())


class PanelTests(TempDir):
    def setUp(self):
        super().setUp()
        try:
            import tkinter as tk

            self.root = tk.Tk()
        except Exception as exc:
            self.skipTest(f"tkinter 不可用：{exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy)
        self.tk = tk

    def _destroy(self):
        try:
            self.root.destroy()
        except self.tk.TclError:
            pass

    def test_panel_shows_the_pets(self):
        from ui.desktop import DesktopPet

        config = Config(load_env=False)
        config.dev_mode = True
        # 这台机器上装了真模型时，测试不该顺手把真渲染窗口拉起来。
        config.live2d_dir = os.path.join(self.tmp.name, "live2d")
        db = Database(os.path.join(self.tmp.name, "panel.sqlite3"))
        db.migrate()
        agent, memory = build_agent(config, db, None, get_logger())
        pet = DesktopPet(agent, config, memory=memory, root=self.root)
        pet._set_dev({})
        text = pet.dev.get("1.0", "end")
        self.assertIn("宠物", text)


class BoundaryTests(unittest.TestCase):
    def test_manager_talks_to_no_model_or_database(self):
        text = (ROOT / "pets" / "manager.py").read_text(encoding="utf-8")
        for word in ("ModelProvider", "provider.chat", "sqlite", "db.execute", "INSERT"):
            self.assertNotIn(word, text)

    def test_agent_core_does_not_know_about_pets(self):
        text = (ROOT / "agent" / "core.py").read_text(encoding="utf-8")
        for word in ("PetManager", "pets.manager", "PetProfile"):
            self.assertNotIn(word, text)

    def test_capability_takes_the_roster_from_the_router(self):
        text = (ROOT / "agent" / "capabilities.py").read_text(encoding="utf-8")
        self.assertIn('getattr(ctx, "pets", None)', text)
        self.assertIn('"pets"', text)


if __name__ == "__main__":
    unittest.main()
