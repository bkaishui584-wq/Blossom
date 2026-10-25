"""阶段 F 验收：本地基础能力。

三件事：
  1. 六类必备能力都在，而且真的能跑出结果（不是只有一个名字）。
  2. 没有任何模型时，这些确定性能力照样可用；应用照样起得来。
  3. 手动界面只能跑"只读、不要参数、自己会说人话"的那批；写文件、跑命令这类
     碰不到，它们要单独授权。

全程临时目录和临时数据库，不联网。
"""

import os
import sys
import tempfile
import tkinter as tk
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.main import build_agent  # noqa: E402
from behavior.manager import BehaviorManager  # noqa: E402
from database.db import Database  # noqa: E402
from events.bus import EventBus  # noqa: E402
from memory.manager import MemoryManager  # noqa: E402
from memory.repository import MemoryRepository  # noqa: E402
from security.config import Config  # noqa: E402
from tools.permission import SAFE_LEVELS, normalize_level  # noqa: E402
from tools.registry import Tool  # noqa: E402
from ui.capability_view import CapabilityWindow  # noqa: E402
from ui.desktop import DesktopPet  # noqa: E402

# 阶段 F 点名要有的六类，每类至少一个工具。名字按当前工程实际注册的来。
REQUIRED = {
    "时间与计时": ("local_time", "timer_start", "pomodoro"),
    "待办与提醒": ("todo_add", "todo_list", "todo_done", "add_calendar_event"),
    "本地文件搜索": ("search_files",),
    "文本处理": ("text_process",),
    "任务记录与诊断": ("task_unfinished", "run_diagnostics", "system_status"),
    "离线能力说明": ("local_ability_status",),
}

# 碰文件、碰系统、往外发的东西，必须单独授权，不许进手动入口。
RISKY = (
    "write_file",
    "copy_file",
    "run_command",
    "run_tests",
    "launch_app",
    "open_path",
    "git_action",
    "restore_backup",
)

NO_MODEL_ENV = {
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


@contextmanager
def no_model_env(root):
    """把模型相关的环境变量清掉，跑完原样恢复。

    只"不设置"它们是不够的：机器上要是本来就有，测试就测不准。
    """
    keys = list(NO_MODEL_ENV) + ["DESKPET_ROOT"]
    saved = {key: os.environ.get(key) for key in keys}
    os.environ["DESKPET_ROOT"] = str(root)
    for key in NO_MODEL_ENV:
        os.environ.pop(key, None)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class OfflineBase(unittest.TestCase):
    """按真实启动流程装一遍，但不配任何模型。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        env = no_model_env(self.base)
        env.__enter__()
        self.addCleanup(lambda: env.__exit__(None, None, None))
        self.config = Config(load_env=False)
        self.database = Database(self.config.db_path)
        self.database.migrate()
        self.agent, self.memory = build_agent(
            self.config, self.database, None, None, bus=EventBus()
        )


class InventoryTests(OfflineBase):
    def test_every_required_area_has_a_real_tool(self):
        names = set(self.agent.registry.names())
        for area, wanted in REQUIRED.items():
            with self.subTest(area=area):
                self.assertTrue(set(wanted) & names, "%s 一个工具都没有" % area)

    def test_file_and_system_tools_need_their_own_authorisation(self):
        registry = self.agent.registry
        for name in RISKY:
            tool = registry.get(name)
            self.assertIsNotNone(tool, name)
            self.assertNotIn(
                normalize_level(tool.permission),
                SAFE_LEVELS,
                "%s 不该落在自动放行的等级里" % name,
            )

    def test_there_is_no_delete_tool(self):
        for name in self.agent.registry.names():
            self.assertNotIn("delete", name.lower(), name)
            self.assertNotIn("remove", name.lower(), name)


class NoModelTests(OfflineBase):
    """一个模型都没有，这些活儿也要干得了。"""

    def test_the_app_comes_up_in_local_mode(self):
        self.assertEqual(self.agent.mode_name, "local")
        self.assertFalse(self.config.has_companion())
        self.assertFalse(self.config.has_task())

    def run_tool(self, name, arguments=None):
        return self.agent.executor.execute(name, arguments or {})

    def test_time_still_works(self):
        outcome = self.run_tool("local_time")
        self.assertTrue(outcome.ok, outcome.error)
        self.assertTrue(str(outcome.result).strip())

    def test_todo_can_be_added_and_listed(self):
        added = self.run_tool("todo_add", {"title": "把验收文档补上"})
        self.assertTrue(added.ok, added.error)
        listed = self.run_tool("todo_list")
        self.assertTrue(listed.ok, listed.error)
        self.assertIn("把验收文档补上", str(listed.result))

    def test_text_processing_works(self):
        outcome = self.run_tool("text_process", {"text": "  hello   world  ", "what": "clean"})
        self.assertTrue(outcome.ok, outcome.error)
        self.assertTrue(str(outcome.result).strip())

    def test_diagnostics_run_without_a_model(self):
        outcome = self.run_tool("run_diagnostics")
        self.assertTrue(outcome.ok, outcome.error)
        self.assertTrue(str(outcome.result).strip())

    def test_a_chat_answer_still_comes_back_without_a_model(self):
        reply = self.agent.respond("s", "现在几点")
        self.assertTrue(reply.ok, reply.error)


class NetworkToolTests(OfflineBase):
    """天气、新闻这类要联网的，不许装作本地就能用。"""

    def test_a_network_tool_says_why_it_cannot_work(self):
        for name in ("get_news", "get_weather"):
            with self.subTest(tool=name):
                outcome = self.agent.executor.execute(name, {})
                if outcome.ok:
                    self.assertTrue(str(outcome.result).strip(), "说成功就得有内容")
                else:
                    self.assertTrue(outcome.error, "连不上就得给出原因")


class ManualEntryTests(unittest.TestCase):
    """能力中心里的手动入口：只能跑安全那批。"""

    def setUp(self):
        try:
            self.root = tk.Tk()
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        self.root.withdraw()
        self.addCleanup(self._destroy)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        env = no_model_env(base)
        env.__enter__()
        self.addCleanup(lambda: env.__exit__(None, None, None))
        config = Config(load_env=False)
        config.data_dir = base
        config.log_dir = base / "logs"
        config.db_path = base / "t.sqlite3"
        config.characters_dir = base / "characters"
        config.live2d_dir = base / "live2d"
        config.devices_dir = base / "devices"
        database = Database(base / "t.sqlite3")
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        agent, memory = build_agent(config, database, None, None, bus=EventBus())
        self.pet = DesktopPet(
            agent,
            config,
            memory=memory,
            behavior=BehaviorManager(persona=agent.persona),
            root=self.root,
        )
        self.addCleanup(self._close)
        frame = tk.Frame(self.root)
        frame.pack(fill="both", expand=True)
        self.view = CapabilityWindow(self.pet, parent=frame)

    def _destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _close(self):
        try:
            self.pet.close_panel()
        except Exception:
            pass

    def test_the_manual_list_holds_only_safe_readonly_tools(self):
        rows = self.view._runnable_tools()
        names = {row["name"] for row in rows}
        self.assertTrue(names, "总得有几条能手动跑的")
        for name in RISKY:
            self.assertNotIn(name, names, "%s 不该出现在手动入口里" % name)
        for row in rows:
            self.assertTrue(row["readonly"], row["name"])
            self.assertIn(normalize_level(row["permission"]), SAFE_LEVELS, row["name"])

    def test_running_one_shows_the_real_result(self):
        if "local_time" not in self.view.ability_names:
            self.skipTest("这台机器上没列出 local_time")
        text = self.view.run_ability("local_time")
        self.assertIn("local_time", text)
        self.assertIn(text, self.view.ability_result.cget("text"))

    def test_a_risky_tool_cannot_be_run_from_here(self):
        self.assertIn("先挑一条", self.view.run_ability("write_file"))

    def test_an_unknown_name_cannot_be_run_from_here(self):
        self.assertIn("先挑一条", self.view.run_ability("压根没有这个工具"))

    def test_searching_files_works_here_without_a_model(self):
        """要一个搜索词的那种，也是能手动跑的。"""
        self.assertIn("search_files", self.view.ability_names)
        target = Path(self.tmp.name) / "note.md"
        target.write_text("这里有一句独一无二的暗号：紫罗兰色的茶杯", encoding="utf-8")
        text = self.view.run_ability("search_files", {"query": "紫罗兰色的茶杯"})
        self.assertIn("search_files", text)
        self.assertNotIn("没跑成", text)

    def test_a_tool_that_needs_more_than_one_string_stays_out(self):
        """参数不是一个字符串的，不硬凑表单，等有模型再说。"""
        two = Tool(
            name="two",
            description="两个参数",
            permission="READ",
            schema={
                "required": ["a", "b"],
                "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
            },
        )
        numeric = Tool(
            name="numeric",
            description="要一个数字",
            permission="READ",
            schema={"required": ["n"], "properties": {"n": {"type": "integer"}}},
        )
        single = Tool(
            name="single",
            description="只要一个字符串",
            permission="READ",
            schema={"required": ["q"], "properties": {"q": {"type": "string"}}},
        )
        self.assertFalse(self.view._askable(two))
        self.assertFalse(self.view._askable(numeric))
        self.assertTrue(self.view._askable(single))


if __name__ == "__main__":
    unittest.main()
