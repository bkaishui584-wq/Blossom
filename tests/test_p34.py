"""P4 Live2D 接入层。模型由用户自己准备，程序只发现、校验、映射、下发；不做渲染。"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from avatar.interface import AvatarProvider, DefaultAvatarProvider
from avatar.live2d import (
    ANIMATION_MOTIONS,
    EMOTION_MOTIONS,
    NO_HOST,
    NO_MODEL,
    Live2DHost,
    Live2DModel,
    Live2DProvider,
    NullLive2DHost,
    build_provider,
    find_models,
    pick_model,
)
from database.db import Database
from logkit.logger import get_logger
from security.config import Config


class FakeLogger:
    def __init__(self):
        self.warnings = []

    def warning(self, message, *args):
        self.warnings.append(message % args if args else message)


class FakeHost(Live2DHost):
    name = "fake"

    def __init__(self, available=True, boom=False):
        self.available = available
        self.boom = boom
        self.calls = []

    def is_available(self):
        return self.available

    def load(self, model):
        if self.boom:
            raise RuntimeError("渲染器炸了")
        self.calls.append(("load", model.id))
        return {"ok": True, "host": self.name}

    def set_expression(self, expression):
        if self.boom:
            raise RuntimeError("渲染器炸了")
        self.calls.append(("expression", expression))
        return {"ok": True}

    def play_motion(self, group, index=0):
        if self.boom:
            raise RuntimeError("渲染器炸了")
        self.calls.append(("motion", group))
        return {"ok": True}

    def snapshot(self):
        return {"host": self.name, "calls": len(self.calls)}


class BrokenHost(Live2DHost):
    name = "broken"

    def is_available(self):
        raise RuntimeError("探测不到渲染器")

    def load(self, model):
        return {"ok": False}

    def set_expression(self, expression):
        return {"ok": False}

    def play_motion(self, group, index=0):
        return {"ok": False}

    def snapshot(self):
        return {}


class FakeFallback(AvatarProvider):
    name = "fallback"

    def __init__(self, boom=False):
        self.boom = boom
        self.calls = []

    def set_expression(self, expression):
        if self.boom:
            raise RuntimeError("占位图坏了")
        self.calls.append(("expression", expression))
        return expression

    def play_animation(self, animation):
        if self.boom:
            raise RuntimeError("占位图坏了")
        self.calls.append(("animation", animation))
        return animation

    def set_state(self, state):
        if self.boom:
            raise RuntimeError("占位图坏了")
        self.calls.append(("state", dict(state or {})))
        return state


MODEL_JSON = {
    "Version": 3,
    "Name": "小桌",
    "FileReferences": {
        "Textures": ["小桌.2048/texture_00.png"],
        "Expressions": [
            {"Name": "smile", "File": "expressions/smile.exp.json"},
            {"Name": "sleepy", "File": "expressions/sleepy.exp.json"},
        ],
        "Motions": {
            "Idle": [{"File": "motions/idle_01.motion3.json"}],
            "TapBody": [
                {"File": "motions/tap_01.motion3.json"},
                {"File": "motions/tap_02.motion3.json"},
            ],
        },
    },
}


class TempDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write_model(self, folder="xiaozhuo", name="xiaozhuo.model3.json", data=None):
        target = self.root / folder
        target.mkdir(parents=True, exist_ok=True)
        path = target / name
        path.write_text(json.dumps(MODEL_JSON if data is None else data, ensure_ascii=False), encoding="utf-8")
        return path

    def model(self, data=None):
        return Live2DModel.from_file(self.write_model(data=data))


class DiscoveryTests(TempDir):
    def test_missing_directory_is_not_a_problem(self):
        self.assertEqual(find_models(""), [])
        self.assertEqual(find_models(str(self.root / "nope")), [])

    def test_finds_models_in_subfolders_and_root(self):
        self.write_model("xiaozhuo", "xiaozhuo.model3.json")
        self.write_model("amoy", "amoy.model.json")
        self.write_model("", "root.model3.json")
        found = find_models(str(self.root))
        self.assertEqual([model.id for model in found], ["amoy", "root", "xiaozhuo"])

    def test_broken_and_unrelated_files_are_skipped(self):
        (self.root / "bad.model3.json").write_text("{ 这不是 json", encoding="utf-8")
        (self.root / "other.json").write_text("{}", encoding="utf-8")
        (self.root / "note.txt").write_text("hello", encoding="utf-8")
        self.assertEqual(find_models(str(self.root)), [])

    def test_model_reads_its_own_declarations(self):
        model = self.model()
        self.assertEqual(model.id, "xiaozhuo")
        self.assertEqual(model.name, "小桌")
        self.assertEqual(model.label(), "小桌")
        self.assertEqual(sorted(model.expressions), ["sleepy", "smile"])
        self.assertEqual(model.motions, {"Idle": 1, "TapBody": 2})
        self.assertEqual(len(model.textures), 1)

    def test_old_style_expressions_fall_back_to_file_names(self):
        model = self.model(
            {
                "FileReferences": {
                    "expressions": ["f01.exp.json"],
                    "motions": {"idle": ["idle_01.motion3.json"]},
                }
            }
        )
        self.assertEqual(model.expressions, ("f01",))
        self.assertEqual(model.motions, {"idle": 1})
        self.assertEqual(model.label(), "xiaozhuo")

    def test_empty_model_is_still_a_model(self):
        model = self.model({})
        self.assertEqual(model.expressions, ())
        self.assertEqual(model.motions, {})
        self.assertTrue(model.describe()["id"])

    def test_describe_is_plain(self):
        info = self.model().describe()
        self.assertEqual(info["id"], "xiaozhuo")
        self.assertEqual(info["motions"]["TapBody"], 2)
        self.assertEqual(info["textures"], 1)

    def test_expression_matching(self):
        model = self.model()
        self.assertEqual(model.match_expression("smile"), "smile")
        self.assertEqual(model.match_expression("SMILE"), "smile")
        self.assertIsNone(model.match_expression("grumpy"))
        self.assertIsNone(model.match_expression(""))

    def test_motion_matching(self):
        model = self.model()
        self.assertEqual(model.match_motion("TapBody"), "TapBody")
        self.assertEqual(model.match_motion("tapbody"), "TapBody")
        self.assertIsNone(model.match_motion("Dance"))

    def test_pick_model(self):
        self.assertIsNone(pick_model([], ""))
        first = self.model()
        second = Live2DModel(id="other", name="别的")
        self.assertIs(first, pick_model([first, second], ""))
        self.assertIs(second, pick_model([first, second], "别的"))
        self.assertIs(first, pick_model([first, second], "xiaozhuo"))
        # 点名要一个没有的模型：这一个函数不猜，返回 None。兜底在下面 build_provider 那层。
        self.assertIsNone(pick_model([first], "找不到"))


class HostTests(unittest.TestCase):
    def test_null_host_says_it_is_missing(self):
        host = NullLive2DHost()
        self.assertFalse(host.is_available())
        self.assertFalse(host.load(None)["ok"])
        self.assertIn(NO_HOST, host.load(None)["error"])
        self.assertIn(NO_HOST, host.set_expression("smile")["error"])
        self.assertIn(NO_HOST, host.play_motion("Idle")["error"])
        self.assertEqual(host.describe()["available"], False)

    def test_module_needs_no_third_party(self):
        lines = (ROOT / "avatar" / "live2d.py").read_text(encoding="utf-8").splitlines()
        # 标准库 + 本项目自己的模块。这一层不许拉第三方依赖。
        allowed = ("import json", "import shutil", "from abc", "import abc", "from dataclasses",
                   "from pathlib", "from avatar", "from characters",
                   # 随包模型目录要问资源定位器，这个 import 写在函数里，按需才加载。
                   "from security")
        for line in lines:
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                self.assertTrue(stripped.startswith(allowed), stripped)
        text = "\n".join(lines).lower()
        for word in ("http://", "https://", "pip install", "live2d-py"):
            self.assertNotIn(word, text)


class IdleTests(TempDir):
    def provider(self, **kwargs):
        return Live2DProvider(**kwargs)

    def test_without_model_nothing_is_rendered(self):
        fallback = FakeFallback()
        provider = self.provider(fallback=fallback)
        provider.set_state({"emotion": "happy"})
        self.assertEqual(provider.expression, "smile")
        self.assertFalse(provider.rendering())
        self.assertIsNone(provider.describe()["model"])
        self.assertIn(NO_MODEL, provider.describe()["reason"])
        self.assertEqual(fallback.calls, [("state", {"emotion": "happy"})])

    def test_placeholder_keeps_drawing(self):
        fallback = DefaultAvatarProvider()
        provider = self.provider(fallback=fallback)
        provider.set_expression("sad")
        provider.play_animation("nod")
        self.assertEqual(fallback.expression, "sad")
        self.assertEqual(fallback.animation, "nod")

    def test_snapshot_matches_the_placeholder_shape(self):
        provider = self.provider()
        provider.set_state({"emotion": "tired"})
        snap = provider.snapshot()
        self.assertEqual(snap["expression"], "sleepy")
        self.assertEqual(snap["state"], {"emotion": "tired"})
        self.assertFalse(snap["rendering"])

    def test_provider_is_a_real_avatar(self):
        self.assertIsInstance(self.provider(), AvatarProvider)


class RenderTests(TempDir):
    def ready(self, host=None, data=None):
        host = host or FakeHost()
        provider = Live2DProvider(model=self.model(data=data), host=host)
        provider.load()
        return provider, host

    def test_load_needs_a_model(self):
        provider = Live2DProvider(host=FakeHost())
        result = provider.load()
        self.assertFalse(result["ok"])
        self.assertIn(NO_MODEL, result["error"])

    def test_load_needs_a_host(self):
        provider = Live2DProvider(model=self.model())
        result = provider.load()
        self.assertFalse(result["ok"])
        self.assertIn(NO_HOST, result["error"])
        self.assertFalse(provider.rendering())

    def test_load_hands_the_model_to_the_host(self):
        provider, host = self.ready()
        self.assertIn(("load", "xiaozhuo"), host.calls)
        self.assertTrue(provider.rendering())

    def test_emotion_goes_to_expression_and_motion(self):
        provider, host = self.ready()
        provider.set_state({"emotion": "happy"})
        self.assertIn(("expression", "smile"), host.calls)
        self.assertIn(("motion", "TapBody"), host.calls)
        self.assertEqual(provider.expressions_sent, 1)
        self.assertEqual(provider.motions_sent, 1)

    def test_missing_expression_is_reported(self):
        provider, host = self.ready()
        logger = FakeLogger()
        provider.logger = logger
        provider.set_state({"emotion": "grumpy"})
        self.assertEqual(provider.missed, 1)
        self.assertEqual(provider.last_miss["value"], "grumpy")
        self.assertNotIn(("expression", "grumpy"), host.calls)
        self.assertEqual(len(logger.warnings), 1)

    def test_missing_motion_group_is_reported(self):
        provider, host = self.ready()
        self.assertIsNone(provider._send_motion("Dance"))
        self.assertEqual(provider.missed, 1)
        self.assertEqual(provider.last_miss["value"], "Dance")

    def test_animation_maps_to_a_motion_group(self):
        provider, host = self.ready()
        provider.play_animation("nod")
        self.assertIn(("motion", "TapBody"), host.calls)
        provider.play_animation("blink")
        self.assertIn(("motion", "Idle"), host.calls)
        self.assertIn("nod", ANIMATION_MOTIONS)

    def test_nothing_is_sent_before_the_model_is_loaded(self):
        host = FakeHost()
        provider = Live2DProvider(model=self.model(), host=host)
        provider.set_state({"emotion": "happy"})
        self.assertEqual([call for call in host.calls if call[0] != "load"], [])
        self.assertIn("没装上", provider.describe()["reason"])

    def test_every_emotion_has_a_candidate(self):
        for emotion in ("neutral", "happy", "sad", "angry", "tired", "excited", "thinking"):
            self.assertIn(emotion, EMOTION_MOTIONS)

    def test_describe_shows_the_whole_picture(self):
        provider, _ = self.ready()
        provider.set_state({"emotion": "happy"})
        info = provider.describe()
        self.assertEqual(info["model"]["id"], "xiaozhuo")
        self.assertEqual(info["host"], "fake")
        self.assertTrue(info["host_available"])
        self.assertTrue(info["rendering"])
        self.assertIsNone(info["reason"])
        self.assertEqual(info["expressions_sent"], 1)


class FailureTests(TempDir):
    def test_broken_host_probe_is_contained(self):
        logger = FakeLogger()
        provider = Live2DProvider(model=self.model(), host=BrokenHost(), logger=logger)
        self.assertFalse(provider.host_available())
        result = provider.load()
        self.assertFalse(result["ok"])
        self.assertGreaterEqual(len(logger.warnings), 1)

    def test_host_errors_are_contained(self):
        logger = FakeLogger()
        provider = Live2DProvider(model=self.model(), host=FakeHost(boom=True), logger=logger)
        provider.load()
        provider.set_state({"emotion": "happy"})
        self.assertFalse(provider.rendering())
        self.assertTrue(logger.warnings)
        self.assertEqual(provider.expressions_sent, 0)

    def test_load_failure_is_reported(self):
        class NoLoad(FakeHost):
            def load(self, model):
                return {"ok": False, "error": "这个模型读不动"}

        provider = Live2DProvider(model=self.model(), host=NoLoad())
        result = provider.load()
        self.assertFalse(result["ok"])
        self.assertIn("读不动", provider.describe()["reason"])
        self.assertFalse(provider.rendering())

    def test_broken_placeholder_is_contained(self):
        logger = FakeLogger()
        provider = Live2DProvider(fallback=FakeFallback(boom=True), logger=logger)
        provider.set_state({"emotion": "happy"})
        self.assertEqual(provider.expression, "smile")
        self.assertEqual(len(logger.warnings), 1)

    def test_no_fallback_is_fine(self):
        provider = Live2DProvider()
        provider.set_expression("smile")
        provider.play_animation("nod")
        self.assertEqual(provider.expression, "smile")

    def test_state_without_emotion_is_harmless(self):
        provider = Live2DProvider()
        provider.set_state({"intensity": 0.5})
        self.assertEqual(provider.expression, "normal")


class BuilderTests(TempDir):
    def test_no_directory_no_model(self):
        config = Config(load_env=False)
        config.live2d_dir = str(self.root / "nope")
        provider = build_provider(config)
        self.assertIsNone(provider.model)
        self.assertTrue(provider.describe()["reason"])

    def test_model_is_found_and_loaded_port_is_ready(self):
        self.write_model()
        config = Config(load_env=False)
        config.live2d_dir = str(self.root)
        # 关掉真渲染端口，断言才跟这台机器装没装渲染器无关
        config.live2d_render = False
        provider = build_provider(config)
        self.assertEqual(provider.model.id, "xiaozhuo")
        self.assertIsInstance(provider.host, Live2DHost)
        self.assertFalse(provider.host_available())

    def test_config_can_pick_a_model(self):
        self.write_model("xiaozhuo", "xiaozhuo.model3.json")
        self.write_model("amoy", "amoy.model3.json")
        config = Config(load_env=False)
        config.live2d_dir = str(self.root)
        config.live2d_model = "amoy"
        self.assertEqual(build_provider(config).model.id, "amoy")
        # 配置点名的模型不在这台机器上时，退回扫到的第一个并留一条日志：
        # 换电脑或改过模型目录，桌宠不该整个消失。
        config.live2d_model = "没有这个"
        logger = FakeLogger()
        provider = build_provider(config, logger=logger)
        self.assertEqual(provider.model.id, "amoy", "点名找不到时，启动也要显示一个角色")
        self.assertTrue(
            any("没有这个" in item for item in logger.warnings), logger.warnings
        )


class WiringTests(TempDir):
    def agent(self, **overrides):
        config = Config(load_env=False)
        config.db_path = os.path.join(self.tmp.name, "deskpet.sqlite3")
        for key, value in overrides.items():
            setattr(config, key, value)
        db = Database(config.db_path)
        db.migrate()
        from app.main import build_agent

        agent, memory = build_agent(config, db, None, get_logger())
        return agent, config

    def test_config_knows_where_models_live(self):
        config = Config(load_env=False)
        self.assertTrue(str(config.live2d_dir).endswith("live2d"))
        self.assertEqual(config.live2d_model, "")
        # 目录提前建好，用户直接把模型文件夹拖进去就行。
        self.assertTrue(config.live2d_dir.is_dir())

    def test_agent_gets_the_avatar_layer(self):
        agent, _ = self.agent()
        self.assertIsInstance(agent.avatar, Live2DProvider)
        self.assertEqual(agent.avatar.name, "live2d")

    def test_agent_still_works_with_a_model_in_place(self):
        self.write_model()
        agent, _ = self.agent(live2d_dir=str(self.root))
        agent._update_emotion("happy")
        self.assertEqual(agent.avatar.expression, "smile")

    def test_ui_attaches_the_placeholder_as_fallback(self):
        text = (ROOT / "ui" / "desktop.py").read_text(encoding="utf-8")
        self.assertIn("attach_fallback", text)
        self.assertIn("形象", text)

    def test_agent_core_stays_out_of_live2d(self):
        text = (ROOT / "agent" / "core.py").read_text(encoding="utf-8").lower()
        for word in ("live2d", "avatarprovider", "cubism"):
            self.assertNotIn(word, text)


class PanelTests(TempDir):
    def setUp(self):
        super().setUp()
        try:
            import tkinter as tk

            self.root_window = tk.Tk()
        except Exception as exc:
            self.skipTest(f"tkinter 不可用：{exc}")
        self.root_window.withdraw()
        self.addCleanup(self._destroy)
        self.tk = tk

    def _destroy(self):
        try:
            self.root_window.destroy()
        except self.tk.TclError:
            pass

    def test_panel_shows_the_avatar(self):
        from app.main import build_agent
        from ui.desktop import DesktopPet

        config = Config(load_env=False)
        config.dev_mode = True
        config.db_path = os.path.join(self.tmp.name, "panel.sqlite3")
        db = Database(config.db_path)
        db.migrate()
        agent, memory = build_agent(config, db, None, get_logger())
        pet = DesktopPet(agent, config, memory=memory, root=self.root_window)
        pet._set_dev({})
        text = pet.dev.get("1.0", "end")
        self.assertIn("形象", text)
        self.assertIn("live2d", text)


if __name__ == "__main__":
    unittest.main()
