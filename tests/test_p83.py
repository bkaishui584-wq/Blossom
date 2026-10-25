"""P2 验收：本地智能与工作流。

覆盖：能力目录说的依赖是真的、高频说法都走到对的本地能力上、
没有模型也答得出来、记东西不靠模型、任务做到哪和为什么没成能查、
在等的提醒和主动规则列得出来、有任务模型时本地不抢活、
取不到数据时如实说取不到。全程用临时数据库，不碰真实用户数据。
"""

import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent import capabilities as caps
from agent.context import ContextManager
from agent.core import AgentCore
from agent.task import TaskRecord, TaskState
from app.main import build_agent
from database.db import Database
from logkit.logger import get_logger
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatDelta, ChatResult, ModelProvider, Usage
from security.config import Config
from ui import theme
from ui.desktop import DesktopPet


class OfflineProvider(ModelProvider):
    """一个没配好的聊天模型：不可用，也不该被调用。"""

    name = "offline"

    def __init__(self):
        self.calls = 0

    def is_available(self):
        return False

    def chat(self, messages, tools=None, temperature=0.7):
        self.calls += 1
        return ChatResult(ok=False, error="没有配模型", usage=Usage(), model="offline")

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


class TempCase(unittest.TestCase):
    def setUp(self):
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

    def build_agent(self, provider=None):
        database = Database(self.config.db_path)
        database.migrate()
        agent, memory = build_agent(
            self.config, database, provider or OfflineProvider(), get_logger()
        )
        return agent, memory


class CatalogTests(TempCase):
    """能力目录：说的是真的，能用的才写能用。"""

    def test_every_capability_has_a_label_and_an_honest_requirement(self):
        rows = caps.LocalCapabilityRegistry().catalog()
        self.assertGreaterEqual(len(rows), 18)
        for row in rows:
            self.assertTrue(row["label"], row)
            self.assertIn(row["requires"], (caps.REQUIRES_LOCAL, caps.REQUIRES_NETWORK, caps.REQUIRES_MODEL))
            self.assertTrue(row["reason"], row)

    def test_only_local_ones_count_as_offline(self):
        registry = caps.LocalCapabilityRegistry()
        registry.add(caps.LocalCapability("weather", "查天气", lambda text, ctx: None, ("天气",), requires=caps.REQUIRES_NETWORK))
        offline = registry.offline_names()
        self.assertIn("time", offline)
        self.assertIn("tasks", offline)
        self.assertNotIn("weather", offline)

    def test_a_broken_probe_is_not_reported_as_working(self):
        registry = caps.LocalCapabilityRegistry(
            probes={"time": lambda: (_ for _ in ()).throw(RuntimeError("探针炸了"))}
        )
        row = [item for item in registry.catalog() if item["name"] == "time"][0]
        self.assertFalse(row["ready"])
        self.assertEqual(row["reason"], "能力检测没跑起来")


class RoutingTests(TempCase):
    """规则路由：常见说法走到对的本地能力上，不靠模型猜。"""

    def setUp(self):
        super().setUp()
        self.agent, self.memory = self.build_agent()

    def answer(self, text):
        reply = self.agent.respond("p2", text)
        return reply

    def test_local_phrasings_hit_local_capabilities(self):
        cases = {
            "现在几点了？": "time",
            "今天几号？": "date",
            "算一下 12*12": "calculator",
            "100 厘米等于多少米": "convert",
        }
        for text, expected in cases.items():
            reply = self.answer(text)
            self.assertTrue(reply.ok, text)
            self.assertEqual(reply.debug.get("capability"), expected, text)
            self.assertEqual(reply.debug.get("mode"), "local", text)

    def test_a_timer_says_so_when_the_scheduler_is_missing(self):
        reply = self.answer("5 分钟后提醒我喝水")
        self.assertTrue(reply.ok)
        self.assertEqual(reply.debug.get("capability"), "timer")
        self.assertTrue(reply.content.strip())

    def test_asking_about_weather_without_a_service_is_not_faked(self):
        reply = self.answer("今天天气怎么样")
        text = (reply.content or reply.error or "")
        self.assertNotIn("晴", text)
        self.assertNotIn("度", text)

    def test_no_model_means_no_pretend_chat(self):
        reply = self.answer("你觉得宇宙的意义是什么")
        self.assertFalse(reply.ok)
        self.assertEqual(reply.debug.get("error_type"), "COMPANION_MODEL_UNAVAILABLE")
        self.assertFalse(reply.content)

    def test_the_same_agent_still_answers_local_questions(self):
        self.assertFalse(self.answer("聊点哲学").ok)
        reply = self.answer("算一下 6*7")
        self.assertTrue(reply.ok)
        self.assertIn("42", reply.content)


class MemoryWithoutModelTests(TempCase):
    """记东西、查东西都用本地库，不需要模型。"""

    def test_remembering_writes_a_user_stated_memory(self):
        agent, memory = self.build_agent()
        reply = agent.respond("p2", "记住我明天要交报告")
        self.assertTrue(reply.ok)
        rows = memory.recent_memories(limit=5)
        self.assertEqual(len(rows), 1)
        self.assertIn("明天要交报告", rows[0]["content"])
        self.assertEqual(rows[0]["source"], "USER_EXPLICIT")

    def test_asking_back_what_was_saved_answers_from_the_local_store(self):
        agent, memory = self.build_agent()
        agent.respond("p2", "记住我的项目叫芳菲")
        reply = agent.respond("p2", "你还记得我的项目吗")
        self.assertTrue(reply.ok)
        self.assertIn("芳菲", reply.content)


class TaskStatusTests(TempCase):
    """任务做到哪、为什么没成：读本地任务库，读不到就直说。"""

    def setUp(self):
        super().setUp()
        self.agent, self.memory = self.build_agent()
        self.store = self.agent.task_store

    def seed(self):
        record = TaskRecord(goal="把报告写完", state=TaskState.RUNNING.value)
        self.store.save(record)
        self.store.replace_steps(record.id, [
            {"index": 0, "title": "读资料", "status": "done"},
            {"index": 1, "title": "写初稿", "status": "pending"},
        ])
        failed = TaskRecord(goal="改配置文件", state=TaskState.FAILED.value, error="权限不足，写文件被拒")
        self.store.save(failed)
        return record

    def test_it_reports_the_unfinished_work_and_the_next_step(self):
        self.seed()
        reply = self.agent.respond("p2", "现在有哪些任务没做完")
        self.assertTrue(reply.ok)
        self.assertEqual(reply.debug.get("capability"), "tasks")
        self.assertIn("把报告写完", reply.content)
        self.assertIn("写初稿", reply.content)

    def test_it_tells_why_something_failed(self):
        self.seed()
        reply = self.agent.respond("p2", "上次为什么失败了")
        self.assertTrue(reply.ok)
        self.assertIn("改配置文件", reply.content)
        self.assertIn("权限不足", reply.content)

    def test_no_task_store_is_admitted(self):
        router = caps.LocalCapabilityRegistry()
        answer = router.resolve("有哪些任务")
        self.assertEqual(answer.text, "任务记录在本地任务库里，现在读不到。")

    def test_an_empty_store_does_not_invent_tasks(self):
        reply = self.agent.respond("p2", "有哪些任务")
        self.assertTrue(reply.ok)
        self.assertIn("还没有任务记录", reply.content)

    def test_with_a_task_model_the_local_layer_steps_aside(self):
        # 有任务模型时，接着干这种事交给它，本地不抢答。
        router = caps.LocalCapabilityRegistry(
            tasks=self.store, status=lambda: {"task_ready": True, "companion_ready": True}
        )
        self.assertIsNone(router.resolve("继续昨天没做完的任务"))
        # 只是问进度，本地照样答。
        answer = router.resolve("有哪些任务")
        self.assertIsNotNone(answer)
        self.assertEqual(answer.capability, "tasks")

    def test_without_a_task_model_it_says_what_is_missing(self):
        self.seed()
        reply = self.agent.respond("p2", "继续昨天没做完的任务")
        self.assertTrue(reply.ok)
        self.assertIn("需要任务模型", reply.content)


class PlanListingTests(TempCase):
    """在等的提醒和主动规则：列真的装上的，并说清程序关了就不跑。"""

    def test_it_lists_the_installed_rules_and_the_honest_caveat(self):
        agent, _memory = self.build_agent()

        class Rule:
            name = "daily_summary"

        class FakeScheduler:
            rules = [Rule()]

        agent.capabilities.scheduler = FakeScheduler()
        reply = agent.respond("p2", "有哪些计划")
        self.assertTrue(reply.ok)
        self.assertEqual(reply.debug.get("capability"), "plans")
        self.assertIn("daily_summary", reply.content)
        self.assertIn("关掉程序", reply.content)

    def test_nothing_scheduled_says_nothing_scheduled(self):
        agent, _memory = self.build_agent()
        reply = agent.respond("p2", "有哪些计划")
        self.assertTrue(reply.ok)
        self.assertIn("没有排着的提醒或主动规则", reply.content)


class CapabilityPageTests(TempCase):
    """能力页：把依赖和状态摆出来，不吹。"""

    def setUp(self):
        super().setUp()
        self.addCleanup(theme.reset_scale)
        theme.reset_scale()
        try:
            self.root = tk.Tk()
        except Exception as exc:  # pragma: no cover
            self.skipTest("tkinter 不可用：%s" % exc)
        self.root.withdraw()
        self.addCleanup(self._destroy)

    def _destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def build_pet(self):
        database = Database(self.config.db_path)
        database.migrate()
        agent, memory = build_agent(
            self.config, database, OfflineProvider(), get_logger()
        )
        pet = DesktopPet(agent, self.config, memory=memory, root=self.root)
        pet.show_chat()
        return pet

    def test_the_page_lists_local_capabilities_with_their_state(self):
        pet = self.build_pet()
        page = pet.open_capabilities()
        self.assertTrue(pet.chat.page_is_open())
        page.refresh()
        rows = [page.cap_tree.item(item, "values") for item in page.cap_tree.get_children()]
        self.assertTrue(rows)
        names = {row[0] for row in rows}
        self.assertIn("看任务做到哪了", names)
        offline = str(page.offline_label.cget("text"))
        self.assertIn("断网、没模型也能用", offline)
        self.assertIn("tasks", offline)

    def test_the_tools_tab_shows_real_permission_levels(self):
        pet = self.build_pet()
        page = pet.open_capabilities()
        page.refresh()
        rows = [page.tool_tree.item(item, "values") for item in page.tool_tree.get_children()]
        self.assertTrue(rows)
        names = {row[0] for row in rows}
        self.assertIn("local_time", names)
        self.assertIn("write_file", names)


if __name__ == "__main__":
    unittest.main()