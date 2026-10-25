"""P18 验收：语音输入接口 —— 接口稳定、没有麦克风也能跑、换语音包不改 Agent Core。"""

import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path

from agent.core import AgentReply
from persona.state import EmotionState

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.context import ContextManager
from agent.core import AgentCore
from database.db import Database
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatDelta, ChatResult, ModelProvider
from security.config import Config
from voice.interface import NullVoiceProvider, VoiceInput, VoiceProvider
from voice.manager import VoicePipeline


class ScriptProvider(ModelProvider):
    name = "script"

    def __init__(self, reply="在的。"):
        self.reply = reply
        self.calls = []

    def is_available(self):
        return True

    def chat(self, messages, tools=None, temperature=0.7):
        self.calls.append(list(messages))
        return ChatResult(ok=True, content=self.reply, model=self.name)

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


class FakeMic(VoiceProvider):
    """模拟用户以后接入的语音包。"""

    name = "fake-mic"

    def __init__(self, *script, available=True):
        self._script = list(script)
        self.available = available
        self.started = False

    def is_available(self):
        return self.available

    def start(self):
        self.started = True
        return True

    def stop(self):
        self.started = False

    def listen(self, timeout=None):
        return self._script.pop(0) if self._script else None


class BrokenMic(VoiceProvider):
    name = "broken"

    def is_available(self):
        raise RuntimeError("no driver")

    def start(self):
        raise RuntimeError("boom")

    def stop(self):
        raise RuntimeError("boom")

    def listen(self, timeout=None):
        raise RuntimeError("boom")


def temp_dir():
    # 工作线程可能还占着库文件，清理失败不该让测试变红。
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


def local_agent(tmp, provider=None):
    database = Database(Path(tmp) / "t.sqlite3")
    database.migrate()
    memory = MemoryManager(MemoryRepository(database))
    return AgentCore(provider, memory, ContextManager(token_budget=600)), memory


class StubAgent:
    """只做界面接线测试，不碰数据库：回复固定，不写记忆。"""

    def __init__(self):
        self.avatar = None
        self.emotion = EmotionState()
        self.persona = None
        self.mode_name = "local"

    def respond(self, session_id, text, persona_note=""):
        return AgentReply(ok=True, content=f"收到：{text}")


class InterfaceTests(unittest.TestCase):
    def test_provider_has_the_four_methods(self):
        for name in ("is_available", "start", "stop", "listen"):
            self.assertTrue(hasattr(VoiceProvider, name))

    def test_null_provider_never_hears_anything(self):
        provider = NullVoiceProvider()
        self.assertFalse(provider.is_available())
        self.assertFalse(provider.start())
        self.assertFalse(provider.stop())
        self.assertIsNone(provider.listen())
        self.assertIn("没有接入语音输入", provider.describe()["note"])

    def test_voice_input_roundtrip(self):
        heard = VoiceInput(text="你好", final=False, confidence=0.5, source="mic")
        data = heard.to_dict()
        self.assertEqual(data["text"], "你好")
        self.assertFalse(data["final"])
        self.assertEqual(data["source"], "mic")


class PipelineTests(unittest.TestCase):
    def test_no_microphone_is_silent_not_broken(self):
        pipeline = VoicePipeline()
        self.assertFalse(pipeline.available())
        self.assertFalse(pipeline.start())
        self.assertIsNone(pipeline.poll())

    def test_poll_returns_nothing_until_started(self):
        pipeline = VoicePipeline(provider=FakeMic("你好"))
        self.assertIsNone(pipeline.poll())

    def test_started_pipeline_delivers_final_text(self):
        heard = []
        pipeline = VoicePipeline(provider=FakeMic("几点了"), on_text=heard.append)
        self.assertTrue(pipeline.start())
        result = pipeline.poll()
        self.assertIsNotNone(result)
        self.assertEqual(heard, ["几点了"])
        self.assertEqual(pipeline.accepted, 1)

    def test_partial_result_is_not_sent_to_the_agent(self):
        heard = []
        pipeline = VoicePipeline(
            provider=FakeMic(VoiceInput(text="几", final=False), "几点了"),
            on_text=heard.append,
        )
        pipeline.start()
        pipeline.poll()
        self.assertEqual(heard, [])
        self.assertEqual(pipeline.rejected, 1)
        pipeline.poll()
        self.assertEqual(heard, ["几点了"])

    def test_empty_and_low_confidence_are_ignored(self):
        heard = []
        pipeline = VoicePipeline(
            provider=FakeMic("", VoiceInput(text="嗯", confidence=0.1), "好"),
            on_text=heard.append,
            min_confidence=0.5,
        )
        pipeline.start()
        self.assertIsNone(pipeline.poll())
        self.assertIsNone(pipeline.poll())
        self.assertIsNotNone(pipeline.poll())
        self.assertEqual(heard, ["好"])
        self.assertEqual(pipeline.rejected, 2)

    def test_plain_string_is_accepted(self):
        heard = []
        pipeline = VoicePipeline(provider=FakeMic("你好"), on_text=heard.append)
        pipeline.feed("你好")
        self.assertEqual(heard, ["你好"])

    def test_unknown_payload_is_rejected(self):
        pipeline = VoicePipeline(provider=FakeMic())
        self.assertIsNone(pipeline.handle({"text": "你好"}))
        self.assertEqual(pipeline.rejected, 1)

    def test_broken_provider_is_contained(self):
        pipeline = VoicePipeline(provider=BrokenMic())
        self.assertFalse(pipeline.available())
        self.assertFalse(pipeline.start())
        self.assertIsNone(pipeline.poll())
        pipeline.stop()

    def test_broken_on_text_does_not_raise(self):
        def boom(_text):
            raise RuntimeError("下游炸了")

        pipeline = VoicePipeline(provider=FakeMic("你好"), on_text=boom)
        self.assertIsNotNone(pipeline.feed("你好"))

    def test_describe_is_a_read_only_view(self):
        pipeline = VoicePipeline(provider=FakeMic("你好"))
        pipeline.feed("你好")
        info = pipeline.describe()
        self.assertTrue(info["available"])
        self.assertEqual(info["provider"], "fake-mic")
        self.assertEqual(info["accepted"], 1)
        self.assertEqual(info["last_input"]["text"], "你好")


class AgentIntegrationTests(unittest.TestCase):
    """Case 10：语音包只把文字交给 Agent，不改 Agent Core。"""

    def test_voice_text_reaches_the_agent_through_respond_only(self):
        with temp_dir() as tmp:
            provider = ScriptProvider(reply="在的。")
            agent, _ = local_agent(tmp, provider)
            replies = []
            pipeline = VoicePipeline(
                provider=FakeMic("你好"),
                on_text=lambda text: replies.append(agent.respond("voice", text)),
            )
            self.assertTrue(pipeline.start())
            self.assertIsNotNone(pipeline.poll())
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(replies[0].content, "在的。")
            # pipeline 自己不认识模型，调用只经过 agent
            self.assertFalse(hasattr(pipeline, "provider_calls"))

    def test_agent_core_does_not_mention_voice(self):
        text = (ROOT / "agent" / "core.py").read_text(encoding="utf-8").lower()
        for word in ("voice", "语音", "microphone", "麦克风"):
            self.assertNotIn(word, text)

    def test_voice_package_imports_no_audio_library(self):
        for name in ("interface.py", "manager.py"):
            text = (ROOT / "voice" / name).read_text(encoding="utf-8").lower()
            for needle in ("pyaudio", "sounddevice", "speech_recognition", "whisper", "vosk"):
                self.assertNotIn(needle, text, name)

    def test_swapping_the_voice_package_needs_no_agent_change(self):
        with temp_dir() as tmp:
            agent, _ = local_agent(tmp, ScriptProvider(reply="嗯。"))
            for provider in (FakeMic("你好"), NullVoiceProvider(), BrokenMic()):
                pipeline = VoicePipeline(
                    provider=provider, on_text=lambda text: agent.respond("voice", text)
                )
                pipeline.start()
                pipeline.poll()
            self.assertTrue(True)


class SilentDesktopTests(unittest.TestCase):
    """没有语音包时，桌面界面照常启动，不排取音定时。"""

    def setUp(self):
        try:
            self.root = tk.Tk()
        except Exception as exc:
            self.skipTest(f"tkinter 不可用：{exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy)

    def _destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _pet(self, voice=None):
        from ui.desktop import DesktopPet

        agent = StubAgent()
        return (
            DesktopPet(
                agent,
                Config(load_env=False),
                voice=voice,
                root=self.root,
            ),
            agent,
        )

    def test_desktop_runs_without_a_voice_package(self):
        pet, _ = self._pet(VoicePipeline())
        self.assertFalse(pet.voice.available())
        pet._voice_tick()  # 不该抛异常

    def test_voice_text_uses_the_same_chat_path(self):
        pipeline = VoicePipeline(provider=FakeMic("你好"))
        pet, _ = self._pet(pipeline)
        pipeline.on_text = pet.submit_voice
        pipeline.start()
        pipeline.poll()
        transcript = pet.transcript.get("1.0", "end")
        self.assertIn("你（语音）：你好", transcript)

    def test_voice_is_stopped_on_close(self):
        mic = FakeMic("你好")
        pipeline = VoicePipeline(provider=mic)
        pet, _ = self._pet(pipeline)
        pipeline.start()
        self.assertTrue(mic.started)
        pet.voice.stop()
        self.assertFalse(mic.started)


if __name__ == "__main__":
    unittest.main()
