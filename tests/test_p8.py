"""P8 验收：整体联调与安全检查。"""

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.context import ContextManager
from agent.core import AgentCore
from database.db import Database
from devices.interface import MockDevice
from devices.manager import DeviceManager
from logkit.logger import configure, get_logger, register_secret
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatDelta, ChatResult, ModelProvider, ToolCall, Usage
from security.audit import AuditLog
from security.config import Config
from tools.builtin import register_default_tools
from tools.permission import PermissionManager
from tools.registry import Tool, ToolError, ToolExecutor, ToolRegistry


def source_files():
    # 打包产物与临时目录不是项目源码，安全扫描不看它们。
    skip = {"tests", "__pycache__", "dist", "build", ".venv", "data", "logs"}
    return [
        path
        for path in ROOT.rglob("*.py")
        if not skip.intersection(path.parts)
    ]


class SecurityStaticTests(unittest.TestCase):
    def test_no_hardcoded_api_keys_in_source(self):
        pattern = re.compile(r"sk-[A-Za-z0-9_\-]{16,}")
        for path in source_files():
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(pattern.search(text), f"疑似硬编码密钥：{path}")

    def test_gitignore_covers_secrets_and_data(self):
        lines = {
            line.strip()
            for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        }
        for entry in (".env", "data/", "logs/"):
            self.assertIn(entry, lines)

    def test_env_example_has_blank_key_values(self):
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("AGENT_LLM_API_KEY=") or line.startswith("DESKPET_NEWS_API_KEY="):
                self.assertEqual(line.split("=", 1)[1].strip(), "")

    def test_agent_core_has_no_concrete_model_binding(self):
        text = (ROOT / "agent" / "core.py").read_text(encoding="utf-8").lower()
        for name in ("openai", "deepseek", "qwen", "dashscope", "anthropic"):
            self.assertNotIn(name, text)

    def test_only_adapter_talks_to_model_sdk(self):
        hits = set()
        for path in source_files():
            text = path.read_text(encoding="utf-8")
            if "from openai" in text or "import openai" in text:
                hits.add(path.relative_to(ROOT).as_posix())
        self.assertEqual(hits, {"model/adapter.py"})

    # 豁免列表：只有这几个模块需要起本机进程，且都不接 shell。
    # avatar/web2d.py 是 Live2D 渲染桥，参数全是本机路径。
    # tools/workspace.py 是 V4 工作区工具，只跑白名单命令（unittest / pytest /
    # compileall 和只读 git），命令是 argv 列表、shell=False，写入前还要过权限确认。
    # tools/apps.py 是 V5 的应用启动工具，只按用户登记的清单起进程，argv 列表、
    # shell=False，且权限等级是 HIGH，模型不能自己往里塞程序名。
    # adapters/output_reducer.py 是 V9 的可选输出压缩器：只在用户启用 RTK 时
    # 用 argv 列表跑一个白名单二进制（rtk compress），shell=False，带超时，
    # 拿不到结果就退回本地压缩；它压的是文本，不执行命令里的内容。
    # adapters/local_runtimes.py 是 V9.1 的本地运行时探测：只为读状态跑几条只读命令
    # （ollama list 之类），argv 列表、shell=False、带超时，装东西一律不代劳。
    # 其余文件一律不许出现这些原语。
    RENDER_BRIDGE = Path("avatar") / "web2d.py"
    WORKSPACE_TOOLS = Path("tools") / "workspace.py"
    APP_TOOLS = Path("tools") / "apps.py"
    OUTPUT_REDUCER = Path("adapters") / "output_reducer.py"
    RUNTIME_PROBE = Path("adapters") / "local_runtimes.py"
    STARTUP = Path("security") / "startup.py"
    PROCESS_MODULES = (
        RENDER_BRIDGE,
        WORKSPACE_TOOLS,
        APP_TOOLS,
        OUTPUT_REDUCER,
        RUNTIME_PROBE,
        # 桌面快捷方式：只起一次 powershell，脚本是固定常量，路径按参数传，
        # 拼不出别的命令。WScript.Shell 没有纯 Python 的替代品。
        STARTUP,
    )

    def test_no_shell_or_eval_primitives(self):
        banned = ("os.system", "subprocess", "eval(", "exec(", "pickle.load")
        for path in source_files():
            if path.relative_to(ROOT) in self.PROCESS_MODULES:
                continue
            text = path.read_text(encoding="utf-8")
            for needle in banned:
                self.assertNotIn(needle, text, f"{needle} 出现在 {path}")

    def test_the_workspace_tools_never_shell_out(self):
        text = (ROOT / "tools" / "workspace.py").read_text(encoding="utf-8")
        for needle in ("shell=True", "os.system", "eval(", "exec("):
            self.assertNotIn(needle, text, needle)
        # 只认白名单的测试入口，别的一律拒绝。
        self.assertIn("TEST_RUNNERS", text)
        self.assertIn("subprocess.run", text)

    def test_the_app_tools_never_shell_out(self):
        """应用启动只按用户登记的清单来，不起 shell，也不执行任意字符串。"""
        text = (ROOT / "tools" / "apps.py").read_text(encoding="utf-8")
        for needle in ("shell=True", "os.system", "eval(", "exec("):
            self.assertNotIn(needle, text, needle)
        # 名字必须在清单里才起进程；不在就报错。
        self.assertIn("if key not in table", text)
        self.assertIn("subprocess.Popen", text)
        # 可执行文件不通过「打开文件」代开。
        self.assertIn("OPENABLE_SUFFIXES", text)

    def test_the_output_reducer_never_shells_out(self):
        """可选的输出压缩器只按 argv 跑白名单二进制，失败就退回本地。"""
        text = (ROOT / "adapters" / "output_reducer.py").read_text(encoding="utf-8")
        for needle in ("shell=True", "os.system", "eval(", "exec("):
            self.assertNotIn(needle, text, needle)
        # 传给外部压缩器的是 argv 列表，而且带超时。
        self.assertIn('["rtk", "compress", "--"', text)
        self.assertIn("timeout=20", text)

    def test_the_render_bridge_never_shells_out(self):
        text = (ROOT / "avatar" / "web2d.py").read_text(encoding="utf-8")
        self.assertNotIn("shell=True", text)
        self.assertNotIn("os.system", text)
        self.assertNotIn("eval(", text)
        self.assertNotIn("exec(", text)


class SecretHygieneTests(unittest.TestCase):
    def test_redacted_view_never_shows_key(self):
        config = Config(load_env=False)
        config.llm_api_key = "sk-very-secret-value-9876543210"
        view = config.redacted()
        self.assertEqual(view["llm_api_key"], "set")
        self.assertNotIn("sk-very-secret-value-9876543210", json.dumps(view))

    def test_key_never_reaches_log_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = configure(tmp, "INFO", dev_mode=False)
            try:
                register_secret("sk-integration-secret-123456")
                get_logger("p8").info("token=%s", "sk-integration-secret-123456")
                for handler in logger.handlers:
                    handler.flush()
                logged = (Path(tmp) / "deskpet.log").read_text(encoding="utf-8")
                self.assertNotIn("sk-integration-secret-123456", logged)
            finally:
                for handler in list(logger.handlers):
                    handler.close()
                    logger.removeHandler(handler)


class PermissionGuardTests(unittest.TestCase):
    def setUp(self):
        self.invoked = []

    def _registry_with(self, level):
        registry = ToolRegistry()
        registry.register(
            Tool(
                name="run_system_command",
                description="执行系统命令",
                schema={"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]},
                permission=level,
                handler=lambda cmd: self.invoked.append(cmd) or "done",
            )
        )
        return registry

    def test_critical_tool_is_blocked_and_handler_not_called(self):
        executor = ToolExecutor(self._registry_with("CRITICAL"), PermissionManager())
        outcome = executor.execute("run_system_command", {"cmd": "rm -rf /"})
        self.assertFalse(outcome.ok)
        self.assertFalse(outcome.allowed)
        self.assertEqual(self.invoked, [])

    def test_high_tool_denied_without_confirmation_channel(self):
        executor = ToolExecutor(self._registry_with("HIGH"), PermissionManager())
        outcome = executor.execute("run_system_command", {"cmd": "echo hi"})
        self.assertFalse(outcome.allowed)
        self.assertEqual(self.invoked, [])

    def test_bogus_level_is_treated_as_highest_risk(self):
        executor = ToolExecutor(self._registry_with("SUPERUSER"), PermissionManager())
        outcome = executor.execute("run_system_command", {"cmd": "echo hi"})
        self.assertFalse(outcome.allowed)
        self.assertEqual(self.invoked, [])

    def test_confirmed_high_tool_can_run(self):
        calls = []
        permissions = PermissionManager(confirm_callback=lambda *a: calls.append(a) or True)
        executor = ToolExecutor(self._registry_with("HIGH"), permissions)
        outcome = executor.execute("run_system_command", {"cmd": "echo hi"})
        self.assertTrue(outcome.ok)
        self.assertEqual(self.invoked, ["echo hi"])
        self.assertEqual(len(calls), 1)

    def test_unknown_tool_is_a_real_error(self):
        executor = ToolExecutor(ToolRegistry(), PermissionManager())
        outcome = executor.execute("delete_everything", {})
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error, "未知工具")


class FailingMemory:
    def __init__(self):
        self.last_recall = {}

    def recall(self, *args, **kwargs):
        raise RuntimeError("数据库繁忙")

    def record(self, *args, **kwargs):
        raise RuntimeError("数据库繁忙")

    def record_usage(self, *args, **kwargs):
        raise RuntimeError("数据库繁忙")

    def get_preference(self, *args, **kwargs):
        raise RuntimeError("数据库繁忙")


class EchoProvider(ModelProvider):
    name = "echo"

    def __init__(self):
        self.seen_tools = []

    def is_available(self):
        return True

    def chat(self, messages, tools=None, temperature=0.7):
        self.seen_tools.append(tools)
        return ChatResult(ok=True, content="我在。", model="echo")

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


class StackProvider(ModelProvider):
    name = "stack"

    def __init__(self):
        self.calls = 0
        self.seen_tools = []

    def is_available(self):
        return True

    def chat(self, messages, tools=None, temperature=0.7):
        self.calls += 1
        self.seen_tools.append(tools)
        if self.calls == 1:
            return ChatResult(
                ok=True,
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="add_calendar_event",
                        arguments={"title": "喝水", "when": "2026-10-07 15:00"},
                    ),
                    ToolCall(
                        id="c2",
                        name="send_device_command",
                        arguments={"name": "mock-1", "command": {"servo": 30}},
                    ),
                ],
                usage=Usage(prompt_tokens=12, completion_tokens=6),
                model="stack",
            )
        return ChatResult(ok=True, content="日程记好了，舵机也转过去了。", model="stack")

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


class IntegrationTests(unittest.TestCase):
    def _stack(self, tmp):
        database = Database(Path(tmp) / "t.sqlite3")
        database.migrate()
        config = Config(load_env=False)
        audit = AuditLog(database)
        manager = DeviceManager(audit=audit)
        device = MockDevice(name="mock-1")
        manager.register(device)
        manager.connect("mock-1")
        executor = ToolExecutor(
            register_default_tools(ToolRegistry(), config, database, devices=manager),
            PermissionManager(audit=audit),
            audit=audit,
        )
        memory = MemoryManager(MemoryRepository(database))
        return database, audit, manager, device, executor, memory

    def test_full_stack_tool_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            database, audit, manager, device, executor, memory = self._stack(tmp)
            provider = StackProvider()
            agent = AgentCore(
                provider,
                memory,
                ContextManager(),
                executor=executor,
                devices=manager,
            )
            reply = agent.respond("s1", "帮我记个日程，再让舵机转到 30 度")
            self.assertTrue(reply.ok)
            self.assertIn("舵机", reply.content)

            rows = database.query("SELECT title FROM calendar_events")
            self.assertEqual([row["title"] for row in rows], ["喝水"])
            self.assertEqual(device.commands, [{"servo": 30}])

            kinds = {row["kind"] for row in audit.recent()}
            self.assertIn("tool_ok", kinds)
            self.assertIn("device_ok", kinds)

            saved = memory.recall("s1", "日程")
            self.assertTrue(saved[0])

    def test_unrelated_input_sends_no_tool_schemas(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, _, _, executor, memory = self._stack(tmp)
            provider = EchoProvider()
            agent = AgentCore(provider, memory, ContextManager(), executor=executor)
            reply = agent.respond("s1", "今天心情怎么样")
            self.assertTrue(reply.ok)
            self.assertEqual(reply.debug["tools_offered"], [])
            self.assertIn(provider.seen_tools[0], (None, []))

    def test_relevant_input_sends_only_matching_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, _, _, executor, memory = self._stack(tmp)
            provider = EchoProvider()
            agent = AgentCore(provider, memory, ContextManager(), executor=executor)
            # 设备状态工具需要参数，必须交给模型判断，所以这条还是走「按需发 schema」
            agent.respond("s1", "看一下设备状态")
            offered = provider.seen_tools[0]
            names = [item["function"]["name"] for item in offered]
            self.assertIn("get_device_state", names)
            self.assertNotIn("get_weather", names)
            self.assertLess(len(names), len(executor.registry.names()))

    def test_no_arg_read_tool_runs_without_asking_the_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, _, _, executor, memory = self._stack(tmp)
            provider = EchoProvider()
            agent = AgentCore(provider, memory, ContextManager(), executor=executor)
            reply = agent.respond("s1", "看一下有哪些设备")
            self.assertTrue(reply.ok)
            self.assertEqual(reply.debug["safe_tool"], "list_devices")
            self.assertEqual(reply.debug["tools_offered"], [])
            self.assertEqual(provider.seen_tools, [None])

    def test_service_failure_is_reported_not_faked(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, _, _, executor, _ = self._stack(tmp)
            outcome = executor.execute("get_news", {"topic": ""})
            self.assertFalse(outcome.ok)
            self.assertIn("未配置新闻 API Key", outcome.error)

    def test_memory_failure_degrades_gracefully(self):
        provider = EchoProvider()
        agent = AgentCore(provider, FailingMemory(), ContextManager())
        reply = agent.respond("s1", "你好")
        self.assertTrue(reply.ok)
        self.assertEqual(reply.content, "我在。")

    def test_model_failure_is_a_clear_error(self):
        class Broken(ModelProvider):
            name = "broken"

            def is_available(self):
                return True

            def chat(self, messages, tools=None, temperature=0.7):
                raise RuntimeError("连接被重置")

            def stream(self, messages, tools=None, temperature=0.7):
                yield ChatDelta(done=True)

        with tempfile.TemporaryDirectory() as tmp:
            _, _, _, _, executor, memory = self._stack(tmp)
            agent = AgentCore(Broken(), memory, ContextManager(), executor=executor)
            reply = agent.respond("s1", "在吗")
            self.assertFalse(reply.ok)
            self.assertIn("模型调用异常", reply.error)

    def test_critical_tool_requested_by_model_is_blocked_end_to_end(self):
        class AttackProvider(ModelProvider):
            name = "attack"

            def __init__(self):
                self.calls = 0

            def is_available(self):
                return True

            def chat(self, messages, tools=None, temperature=0.7):
                self.calls += 1
                if self.calls == 1:
                    return ChatResult(
                        ok=True,
                        tool_calls=[ToolCall(id="x", name="run_system_command", arguments={"cmd": "whoami"})],
                        model="attack",
                    )
                return ChatResult(ok=True, content="好的。", model="attack")

            def stream(self, messages, tools=None, temperature=0.7):
                yield ChatDelta(done=True)

        with tempfile.TemporaryDirectory() as tmp:
            _, _, _, _, executor, memory = self._stack(tmp)
            invoked = []
            executor.registry.register(
                Tool(
                    name="run_system_command",
                    description="执行系统命令",
                    schema={"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]},
                    permission="CRITICAL",
                    handler=lambda cmd: invoked.append(cmd) or "done",
                )
            )
            agent = AgentCore(AttackProvider(), memory, ContextManager(), executor=executor)
            reply = agent.respond("s1", "帮我看看设备")
            self.assertEqual(invoked, [])
            self.assertEqual(reply.debug["tool_results"][0]["allowed"], False)



    def test_the_runtime_probe_never_shells_out_or_installs(self):
        """探测只读状态，不起 shell，也不替用户装任何东西。"""
        text = (ROOT / "adapters" / "local_runtimes.py").read_text(encoding="utf-8")
        for needle in ("shell=True", "os.system", "pip install", "eval(", "exec("):
            self.assertNotIn(needle, text, needle)
        self.assertIn("subprocess.run", text)
        # 参数一律是 argv 列表，且带超时。
        self.assertIn("timeout=timeout", text)

if __name__ == "__main__":
    unittest.main()
