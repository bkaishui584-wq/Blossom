"""设备接口。当前阶段只提供接口与 Mock 实现，禁止连接真实硬件。"""

from abc import ABC, abstractmethod


class DeviceAdapter(ABC):
    """外部设备接入点。ESP32、机器人、传感器都实现这四个方法。"""

    name = "base"
    is_mock = False

    @abstractmethod
    def connect(self):
        """建立连接。"""

    @abstractmethod
    def disconnect(self):
        """断开连接。"""

    @abstractmethod
    def get_state(self):
        """返回设备状态。"""

    @abstractmethod
    def execute(self, command):
        """执行一条命令。"""


class MockDevice(DeviceAdapter):
    """内存假设备。只记录命令，绝不碰真实硬件。"""

    is_mock = True

    def __init__(self, name="mock-1", capabilities=()):
        self.name = name
        self.capabilities = tuple(capabilities)
        self.connected = False
        self.commands = []
        self.last_command = None

    def connect(self):
        self.connected = True
        return {"ok": True, "simulated": True, "device": self.name, "state": "connected"}

    def disconnect(self):
        self.connected = False
        return {"ok": True, "simulated": True, "device": self.name, "state": "disconnected"}

    def get_state(self):
        return {
            "device": self.name,
            "kind": "mock",
            "simulated": True,
            "connected": self.connected,
            "capabilities": list(self.capabilities),
            "command_count": len(self.commands),
            "last_command": dict(self.last_command) if self.last_command else None,
        }

    def execute(self, command):
        if not self.connected:
            return {
                "ok": False,
                "simulated": True,
                "device": self.name,
                "error": "设备未连接",
            }
        if not isinstance(command, dict) or not command:
            return {
                "ok": False,
                "simulated": True,
                "device": self.name,
                "error": "命令必须是非空对象",
            }
        self.last_command = dict(command)
        self.commands.append(dict(command))
        return {
            "ok": True,
            "simulated": True,
            "device": self.name,
            "echo": dict(command),
            "note": "这是模拟设备，没有真实硬件被驱动",
        }
