"""P3 验收：屏幕感知与工作上下文。

覆盖：默认关闭、要靠隐私开关才真的开、只读应用名与标题、分类只当线索、
聊天这类页面不往下读内容、排除名单立刻生效、读到的内容能一键清掉、
没有语义引擎时如实说读不到、状态与事件都对得上、感知页能列出来。
"""

import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.context import ContextManager
from agent.core import AgentCore
from behavior import awareness as aw
from database.db import Database
from events.bus import EventBus
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatDelta, ChatResult, ModelProvider, Usage
from security.config import Config
from security.privacy import PrivacyCenter
from ui import theme
from ui.desktop import DesktopPet


class StubProvider(ModelProvider):
    name = "stub"

    def is_available(self):
        return True

    def chat(self, messages, tools=None, temperature=0.7):
        return ChatResult(ok=True, content="在的。", usage=Usage(5, 3), model="stub")

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


class FakeActivity(aw.ActivityProvider):
    """替身：想让它报什么就报什么。"""

    name = "fake"

    def __init__(self, app="code.exe", title="deskpet - Visual Studio Code"):
        self.app = app
        self.title = title
        self.calls = 0

    def is_available(self):
        return True

    def current(self):
        self.calls += 1
        return {"app": self.app, "title": self.title, "pid": 4242}


class CountingSemantic(aw.SemanticProvider):
    """替身：只有真被调用才算数。"""

    name = "counting"

    def __init__(self, payload=None):
        self.payload = payload or {"page": "编辑器", "controls": [{"role": "Edit", "name": "main.py"}]}
        self.calls = 0

    def is_available(self):
        return True

    def read(self, limit=8):
        self.calls += 1
        return dict(self.payload)


class ClassifyTests(unittest.TestCase):
    def test_known_processes_are_recognized(self):
        self.assertEqual(aw.classify("code.exe", "")[0], "ide")
        self.assertEqual(aw.classify("WeChat.exe", "")[0], "chat")
        self.assertEqual(aw.classify("chrome.exe", "")[0], "browser")
        self.assertEqual(aw.classify("powershell.exe", "管理员")[0], "terminal")

    def test_a_title_can_carry_the_hint(self):
        self.assertEqual(aw.classify("unknown.exe", "报告.docx - 写字板")[0], "document")

    def test_something_unknown_is_admitted(self):
        category, confidence = aw.classify("mystery.exe", "")
        self.assertEqual(category, "")
        self.assertEqual(confidence, "unknown")

    def test_labels_have_a_fallback(self):
        self.assertEqual(aw.category_label("nope"), "未分类")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.config = Config(load_env=False)
        self.config.data_dir = base
        self.config.log_dir = base / "logs"
        self.config.db_path = base / "t.sqlite3"
        self.database = Database(self.config.db_path)
        self.database.migrate()
        self.memory = MemoryManager(MemoryRepository(self.database))
        self.privacy = PrivacyCenter(preferences=self.memory, config=self.config)
        self.bus = EventBus()
        self.activity = FakeActivity()
        self.semantic = CountingSemantic()
        self.service = aw.AwarenessService(
            preferences=self.memory,
            privacy=self.privacy,
            bus=self.bus,
            activity=self.activity,
            semantic=self.semantic,
        )

    def turn_on(self, level="metadata"):
        self.privacy.set("awareness", True)
        return self.service.set_level(level)

    def test_it_is_off_by_default(self):
        self.assertEqual(self.service.level(), "off")
        self.assertFalse(self.service.active())
        self.assertFalse(self.service.observe()["ok"])

    def test_a_level_alone_is_not_enough(self):
        self.service.set_level("metadata")
        self.assertFalse(self.service.active())
        self.assertIn("没开", self.service.observe()["note"])

    def test_the_privacy_switch_is_the_master_gate(self):
        self.turn_on()
        self.assertTrue(self.service.active())
        self.privacy.set("awareness", False)
        self.assertFalse(self.service.active())
        self.assertFalse(self.service.observe()["ok"])

    def test_it_reports_the_app_it_saw(self):
        self.turn_on()
        snapshot = self.service.observe()
        self.assertTrue(snapshot["ok"])
        self.assertEqual(snapshot["app"], "code.exe")
        self.assertEqual(snapshot["category"], "ide")
        self.assertEqual(snapshot["confidence_label"], "已确认")

    def test_a_chat_page_is_not_read_further(self):
        self.activity.app = "WeChat.exe"
        self.turn_on("semantic")
        snapshot = self.service.observe(force=True)
        self.assertTrue(snapshot["sensitive"])
        self.assertIsNone(snapshot["semantic"])
        self.assertEqual(self.semantic.calls, 0)

    def test_semantics_are_only_read_at_that_level(self):
        self.turn_on("metadata")
        self.service.observe(force=True)
        self.assertEqual(self.semantic.calls, 0)
        self.service.set_level("semantic")
        snapshot = self.service.observe(force=True)
        self.assertEqual(self.semantic.calls, 1)
        self.assertEqual(snapshot["semantic"]["page"], "编辑器")

    def test_a_missing_semantic_engine_is_admitted(self):
        service = aw.AwarenessService(
            preferences=self.memory,
            privacy=self.privacy,
            activity=self.activity,
            semantic=aw.NullSemanticProvider("没装 UI Automation"),
        )
        self.privacy.set("awareness", True)
        service.set_level("semantic")
        snapshot = service.observe(force=True)
        self.assertIsNone(snapshot["semantic"])
        self.assertIn("没装 UI Automation", snapshot["note"])

    def test_an_excluded_app_is_skipped(self):
        self.turn_on()
        self.service.exclude("code.exe")
        snapshot = self.service.observe(force=True)
        self.assertFalse(snapshot["ok"])
        self.assertIn("排除", snapshot["note"])

    def test_the_same_app_is_only_read_once_per_window(self):
        self.turn_on()
        self.service.observe()
        first = self.activity.calls
        self.service.observe()
        self.assertEqual(self.activity.calls, first)

    def test_a_new_app_is_read_again(self):
        self.turn_on()
        self.service.observe()
        self.activity.app = "chrome.exe"
        self.service.observe(force=True)
        self.assertEqual(self.service.observe()["app"], "chrome.exe")

    def test_clearing_forgets_the_temporary_context(self):
        self.turn_on()
        self.service.observe()
        self.assertTrue(self.service.clear())
        self.assertFalse(self.service.clear())
        self.assertIn("还没读到", self.service.status())

    def test_a_change_is_published_once(self):
        self.turn_on()
        self.service.observe()
        self.service.observe()
        names = [event.get("name") for event in self.bus.recent(10)]
        self.assertEqual(names.count("awareness.changed"), 1)

    def test_the_description_lists_real_capabilities(self):
        self.turn_on()
        describe = self.service.describe()
        self.assertEqual(describe["level_label"], "只看当前应用")
        self.assertTrue(describe["activity"]["available"])
        self.assertIn("level", describe)
        self.assertIn("exclusions", describe)

    def test_an_unavailable_platform_is_not_faked(self):
        service = aw.AwarenessService(
            preferences=self.memory,
            privacy=self.privacy,
            activity=aw.NullActivityProvider(),
        )
        self.privacy.set("awareness", True)
        service.set_level("metadata")
        self.assertFalse(service.observe()["ok"])

    def test_building_the_service_picks_a_provider(self):
        built = aw.build_service(preferences=self.memory, privacy=self.privacy)
        self.assertIsNotNone(built.activity.describe())
        self.assertIsNotNone(built.semantic.describe())


class AwarenessPageTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(theme.reset_scale)
        theme.reset_scale()
        try:
            self.root = tk.Tk()
        except Exception as exc:  # pragma: no cover
            self.skipTest("tkinter 不可用：%s" % exc)
        self.root.withdraw()
        self.addCleanup(self._destroy)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.config = Config(load_env=False)
        self.config.data_dir = base
        self.config.log_dir = base / "logs"
        self.config.db_path = base / "t.sqlite3"
        self.config.characters_dir = base / "characters"
        self.config.live2d_dir = base / "live2d"
        self.config.devices_dir = base / "devices"
        self.config.tray = False
        self.config.live2d_model = ""

    def _destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def build_pet(self):
        database = Database(self.config.db_path)
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        agent = AgentCore(StubProvider(), memory, ContextManager(token_budget=800))
        privacy = PrivacyCenter(preferences=memory, config=self.config)
        service = aw.AwarenessService(
            preferences=memory, privacy=privacy, activity=FakeActivity(), semantic=CountingSemantic()
        )
        pet = DesktopPet(agent, self.config, memory=memory, root=self.root, awareness=service)
        pet.show_chat()
        return pet, service, privacy

    def test_the_page_opens_in_the_workbench(self):
        pet, _service, _privacy = self.build_pet()
        page = pet.open_awareness()
        self.assertTrue(pet.chat.page_is_open())
        self.assertEqual(pet.chat.page_key, "awareness")
        page.refresh()
        self.assertGreater(len(page.state_tree.get_children()), 0)

    def test_the_switch_reflects_the_real_state(self):
        pet, service, privacy = self.build_pet()
        page = pet.open_awareness()
        page.refresh()
        self.assertFalse(page.on_var.get())
        privacy.set("awareness", True)
        page.on_var.set(True)
        page.toggle()
        self.assertTrue(service.active())
        page.on_var.set(False)
        page.toggle()
        self.assertEqual(service.level(), "off")

    def test_the_switch_stays_off_without_permission(self):
        pet, service, _privacy = self.build_pet()
        page = pet.open_awareness()
        page.on_var.set(True)
        page.toggle()
        self.assertFalse(service.active())
        self.assertFalse(page.on_var.get())

    def test_the_exclusion_list_round_trips(self):
        pet, service, privacy = self.build_pet()
        privacy.set("awareness", True)
        service.set_level("metadata")
        page = pet.open_awareness()
        page.exclude_entry.insert(0, "wechat.exe")
        page.add_exclusion()
        self.assertIn("wechat.exe", service.exclusions())
        self.assertEqual(len(page.exclude_tree.get_children()), 1)
        page.exclude_tree.selection_set(page.exclude_tree.get_children()[0])
        page.remove_exclusion()
        self.assertEqual(service.exclusions(), ())

    def test_the_rows_never_claim_to_see_the_screen(self):
        pet, service, privacy = self.build_pet()
        privacy.set("awareness", True)
        service.set_level("metadata")
        rows = dict(pet.awareness_rows())
        self.assertEqual(rows["读取层级"], "只看当前应用")
        self.assertEqual(rows["现在在用"], "code.exe")

    def test_a_pet_without_the_service_says_so(self):
        database = Database(self.config.db_path)
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        agent = AgentCore(StubProvider(), memory, ContextManager(token_budget=800))
        pet = DesktopPet(agent, self.config, memory=memory, root=self.root)
        self.assertEqual(pet.awareness_rows(), [])
        self.assertFalse(pet.toggle_awareness())


if __name__ == "__main__":
    unittest.main()
