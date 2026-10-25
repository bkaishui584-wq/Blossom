"""阶段 C 验收：权限与安全。

守的是这几条：
  1. 工具的执行入口只有一个（ToolExecutor），别的地方碰不到它的 handler。
  2. 危险操作要用户点头，授权能撤销，撤销立刻生效。
  3. 模型递过来的非法参数在执行前就被拦下，handler 根本没机会跑。
  4. 屏幕内容交给云端模型要单独授权；本地模型不算"发出去"。
  5. 配置摘要、日志、审计里不许出现密钥明文。
  6. 工具抛异常、跑得慢、模型连不上，都不许伪装成成功。

全程临时目录和临时数据库。
"""

import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.context import ContextManager  # noqa: E402
from agent.core import AgentCore, SCREEN_TOOLS  # noqa: E402
from database.db import Database  # noqa: E402
from memory.manager import MemoryManager  # noqa: E402
from memory.repository import MemoryRepository  # noqa: E402
from model.base import ChatDelta, ChatResult, ModelProvider, ModelSlot, Usage  # noqa: E402
from persona.interface import DefaultPersonaProvider  # noqa: E402
from security.config import Config  # noqa: E402
from security.privacy import PrivacyCenter  # noqa: E402
from tools import screen as screen_tools  # noqa: E402
from tools.screen import screen_model_policy  # noqa: E402
from tools.permission import (  # noqa: E402
    GrantMode,
    GrantStore,
    PermissionLevel,
    PermissionManager,
)
from tools.registry import Tool, ToolExecutor, ToolRegistry  # noqa: E402

SECRET = "sk-不许出现在日志里-1234567890"


def source_files():
    for path in sorted(ROOT.rglob("*.py")):
        parts = set(path.parts)
        if parts & {"build", "dist", "__pycache__", "_internal", "tests"}:
            continue
        yield path


class EntryPointTests(unittest.TestCase):
    """工具只能从执行层跑，没有第二条路。"""

    def test_tool_handlers_are_only_called_by_the_executor(self):
        offenders = []
        for path in source_files():
            text = path.read_text(encoding="utf-8")
            if re.search(r"\btool\.handler\s*\(", text) and path.name != "registry.py":
                offenders.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(offenders, [], "只有 tools/registry.py 能直接调工具的 handler")

    def test_skills_go_through_the_same_entry(self):
        text = (ROOT / "skills" / "executor.py").read_text(encoding="utf-8")
        self.assertIn("self.tools.execute(", text)
        self.assertNotIn("tool.handler(", text)

    def test_local_capabilities_never_reach_out(self):
        """本地能力是程序自己算出来的确定性回答，不许它们去碰网络和进程。"""
        text = (ROOT / "agent" / "capabilities.py").read_text(encoding="utf-8")
        for needle in ("import socket", "urllib.request", "subprocess", "http.client"):
            self.assertNotIn(needle, text, needle)


def make_registry(flag=None):
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="read_thing",
            description="只读",
            permission=PermissionLevel.READ.value,
            schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
            handler=lambda name: (flag.append(name) if flag is not None else None) or ("读到了 %s" % name),
        )
    )
    registry.register(
        Tool(
            name="risky_thing",
            description="高风险",
            permission=PermissionLevel.HIGH.value,
            handler=lambda: (flag.append("risky") if flag is not None else None) or "做了",
        )
    )
    registry.register(
        Tool(
            name="broken_thing",
            description="会炸",
            permission=PermissionLevel.READ.value,
            handler=lambda: (_ for _ in ()).throw(RuntimeError("里面炸了")),
        )
    )
    return registry


class TempBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"DESKPET_ROOT": str(self.base)})
        env.start()
        self.addCleanup(env.stop)
        self.config = Config(load_env=False)
        self.database = Database(self.base / "t.sqlite3")
        self.database.migrate()
        self.memory = MemoryManager(MemoryRepository(self.database))


class GrantTests(TempBase):
    """授权能撤销，撤销立刻生效。"""

    def executor(self, mode=None, confirm=None):
        store = GrantStore(self.base / "grants.json")
        permissions = PermissionManager(
            confirm_callback=confirm, grants=store
        )
        if mode is not None:
            store.set("risky_thing", mode)
        return ToolExecutor(make_registry(), permissions), store

    def test_a_persistent_grant_lets_it_run_and_revoking_stops_it(self):
        executor, store = self.executor(mode=GrantMode.PERSISTENT.value)
        self.assertTrue(executor.execute("risky_thing", {}).ok)
        store.revoke("risky_thing")
        after = executor.execute("risky_thing", {})
        self.assertFalse(after.ok, "撤销之后立刻不放行")
        self.assertFalse(after.allowed)

    def test_a_denied_grant_blocks_the_low_risk_path_too(self):
        # 拒绝要对着被调用的那个工具说，才叫"拦住它"。
        store = GrantStore(self.base / "grants.json")
        store.set("read_thing", GrantMode.DENY.value)
        executor = ToolExecutor(
            make_registry(), PermissionManager(confirm_callback=lambda *a: True, grants=store)
        )
        outcome = executor.execute("read_thing", {"name": "x"})
        self.assertFalse(outcome.ok)
        self.assertFalse(outcome.allowed)

    def test_a_once_grant_is_spent_after_one_use(self):
        executor, _store = self.executor(mode=GrantMode.ONCE.value)
        self.assertTrue(executor.execute("risky_thing", {}).ok)
        self.assertFalse(executor.execute("risky_thing", {}).ok)

    def test_a_high_risk_tool_needs_a_confirmation_channel(self):
        flag = []
        executor = ToolExecutor(make_registry(flag), PermissionManager(confirm_callback=None))
        outcome = executor.execute("risky_thing", {})
        self.assertFalse(outcome.ok)
        self.assertTrue(outcome.needs_confirmation)
        self.assertEqual(flag, [], "没点头就不许真的执行")

    def test_an_approved_high_risk_tool_runs(self):
        executor = ToolExecutor(
            make_registry(), PermissionManager(confirm_callback=lambda *a: True)
        )
        self.assertTrue(executor.execute("risky_thing", {}).ok)


class ArgumentTests(TempBase):
    """模型递过来的参数先校验，不合格连 handler 都不碰。"""

    def setUp(self):
        super().setUp()
        self.flag = []
        self.executor = ToolExecutor(
            make_registry(self.flag), PermissionManager(confirm_callback=lambda *a: True)
        )

    def test_a_missing_required_argument_is_refused(self):
        outcome = self.executor.execute("read_thing", {})
        self.assertFalse(outcome.ok)
        self.assertIn("参数不合法", outcome.error)
        self.assertEqual(self.flag, [])

    def test_a_wrong_type_is_refused(self):
        outcome = self.executor.execute("read_thing", {"name": {"不是一个字符串": True}})
        self.assertFalse(outcome.ok)
        self.assertEqual(self.flag, [])

    def test_an_unexpected_argument_is_refused(self):
        outcome = self.executor.execute("read_thing", {"name": "x", "顺手删个文件": "/"})
        self.assertFalse(outcome.ok)
        self.assertEqual(self.flag, [], "多塞的参数一律不认")

    def test_a_good_call_runs_once(self):
        outcome = self.executor.execute("read_thing", {"name": "说明.md"})
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(self.flag, ["说明.md"])


class _Stub(ModelProvider):
    name = "stub"

    def is_available(self):
        return True

    def chat(self, messages, tools=None, temperature=0.7):
        return ChatResult(ok=True, content="好的", usage=Usage(), model="stub")

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


class ScreenGateTests(TempBase):
    """屏幕内容交给模型：云端要单独授权，本地模型不算发出去。"""

    def agent(self, base_url="", cloud_allowed=None, with_privacy=True):
        self.config.companion_base_url = base_url
        registry = ToolRegistry()
        registry.register(
            Tool(
                name=SCREEN_TOOLS[0],
                description="读当前前台窗口",
                permission=PermissionLevel.READ.value,
                keywords=("窗口", "前台", "屏幕"),
                handler=lambda: {"title": "记事本"},
            )
        )
        executor = ToolExecutor(registry, PermissionManager(confirm_callback=lambda *a: True))
        privacy = None
        policy = None
        if with_privacy:
            privacy = PrivacyCenter(preferences=self.memory, config=self.config)
            if cloud_allowed is not None:
                privacy.set("screen_to_model", cloud_allowed)
            policy = screen_model_policy(privacy, self.config)
        return AgentCore(
            _Stub(),
            self.memory,
            ContextManager(token_budget=800),
            persona=DefaultPersonaProvider(),
            executor=executor,
            config=self.config,
            screen_policy=policy,
            companion_slot=ModelSlot(role="companion", provider=_Stub(), configured=True),
        )

    def test_a_cloud_model_does_not_get_screen_tools_by_default(self):
        agent = self.agent(base_url="https://api.example.com/v1")
        self.assertNotIn(SCREEN_TOOLS[0], agent._select_tools("看看我在用什么", safe_only=True))

    def test_the_user_can_allow_it(self):
        agent = self.agent(base_url="https://api.example.com/v1", cloud_allowed=True)
        self.assertIn(SCREEN_TOOLS[0], agent._select_tools("现在前台是什么窗口", safe_only=True))

    def test_a_local_model_is_not_treated_as_sending_anything_out(self):
        agent = self.agent(base_url="http://127.0.0.1:11434/v1")
        self.assertIn(SCREEN_TOOLS[0], agent._select_tools("现在前台是什么窗口", safe_only=True))

    def test_a_lookalike_host_is_still_treated_as_cloud(self):
        agent = self.agent(base_url="https://127.0.0.1.evil.example/v1")
        self.assertNotIn(SCREEN_TOOLS[0], agent._select_tools("现在前台是什么窗口", safe_only=True))

    def test_a_cloud_model_still_gets_the_tool_when_the_switch_is_on(self):
        agent = self.agent(base_url="https://api.example.com/v1", cloud_allowed=True)
        self.assertTrue(agent.screen_tools_allowed("companion"))
        self.assertIn(SCREEN_TOOLS[0], agent._select_tools("现在前台是什么窗口", safe_only=True))

    def test_the_rule_itself_knows_a_local_model_from_a_cloud_one(self):
        privacy = PrivacyCenter(preferences=self.memory, config=self.config)
        self.config.companion_base_url = "http://localhost:11434/v1"
        self.assertTrue(
            screen_tools.screen_to_model_allowed(privacy, self.config, "companion")
        )
        self.config.companion_base_url = "https://api.example.com/v1"
        self.assertFalse(
            screen_tools.screen_to_model_allowed(privacy, self.config, "companion")
        )
        privacy.set("screen_to_model", True)
        self.assertTrue(
            screen_tools.screen_to_model_allowed(privacy, self.config, "companion")
        )
        self.assertTrue(
            screen_tools.screen_to_model_allowed(None, self.config, "companion"),
            "没有隐私中心时按老行为放行，主开关仍然把关",
        )

    def test_the_master_switch_still_closes_the_screen_tool(self):
        """没开屏幕感知时，工具直接说做不了，跟授权无关。"""
        screen_tools.set_vision_provider(privacy=lambda: False)
        self.addCleanup(lambda: screen_tools.set_vision_provider(privacy=None))
        self.assertFalse(screen_tools.perception_on())


class SecretHygieneTests(TempBase):
    """密钥不许出现在摘要、日志和审计里。"""

    def test_the_config_summary_never_shows_the_key(self):
        with mock.patch.dict(os.environ, {"DESKPET_LLM_API_KEY": SECRET}):
            config = Config(load_env=False)
        blob = repr(config.redacted())
        self.assertNotIn(SECRET, blob)
        self.assertEqual(config.llm_api_key, SECRET, "真要用的地方还拿得到")

    def test_a_failed_model_call_does_not_write_the_key_to_the_log(self):
        """所有日志都过同一个过滤器，所以这里直接验那一层。

        程序启动时会把 config.secret_values() 逐个登记进去（app/main.py 里那两处），
        这里照做，然后故意写一条带密钥的日志，看它落盘之后长什么样。
        """
        from logkit.logger import configure, get_logger, register_secret

        log_dir = self.base / "logs"
        configure(log_dir, level="INFO")
        logger = get_logger()
        # 测试结束时放掉文件句柄，Windows 才删得掉临时目录。
        self.addCleanup(lambda: [handler.close() for handler in list(logger.handlers)])
        with mock.patch.dict(os.environ, {"DESKPET_LLM_API_KEY": SECRET}):
            config = Config(load_env=False)
        self.assertIn(SECRET, config.secret_values(), "配置里的密钥要能交给过滤器")
        for value in config.secret_values():
            register_secret(value)

        logger.warning("模拟一次调用失败：%s", SECRET)
        logger.warning("模拟一次带前缀的失败：Authorization: Bearer %s", SECRET)
        for handler in list(logger.handlers):
            handler.flush()

        blob = (log_dir / "deskpet.log").read_text(encoding="utf-8", errors="replace")
        self.assertIn("模拟一次调用失败", blob, "日志要真写下来，不然这个测试没意义")
        self.assertNotIn(SECRET, blob, "密钥不许落到日志里")


class ResilienceTests(unittest.TestCase):
    """工具炸了、跑慢了，都要如实说。"""

    def executor(self):
        return ToolExecutor(make_registry(), PermissionManager(confirm_callback=lambda *a: True))

    def test_a_tool_that_throws_is_reported_not_swallowed(self):
        outcome = self.executor().execute("broken_thing", {})
        self.assertFalse(outcome.ok)
        self.assertIn("里面炸了", outcome.error)
        self.assertTrue(outcome.allowed, "它是被允许之后自己炸的")

    def test_a_slow_tool_is_still_a_real_result(self):
        registry = ToolRegistry()
        registry.register(
            Tool(
                name="slow_thing",
                description="慢",
                permission=PermissionLevel.READ.value,
                timeout=0.05,
                handler=lambda: (time.sleep(0.12) or "慢但做完了"),
            )
        )
        outcome = ToolExecutor(
            registry, PermissionManager(confirm_callback=lambda *a: True)
        ).execute("slow_thing", {})
        self.assertTrue(outcome.ok, "超时不硬杀线程，但结果是真的")
        self.assertTrue(outcome.slow, "慢要标出来")


if __name__ == "__main__":
    unittest.main()
