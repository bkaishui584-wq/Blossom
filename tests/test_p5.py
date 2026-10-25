"""P5 验收：界面能构建、情绪能显示、聊天能走通、设置读写偏好、密钥不外泄。"""

import sys
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.context import ContextManager
from agent.core import AgentCore
from avatar.interface import EMOTION_EXPRESSIONS
from database.db import Database
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatDelta, ChatResult, ModelProvider, Usage
from persona.interface import DefaultPersonaProvider
from persona.state import EMOTIONS
from security.config import Config
from ui.desktop import (
    SKIN_COLORS,
    Confirmer,
    DesktopPet,
    PetCanvas,
    TkAvatarProvider,
    emotion_label,
    format_debug,
    format_status,
)


class StubProvider(ModelProvider):
    name = "stub"

    def __init__(self, reply="在的。", fail=False):
        self.reply = reply
        self.fail = fail

    def is_available(self):
        return True

    def chat(self, messages, tools=None, temperature=0.7):
        if self.fail:
            return ChatResult(ok=False, error="模型不可用", model="stub")
        return ChatResult(
            ok=True,
            content=self.reply,
            usage=Usage(prompt_tokens=5, completion_tokens=3),
            model="stub",
        )

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


class HelperTests(unittest.TestCase):
    def test_status_line_shows_emotion_and_model(self):
        text = format_status("happy", {"total_tokens": 42}, None, "test-model")
        self.assertIn("开心", text)
        self.assertIn("test-model", text)
        self.assertIn("42", text)

    def test_status_shows_unconfigured_model(self):
        self.assertIn("未配置", format_status("neutral", None, None, ""))

    def test_unknown_emotion_falls_back(self):
        self.assertEqual(emotion_label(None), "未知")
        self.assertEqual(emotion_label("weird"), "weird")

    def test_debug_text_lists_key_fields(self):
        text = format_debug({"model": "m", "emotion": "happy", "estimated_tokens": 12})
        self.assertIn("emotion: happy", text)
        self.assertIn("estimated_tokens: 12", text)

    def test_debug_text_is_empty_when_nothing_to_show(self):
        self.assertEqual(format_debug({}), "")

    def test_debug_text_shows_token_usage(self):
        text = format_debug(
            {"model": "m", "prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42}
        )
        self.assertIn("prompt_tokens: 30", text)
        self.assertIn("completion_tokens: 12", text)
        self.assertIn("total_tokens: 42", text)

class ConfirmerTests(unittest.TestCase):
    def test_confirmer_denies_before_ui_exists(self):
        self.assertFalse(Confirmer()("rm", "CRITICAL", "危险"))


class TkTestCase(unittest.TestCase):
    """需要显示环境。没有显示时自动跳过。"""

    def setUp(self):
        try:
            self.root = tk.Tk()
        except Exception as exc:
            self.skipTest(f"tkinter 不可用：{exc}")
        self.root.withdraw()
        self.addCleanup(self._safe_destroy)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _safe_destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def build_pet(self, provider=None, config=None, memory=None):
        database = Database(Path(self.tmp.name) / "t.sqlite3")
        database.migrate()
        memory = memory or MemoryManager(MemoryRepository(database))
        agent = AgentCore(
            provider or StubProvider(),
            memory,
            ContextManager(token_budget=600),
            persona=DefaultPersonaProvider(),
        )
        pet = DesktopPet(agent, config or Config(load_env=False), memory=memory, root=self.root)
        return pet, agent, memory


class UISmokeTests(TkTestCase):
    def test_window_and_widgets_are_created(self):
        pet, _, _ = self.build_pet()
        self.assertTrue(pet.pet.winfo_exists())
        self.assertTrue(pet.transcript.winfo_exists())
        self.assertTrue(pet.input.winfo_exists())
        self.assertTrue(self.root.overrideredirect())

    def test_avatar_replaces_agent_provider_and_draws_emotion(self):
        pet, agent, _ = self.build_pet()
        self.assertIsInstance(agent.avatar, TkAvatarProvider)
        agent.avatar.set_state({"emotion": "excited", "intensity": 0.8})
        pet._drain()
        self.assertEqual(pet.pet.expression, "excited")

    def test_reply_is_shown_in_transcript_and_status(self):
        pet, agent, _ = self.build_pet()
        pet._apply_reply(agent.respond("desktop", "在吗"))
        self.assertIn("在的。", pet.transcript.get("1.0", "end"))
        # token 不再常驻状态行，挪进详情里了。
        self.assertIn("tokens", pet._status_full_text())

    def test_send_box_goes_through_agent(self):
        """按发送/回车之后的完整链路：界面 → 工作线程 → Agent → 回显。"""
        pet, _, _ = self.build_pet()
        pet.input.insert(0, "在吗")
        pet._send()
        deadline = time.time() + 5
        while "在的。" not in pet.transcript.get("1.0", "end") and time.time() < deadline:
            pet._drain()
            self.root.update()
            time.sleep(0.02)
        text = pet.transcript.get("1.0", "end")
        self.assertIn("在吗", text)
        self.assertIn("在的。", text)
        self.assertEqual(pet.input.get(), "")

    def test_model_error_is_shown_and_does_not_crash(self):
        pet, agent, _ = self.build_pet(StubProvider(fail=True))
        pet._apply_reply(agent.respond("desktop", "在吗"))
        self.assertIn("模型不可用", pet.transcript.get("1.0", "end"))

    def test_settings_rows_never_expose_api_key(self):
        config = Config(load_env=False)
        config.llm_api_key = "sk-should-never-be-shown-123456"
        pet, _, _ = self.build_pet(config=config)
        rows = dict(pet.settings_rows())
        self.assertEqual(rows["API Key"], "已设置")
        self.assertNotIn("sk-should-never-be-shown-123456", str(rows))

    def test_settings_save_preferences(self):
        pet, _, memory = self.build_pet()
        pet.proactive_var = tk.BooleanVar(value=False)
        pet.settings_hint = tk.Label(self.root, text="")
        pet._save_proactive()
        self.assertEqual(memory.get_preference("proactive_enabled"), "0")
        pet.quiet_entry = tk.Entry(self.root)
        pet.quiet_entry.insert(0, "22-8")
        pet._save_quiet_hours()
        self.assertEqual(memory.get_preference("quiet_hours"), "22-8")

    def test_settings_survive_memory_failure(self):
        class BrokenMemory:
            def set_preference(self, key, value):
                raise RuntimeError("记忆不可用")

        pet, _, _ = self.build_pet()
        pet.memory = BrokenMemory()
        pet.settings_hint = tk.Label(self.root, text="")
        pet.proactive_var = tk.BooleanVar(value=True)
        pet._save_proactive()
        self.assertIn("保存失败", pet.settings_hint.cget("text"))

    def test_dev_panel_only_in_dev_mode(self):
        pet, _, _ = self.build_pet()
        self.assertIsNone(pet.dev)

    def test_dev_panel_present_in_dev_mode(self):
        config = Config(load_env=False)
        config.dev_mode = True
        pet, _, _ = self.build_pet(config=config)
        self.assertIsNotNone(pet.dev)
        pet._set_dev({"model": "m", "emotion": "happy"})
        self.assertIn("emotion", pet.dev.get("1.0", "end"))

    def test_unconfigured_model_shows_hint(self):
        pet, _, _ = self.build_pet()
        self.assertIn("尚未配置模型", pet.transcript.get("1.0", "end"))

    def test_close_stops_scheduler(self):
        class Scheduler:
            def __init__(self):
                self.stopped = False

            def stop(self):
                self.stopped = True

        scheduler = Scheduler()
        pet = DesktopPet(
            AgentCore(StubProvider(), MemoryManager(MemoryRepository(Database(Path(self.tmp.name) / "s.sqlite3"))), ContextManager()),
            Config(load_env=False),
            scheduler=scheduler,
            root=self.root,
        )
        pet.close()
        self.assertTrue(scheduler.stopped)


class PetCanvasTests(TkTestCase):
    def test_every_emotion_renders(self):
        canvas = PetCanvas(self.root)
        for emotion in EMOTIONS:
            expression = EMOTION_EXPRESSIONS[emotion]
            canvas.render(expression)
            self.assertEqual(canvas.expression, expression)
        for expression in EMOTION_EXPRESSIONS.values():
            self.assertIn(expression, SKIN_COLORS)

    def test_unknown_expression_falls_back_to_normal(self):
        canvas = PetCanvas(self.root)
        canvas.render("no-such-expression")
        self.assertEqual(canvas.expression, "normal")


if __name__ == "__main__":
    unittest.main()
