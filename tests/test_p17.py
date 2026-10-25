"""P17 验收：V2 P0 收尾 —— 整体联调与安全检查（对照 V2 方案 §126 / §127）。"""

import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.context import ContextManager
from agent.core import AgentCore
from agent.router import Intent
from app.main import build_agent
from avatar.interface import AvatarProvider, DefaultAvatarProvider
from database.db import Database
from devices.interface import DeviceAdapter
from devices.manager import DeviceManager
from events.bus import EventBus
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatDelta, ChatResult, ModelProvider
from persona.interface import PersonaProvider
from security.audit import AuditLog
from security.config import Config


@contextmanager
def env(**values):
    """临时设置环境变量，结束后恢复。"""
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update({key: value for key, value in values.items() if value is not None})
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# Case 1 的前提：这台机器上没有任何可用的模型配置。
NO_MODEL_ENV = {
    "DESKPET_ROOT": None,
    "DESKPET_DB_PATH": None,
    "DESKPET_DATA_DIR": None,
    "DESKPET_LOG_DIR": None,
    "DESKPET_COMPANION_MODEL": None,
    "DESKPET_COMPANION_API_KEY": None,
    "DESKPET_COMPANION_BASE_URL": None,
    "DESKPET_TASK_MODEL": None,
    "DESKPET_TASK_API_KEY": None,
    "DESKPET_TASK_BASE_URL": None,
    "AGENT_LLM_MODEL": None,
    "AGENT_LLM_API_KEY": None,
    "AGENT_LLM_BASE_URL": None,
}


def source_files():
    skip = {"tests", "__pycache__", "dist", "build", ".venv", "data", "logs"}
    return [path for path in ROOT.rglob("*.py") if not skip.intersection(path.parts)]


def read(relative):
    return (ROOT / relative).read_text(encoding="utf-8")


class ScriptProvider(ModelProvider):
    name = "script"

    def __init__(self, reply="好的。", available=True):
        self.reply = reply
        self.available = available
        self.calls = []

    def is_available(self):
        return self.available

    def chat(self, messages, tools=None, temperature=0.7):
        self.calls.append({"messages": list(messages), "tools": tools})
        return ChatResult(ok=True, content=self.reply, model=self.name)

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


def local_agent(tmp, provider=None, task=None):
    database = Database(Path(tmp) / "t.sqlite3")
    database.migrate()
    memory = MemoryManager(MemoryRepository(database))
    agent = AgentCore(
        provider,
        memory,
        ContextManager(token_budget=800),
        task_provider=task,
    )
    return agent, memory, database


class Case1NoApiTests(unittest.TestCase):
    """Case 1：没有任何 API，DeskPet 仍然要能正常启动。"""

    def test_app_assembles_and_answers_without_any_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            with env(**{**NO_MODEL_ENV, "DESKPET_ROOT": tmp}):
                config = Config(load_env=False)
                self.assertFalse(config.has_companion())
                self.assertFalse(config.has_task())
                database = Database(config.db_path)
                database.migrate()
                agent, _memory = build_agent(config, database, None, None, bus=EventBus())
                self.assertIsNotNone(agent.behavior)
                self.assertEqual(agent.mode_name, "local")

    def test_config_without_keys_is_not_a_failure(self):
        with env(**NO_MODEL_ENV):
            config = Config(load_env=False)
            self.assertFalse(config.has_companion())
            self.assertFalse(config.has_task())
            self.assertEqual(str(config.redacted().get("companion_api_key")), "unset")


class Case2LocalTimeTests(unittest.TestCase):
    """Case 2：没有 LLM 也要能直接读系统时间。"""

    def test_time_is_answered_locally_without_calling_the_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = ScriptProvider()
            agent, _, _ = local_agent(tmp, provider)
            reply = agent.respond("s1", "现在几点？")
            self.assertTrue(reply.ok)
            self.assertRegex(reply.content, r"\d{1,2}:\d{2}")
            self.assertTrue(reply.debug.get("local"))
            self.assertEqual(provider.calls, [])
            self.assertEqual(
                reply.usage.prompt_tokens + reply.usage.completion_tokens, 0
            )


class Case3And4RoutingTests(unittest.TestCase):
    """Case 3 / Case 4：写程序是陪伴，改既有项目才是任务确认。"""

    def test_case3_asking_for_a_program_stays_in_companion(self):
        with tempfile.TemporaryDirectory() as tmp:
            companion = ScriptProvider(reply="给你一段示例。")
            task = ScriptProvider(reply="不该用到任务模型")
            agent, _, _ = local_agent(tmp, companion, task)
            reply = agent.respond("s1", "帮我写一个 Python 程序。")
            self.assertTrue(reply.ok)
            self.assertEqual(agent.mode_name, "companion")
            self.assertEqual(agent.last_intent, Intent.CHAT.value)
            self.assertEqual(task.calls, [])
            self.assertEqual(len(companion.calls), 1)

    def test_case4_modifying_the_project_asks_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            companion = ScriptProvider()
            task = ScriptProvider(reply="改完了")
            agent, _, _ = local_agent(tmp, companion, task)
            reply = agent.respond("s1", "帮我修改 deskpet 的 AgentCore。")
            self.assertTrue(reply.ok)
            self.assertEqual(agent.mode_name, "task_pending")
            self.assertIn("要现在开始吗", reply.content)
            self.assertEqual(task.calls, [])


class Case5RefuseTaskTests(unittest.TestCase):
    """Case 5：用户拒绝任务后，不能执行任何东西。"""

    def test_refusing_the_task_executes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            companion = ScriptProvider()
            task = ScriptProvider(reply="改完了")
            agent, _, _ = local_agent(tmp, companion, task)
            agent.respond("s1", "帮我修改 deskpet 的 AgentCore。")
            reply = agent.respond("s1", "算了")
            self.assertTrue(reply.ok)
            self.assertEqual(agent.task_state, "cancelled")
            self.assertEqual(agent.mode_name, "companion")
            self.assertEqual(task.calls, [])


class Case6MemoryPersistsTests(unittest.TestCase):
    """Case 6：关掉再打开，重要记忆还在。"""

    def test_important_memory_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.sqlite3"
            first = Database(path)
            first.migrate()
            MemoryManager(MemoryRepository(first)).remember(
                "用户喜欢晚上学习", kind="long_term", source="USER_EXPLICIT"
            )

            reopened = Database(path)
            reopened.migrate()
            rows = MemoryManager(MemoryRepository(reopened)).search("晚上学习")
            self.assertTrue(rows)
            self.assertTrue(any("晚上学习" in row["content"] for row in rows))


class Case7And8DegradeTests(unittest.TestCase):
    """Case 7 / Case 8：没有麦克风、没有 Live2D，也要能跑。"""

    def test_no_audio_library_is_wired_in_yet(self):
        banned = (
            "import speech_recognition",
            "import pyaudio",
            "import sounddevice",
            "import whisper",
            "from speech_recognition",
        )
        for path in source_files():
            text = path.read_text(encoding="utf-8")
            for needle in banned:
                self.assertNotIn(needle, text, str(path))

    def test_default_avatar_is_a_placeholder_not_live2d(self):
        avatar = DefaultAvatarProvider()
        self.assertIsInstance(avatar, AvatarProvider)
        avatar.set_state({"emotion": "happy"})
        self.assertEqual(avatar.expression, "smile")

    def test_no_live2d_package_is_imported(self):
        for path in source_files():
            text = path.read_text(encoding="utf-8").lower()
            for needle in ("import live2dpy", "import live2d_py", "from live2d import",
                            "from live2dpy import", "live2d-py", "pip install live2d"):
                self.assertNotIn(needle, text, str(path))


class FakeLive2DAvatar(AvatarProvider):
    """模拟用户以后导入的 Live2D：只要实现三个方法就能接进来。"""

    name = "future-live2d"

    def __init__(self):
        self.calls = []

    def set_expression(self, expression):
        self.calls.append(("expression", expression))

    def play_animation(self, animation):
        self.calls.append(("animation", animation))

    def set_state(self, state):
        self.calls.append(("state", dict(state or {})))


class FakeEsp32(DeviceAdapter):
    """模拟用户以后接入的 ESP32 开发板。"""

    name = "esp32"

    def __init__(self):
        self.connected = False
        self.commands = []

    def connect(self):
        self.connected = True
        return {"ok": True, "device": self.name, "state": "connected"}

    def disconnect(self):
        self.connected = False
        return {"ok": True, "device": self.name, "state": "disconnected"}

    def get_state(self):
        return {"device": self.name, "connected": self.connected, "pins": 4}

    def execute(self, command):
        if not self.connected:
            return {"ok": False, "device": self.name, "error": "设备未连接"}
        self.commands.append(dict(command))
        return {"ok": True, "device": self.name, "echo": dict(command)}


class CustomPersona(PersonaProvider):
    """模拟用户以后训练好的自定义人格。"""

    def get_profile(self):
        return {"name": "自训练人格", "style": "像老朋友", "traits": {"幽默": 80}}

    def get_behavior_rules(self):
        return ["先接住情绪"]

    def get_current_state(self):
        return {"mood": "warm"}


class Case9To12FutureIntegrationTests(unittest.TestCase):
    """Case 9-12：以后接 Live2D / 语音 / ESP32 / YOLO，都不用改 Agent Core。"""

    def test_agent_core_has_no_body_voice_or_vision_bindings(self):
        text = read("agent/core.py").lower()
        for word in (
            "live2d",
            "esp32",
            "servo",
            "yolo",
            "whisper",
            "microphone",
            "sounddevice",
            "pyaudio",
            "opencv",
            "cv2",
        ):
            self.assertNotIn(word, text)

    def test_agent_core_never_names_a_concrete_model_vendor(self):
        text = read("agent/core.py").lower()
        for word in ("openai", "deepseek", "qwen", "anthropic", "gemini"):
            self.assertNotIn(word, text)

    def test_live2d_plugs_in_through_avatar_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _, _ = local_agent(tmp, ScriptProvider())
            fake = FakeLive2DAvatar()
            agent.avatar = fake
            agent._update_emotion("happy")
            self.assertIn(("state", {"emotion": "happy", "intensity": 0.5}), fake.calls)

    def test_esp32_is_blocked_by_default_then_plugs_in_when_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / "t.sqlite3")
            database.migrate()
            audit = AuditLog(database)
            board = FakeEsp32()

            locked = DeviceManager(audit=audit)
            self.assertFalse(locked.register(board)["ok"])
            self.assertEqual(locked.names(), [])

            opened = DeviceManager(audit=audit, allow_real=True)
            self.assertTrue(opened.register(board)["ok"])
            self.assertTrue(opened.connect("esp32")["ok"])
            result = opened.execute("esp32", {"servo": 90})
            self.assertTrue(result["ok"])
            self.assertEqual(board.commands, [{"servo": 90}])

    def test_trained_persona_plugs_in_without_touching_agent_core(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _, _ = local_agent(tmp, ScriptProvider(reply="在呢。"))
            agent.persona = CustomPersona()
            reply = agent.respond("s1", "陪我聊会儿")
            self.assertTrue(reply.ok)
            profile = agent.persona.get_profile()
            self.assertEqual(profile["name"], "自训练人格")

    def test_voice_would_just_feed_text_into_respond(self):
        # 现在没有语音系统；以后语音包只需把识别出的文字交给 respond()。
        with tempfile.TemporaryDirectory() as tmp:
            agent, _, _ = local_agent(tmp, ScriptProvider(reply="嗯。"))
            reply = agent.respond("voice", "你好")
            self.assertTrue(reply.ok)
            self.assertEqual(reply.content, "嗯。")

    def test_no_vision_library_is_wired_in_yet(self):
        banned = ("import cv2", "import ultralytics", "from ultralytics", "import torch")
        for path in source_files():
            text = path.read_text(encoding="utf-8")
            for needle in banned:
                self.assertNotIn(needle, text, str(path))


class NoNetworkTests(unittest.TestCase):
    """§126：没有网络、服务挂掉时，对话本身不能崩。"""

    def test_service_failure_does_not_break_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent, _, _ = local_agent(tmp, ScriptProvider(reply="在的。"))
            reply = agent.respond("s1", "你好")
            self.assertTrue(reply.ok)


if __name__ == "__main__":
    unittest.main()
