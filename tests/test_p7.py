"""P7 验收：设备接口、MockDevice、DeviceManager、设备工具与端到端调用。"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.context import ContextManager
from agent.core import AgentCore
from database.db import Database
from devices.interface import DeviceAdapter, MockDevice
from devices.manager import DeviceManager
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatDelta, ChatResult, ModelProvider, ToolCall, Usage
from security.audit import AuditLog
from security.config import Config
from tools.builtin import register_default_tools
from tools.permission import PermissionManager
from tools.registry import ToolExecutor, ToolRegistry


class RealDevice(DeviceAdapter):
    """测试用：假装是真实硬件，应当被默认拒绝。"""

    name = "real-1"
    is_mock = False

    def connect(self):
        return {"ok": True}

    def disconnect(self):
        return {"ok": True}

    def get_state(self):
        return {"ok": True, "device": self.name}

    def execute(self, command):
        return {"ok": True}


class ExplodingDevice(MockDevice):
    def execute(self, command):
        raise RuntimeError("硬件没有回应")


class MockDeviceTests(unittest.TestCase):
    def test_execute_requires_connection(self):
        device = MockDevice()
        result = device.execute({"servo": 90})
        self.assertFalse(result["ok"])
        self.assertIn("未连接", result["error"])
        self.assertEqual(device.commands, [])

    def test_execute_after_connect_records_command(self):
        device = MockDevice(name="mock-1")
        device.connect()
        result = device.execute({"servo": 90})
        self.assertTrue(result["ok"])
        self.assertTrue(result["simulated"])
        self.assertEqual(result["echo"], {"servo": 90})
        self.assertEqual(device.commands, [{"servo": 90}])
        self.assertEqual(device.last_command, {"servo": 90})

    def test_empty_command_is_rejected(self):
        device = MockDevice()
        device.connect()
        self.assertFalse(device.execute({})["ok"])
        self.assertFalse(device.execute(None)["ok"])

    def test_state_reports_connection(self):
        device = MockDevice(capabilities=("servo", "sensor"))
        self.assertFalse(device.get_state()["connected"])
        device.connect()
        state = device.get_state()
        self.assertTrue(state["connected"])
        self.assertEqual(state["capabilities"], ["servo", "sensor"])
        device.disconnect()
        self.assertFalse(device.get_state()["connected"])


class DeviceManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Database(Path(self.tmp.name) / "t.sqlite3")
        self.database.migrate()
        self.audit = AuditLog(self.database)
        self.manager = DeviceManager(audit=self.audit)

    def test_real_device_rejected_by_default(self):
        result = self.manager.register(RealDevice())
        self.assertFalse(result["ok"])
        self.assertIn("禁止接入真实设备", result["error"])
        self.assertEqual(self.manager.names(), [])

    def test_real_device_allowed_when_opened(self):
        manager = DeviceManager(allow_real=True)
        self.assertTrue(manager.register(RealDevice())["ok"])
        self.assertEqual(manager.names(), ["real-1"])

    def test_register_requires_name(self):
        device = MockDevice(name="")
        self.assertFalse(self.manager.register(device)["ok"])

    def test_mock_device_lifecycle(self):
        self.manager.register(MockDevice(name="mock-1"))
        self.assertTrue(self.manager.connect("mock-1")["ok"])
        self.assertTrue(self.manager.execute("mock-1", {"servo": 45})["ok"])
        state = self.manager.get_state("mock-1")
        self.assertTrue(state["ok"])
        self.assertEqual(state["device"]["command_count"], 1)
        self.assertTrue(self.manager.disconnect("mock-1")["ok"])

    def test_missing_device_returns_real_error(self):
        result = self.manager.execute("nope", {"servo": 1})
        self.assertFalse(result["ok"])
        self.assertIn("没有这个设备", result["error"])

    def test_adapter_exception_becomes_real_error(self):
        self.manager.register(ExplodingDevice(name="boom"))
        self.manager.connect("boom")
        result = self.manager.execute("boom", {"servo": 1})
        self.assertFalse(result["ok"])
        self.assertIn("硬件没有回应", result["error"])

    def test_device_calls_are_audited(self):
        self.manager.register(MockDevice(name="mock-1"))
        self.manager.connect("mock-1")
        rows = self.audit.recent()
        self.assertTrue(any(row["level"] == "DEVICE" for row in rows))
        kinds = {row["kind"] for row in rows}
        self.assertIn("device_ok", kinds)

    def test_state_of_all_devices(self):
        self.manager.register(MockDevice(name="a"))
        self.manager.register(MockDevice(name="b"))
        result = self.manager.get_state()
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["devices"]), 2)


class DeviceToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Database(Path(self.tmp.name) / "t.sqlite3")
        self.database.migrate()
        self.config = Config(load_env=False)
        self.manager = DeviceManager()
        self.manager.register(MockDevice(name="mock-1"))
        self.registry = ToolRegistry()
        register_default_tools(self.registry, self.config, self.database, devices=self.manager)

    def test_device_tools_registered_with_permissions(self):
        levels = {tool.name: tool.permission for tool in self.registry.all()}
        self.assertEqual(levels["list_devices"], "READ")
        self.assertEqual(levels["get_device_state"], "READ")
        self.assertEqual(levels["send_device_command"], "LOW")

    def test_relevance_picks_device_tools(self):
        self.assertIn("send_device_command", self.registry.relevant("让舵机转一下"))
        self.assertIn("list_devices", self.registry.relevant("有哪些设备"))

    def test_send_command_needs_connection(self):
        executor = ToolExecutor(self.registry, PermissionManager())
        outcome = executor.execute("send_device_command", {"name": "mock-1", "command": {"servo": 90}})
        self.assertFalse(outcome.ok)
        self.assertIn("未连接", outcome.error)

    def test_send_command_after_connect(self):
        self.manager.connect("mock-1")
        executor = ToolExecutor(self.registry, PermissionManager())
        outcome = executor.execute("send_device_command", {"name": "mock-1", "command": {"servo": 90}})
        self.assertTrue(outcome.ok)
        self.assertIn("simulated", outcome.result)

    def test_command_requires_confirmation_when_raised(self):
        manager = DeviceManager()
        manager.register(MockDevice(name="mock-1"))
        manager.connect("mock-1")
        registry = ToolRegistry()
        register_default_tools(registry, self.config, self.database, devices=manager)
        strict = PermissionManager(auto_allow_max="READ")
        executor = ToolExecutor(registry, strict)
        outcome = executor.execute("send_device_command", {"name": "mock-1", "command": {"servo": 90}})
        self.assertFalse(outcome.allowed)
        self.assertFalse(outcome.ok)

    def test_unknown_argument_is_rejected(self):
        executor = ToolExecutor(self.registry, PermissionManager())
        outcome = executor.execute("get_device_state", {"name": "mock-1", "foo": 1})
        self.assertFalse(outcome.ok)
        self.assertIn("未声明参数", outcome.error)


class DeviceThroughAgentTests(unittest.TestCase):
    class DeviceProvider(ModelProvider):
        name = "device-stub"

        def __init__(self):
            self.calls = 0

        def is_available(self):
            return True

        def chat(self, messages, tools=None, temperature=0.7):
            self.calls += 1
            if self.calls == 1:
                return ChatResult(
                    ok=True,
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="send_device_command",
                            arguments={"name": "mock-1", "command": {"servo": 90}},
                        )
                    ],
                    usage=Usage(prompt_tokens=8, completion_tokens=3),
                    model="stub",
                )
            return ChatResult(ok=True, content="舵机已经转到 90 度。", model="stub")

        def stream(self, messages, tools=None, temperature=0.7):
            yield ChatDelta(done=True)

    def test_agent_reaches_mock_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Database(Path(tmp) / "t.sqlite3")
            database.migrate()
            config = Config(load_env=False)

            manager = DeviceManager()
            device = MockDevice(name="mock-1")
            manager.register(device)
            manager.connect("mock-1")

            audit = AuditLog(database)
            executor = ToolExecutor(
                register_default_tools(ToolRegistry(), config, database, devices=manager),
                PermissionManager(audit=audit),
                audit=audit,
            )
            memory = MemoryManager(MemoryRepository(database))
            agent = AgentCore(
                self.DeviceProvider(),
                memory,
                ContextManager(),
                executor=executor,
                devices=manager,
            )

            reply = agent.respond("s1", "让舵机转到 90 度")
            self.assertTrue(reply.ok)
            self.assertEqual(device.commands, [{"servo": 90}])
            self.assertEqual(reply.debug["devices"]["names"], ["mock-1"])


if __name__ == "__main__":
    unittest.main()
