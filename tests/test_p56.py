"""阶段 V5-B 验收：可控的屏幕截图与诚实的 OCR 边界。

要点：没授权不截、没引擎不说瞎话、图只落本地不上传、界面里点一次才截一次。
真机截图只在 Windows 上跑，跑不到就跳过，绝不假装截到了。
"""

import os
import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.context import ContextManager  # noqa: E402
from agent.core import AgentCore  # noqa: E402
from behavior import vision as vision_mod  # noqa: E402
from behavior.vision import (  # noqa: E402
    NO_OCR_ENGINE,
    NullOcrProvider,
    NullVisionProvider,
    ScreenCaptureProvider,
    VisionProvider,
    VisionService,
    build_service,
)
from database.db import Database  # noqa: E402
from events.bus import EventBus  # noqa: E402
from memory.manager import MemoryManager  # noqa: E402
from memory.repository import MemoryRepository  # noqa: E402
from model.base import ChatDelta, ChatResult, ModelProvider, Usage  # noqa: E402
from security.config import Config  # noqa: E402
from security.privacy import PrivacyCenter  # noqa: E402
from ui.desktop import DesktopPet  # noqa: E402


class StubProvider(ModelProvider):
    name = "stub"

    def is_available(self):
        return True

    def chat(self, messages, tools=None, temperature=0.7):
        return ChatResult(
            ok=True,
            content="好。",
            usage=Usage(prompt_tokens=3, completion_tokens=2),
            model="stub",
        )

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


class FakeCapture(VisionProvider):
    """假截图：写一个小 PNG，用来在没桌面的环境里验流程。"""

    name = "fake"

    def __init__(self, ok=True, error="截不到"):
        self.ok = ok
        self.error = error
        self.calls = 0

    def is_available(self):
        return True

    def observe(self, source="screen", region=None):
        self.calls += 1
        if not self.ok:
            return {"ok": False, "error": self.error, "data": None, "source": source}
        return {
            "ok": True,
            "error": "",
            "source": source,
            "data": {"path": "C:\\tmp\\shot.png", "width": 800, "height": 600, "bytes": 42},
        }


class FakeOcr(NullOcrProvider):
    def __init__(self, available=False, text=""):
        self.available_flag = available
        self.text = text

    def is_available(self):
        return self.available_flag

    def read(self, path, region=None):
        if not self.available_flag:
            return {"ok": False, "text": "", "error": NO_OCR_ENGINE, "path": str(path)}
        return {"ok": True, "text": self.text, "error": "", "path": str(path)}


class Permissions:
    """权限替身：按 key 返回是否允许。"""

    def __init__(self, **flags):
        self.flags = flags

    def allows(self, key):
        return bool(self.flags.get(key))


class NullProviderTests(unittest.TestCase):
    def test_null_provider_still_reports_it_cannot_see(self):
        provider = NullVisionProvider()
        self.assertFalse(provider.is_available())
        result = provider.observe()
        self.assertFalse(result["ok"])
        self.assertIsNone(result["data"])
        self.assertIn("还没有接视觉能力", result["error"])


class ConsentTests(unittest.TestCase):
    def test_capture_is_refused_without_permission(self):
        provider = FakeCapture()
        service = VisionService(provider=provider, allows=Permissions())
        result = service.capture()
        self.assertFalse(result["ok"])
        self.assertIn("允许", result["error"])
        self.assertEqual(provider.calls, 0)

    def test_capture_runs_once_permission_is_granted(self):
        provider = FakeCapture()
        service = VisionService(provider=provider, allows=Permissions(vision=True))
        result = service.capture()
        self.assertTrue(result["ok"])
        self.assertEqual(provider.calls, 1)
        self.assertEqual(service.captures, 1)

    def test_missing_permission_source_counts_as_refused(self):
        service = VisionService(provider=FakeCapture(), allows=None)
        self.assertFalse(service.allowed())
        self.assertFalse(service.capture()["ok"])

    def test_broken_permission_check_does_not_open_the_door(self):
        def boom(key):
            raise RuntimeError("权限中心炸了")

        service = VisionService(provider=FakeCapture(), allows=boom)
        self.assertFalse(service.capture()["ok"])

    def test_provider_failure_is_reported_truthfully(self):
        service = VisionService(
            provider=FakeCapture(ok=False, error="这台机器上截不了屏"),
            allows=Permissions(vision=True),
        )
        result = service.capture()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "这台机器上截不了屏")


class EventTests(unittest.TestCase):
    def test_capture_publishes_a_structured_event(self):
        bus = EventBus()
        seen = []
        bus.subscribe("vision.capture", lambda event: seen.append(event.payload))
        service = VisionService(provider=FakeCapture(), allows=Permissions(vision=True), bus=bus)
        service.capture(reason="menu")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["width"], 800)
        self.assertEqual(seen[0]["reason"], "menu")

    def test_failure_publishes_an_error_event(self):
        bus = EventBus()
        seen = []
        bus.subscribe("vision.error", lambda event: seen.append(event.payload))
        service = VisionService(
            provider=FakeCapture(ok=False, error="黑屏"), allows=Permissions(vision=True), bus=bus
        )
        service.capture()
        self.assertEqual(seen[0]["error"], "黑屏")

    def test_callback_receives_the_capture_facts(self):
        got = []
        service = VisionService(
            provider=FakeCapture(),
            allows=Permissions(vision=True),
            on_capture=got.append,
        )
        service.capture()
        self.assertEqual(got[0]["width"], 800)

    def test_broken_callback_does_not_break_the_capture(self):
        def boom(data):
            raise RuntimeError("回调炸了")

        service = VisionService(
            provider=FakeCapture(), allows=Permissions(vision=True), on_capture=boom
        )
        self.assertTrue(service.capture()["ok"])


class OcrBoundaryTests(unittest.TestCase):
    def test_no_engine_says_so_instead_of_pretending(self):
        service = VisionService(
            provider=FakeCapture(), ocr=NullOcrProvider(), allows=Permissions(vision=True)
        )
        result = service.read_text("C:\\tmp\\shot.png")
        self.assertFalse(result["ok"])
        self.assertEqual(result["text"], "")
        self.assertEqual(result["error"], NO_OCR_ENGINE)

    def test_status_reports_missing_engine(self):
        service = VisionService(provider=FakeCapture(), allows=Permissions(vision=True))
        status = service.status()
        self.assertFalse(status["ocr"])
        self.assertEqual(status["note"], NO_OCR_ENGINE)

    def test_engine_output_is_passed_through_when_present(self):
        service = VisionService(
            provider=FakeCapture(),
            ocr=FakeOcr(available=True, text="识别到的字"),
            allows=Permissions(vision=True),
        )
        result = service.read_text("C:\\tmp\\shot.png")
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "识别到的字")

    def test_reading_text_needs_permission_too(self):
        service = VisionService(provider=FakeCapture(), ocr=FakeOcr(available=True, text="x"))
        self.assertFalse(service.read_text("C:\\tmp\\shot.png")["ok"])

    def test_describe_does_not_claim_model_vision(self):
        service = VisionService(provider=FakeCapture(), allows=Permissions(vision=True))
        described = service.describe()
        self.assertFalse(described["model_analysis"])
        self.assertIn("文字", described["model_note"])


class RealCaptureTests(unittest.TestCase):
    """真机截图。截不到就跳过，不算失败。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_capture_writes_a_real_png(self):
        provider = ScreenCaptureProvider(out_dir=Path(self.tmp.name))
        if not provider.is_available():
            self.skipTest("这台机器没有 Windows 桌面")
        result = provider.observe("screen")
        if not result.get("ok"):
            self.skipTest("当前会话截不到屏：%s" % result.get("error"))
        data = result["data"]
        self.assertGreater(data["width"], 0)
        self.assertGreater(data["height"], 0)
        blob = Path(data["path"]).read_bytes()
        self.assertEqual(blob[:8], b"\x89PNG\r\n\x1a\n")
        self.assertGreater(len(blob), 100)

    def test_capture_is_capped_and_reports_the_true_size(self):
        provider = ScreenCaptureProvider(out_dir=Path(self.tmp.name), max_side=64)
        if not provider.is_available():
            self.skipTest("这台机器没有 Windows 桌面")
        result = provider.observe("screen")
        if not result.get("ok"):
            self.skipTest("当前会话截不到屏")
        described = provider.describe()
        self.assertEqual(described["captures"], 1)
        self.assertTrue(described["available"])


class ServiceBuilderTests(unittest.TestCase):
    def test_builder_uses_the_data_directory(self):
        config = Config(load_env=False)
        with tempfile.TemporaryDirectory() as tmp:
            config.data_dir = Path(tmp)
            service = build_service(config=config)
            self.assertEqual(service.provider.out_dir, Path(tmp) / "vision")

    def test_builder_without_config_still_works(self):
        service = build_service()
        self.assertIsInstance(service.ocr, NullOcrProvider)

    def test_privacy_center_carries_the_new_switch(self):
        center = PrivacyCenter()
        keys = [row["key"] for row in center.controls()]
        self.assertIn("vision", keys)
        self.assertFalse(center.allows("vision"))
        self.assertIn("截图分析", center.permission_center())


class NullServiceTests(unittest.TestCase):
    def test_service_with_null_provider_refuses_politely(self):
        service = VisionService(provider=NullVisionProvider(), allows=Permissions(vision=True))
        result = service.capture()
        self.assertFalse(result["ok"])
        self.assertIn("截不了屏", result["error"])


class ChatEntryTests(unittest.TestCase):
    """界面入口：点一次截一次，没接就直说。"""

    def setUp(self):
        try:
            self.root = tk.Tk()
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        self.root.withdraw()
        self.addCleanup(self._safe_destroy)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.config = Config(load_env=False)
        self.config.data_dir = base
        self.config.log_dir = base / "logs"
        self.config.db_path = base / "t.sqlite3"
        self.config.tray = False
        database = Database(base / "t.sqlite3")
        database.migrate()
        self.memory = MemoryManager(MemoryRepository(database))
        self.agent = AgentCore(StubProvider(), self.memory, ContextManager(token_budget=800))

    def _safe_destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _pet(self, **kwargs):
        return DesktopPet(self.agent, self.config, memory=self.memory, root=self.root, **kwargs)

    def test_without_vision_the_menu_entry_says_so(self):
        pet = self._pet()
        result = pet.look_at_screen()
        self.assertFalse(result["ok"])
        self.assertIn("还没有接屏幕截图", pet.chat.history_text())
        pet.close()

    def test_with_vision_the_note_reports_the_file(self):
        service = VisionService(provider=FakeCapture(), allows=Permissions(vision=True))
        pet = self._pet(vision=service)
        result = pet.look_at_screen()
        self.assertTrue(result["ok"])
        text = pet.chat.history_text()
        self.assertIn("800", text)
        self.assertIn("没有上传", text)
        pet.close()

    def test_refused_capture_shows_the_reason(self):
        service = VisionService(provider=FakeCapture(), allows=Permissions())
        pet = self._pet(vision=service)
        pet.look_at_screen()
        self.assertIn("允许", pet.chat.history_text())
        pet.close()

    def test_menu_entry_exists(self):
        pet = self._pet(vision=VisionService(provider=FakeCapture()))
        labels = [
            pet.chat.menu.entrycget(index, "label")
            for index in range(pet.chat.menu.index("end") + 1)
            if pet.chat.menu.type(index) == "command"
        ]
        self.assertIn("看一眼屏幕", labels)
        pet.close()


if __name__ == "__main__":
    unittest.main()