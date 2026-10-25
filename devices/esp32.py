"""ESP32 适配层。当前不接真实硬件，默认只允许模拟传输。

安全边界（当前阶段不可违反）：
    真实传输必须显式实现并显式允许（allow_real），DeviceManager 默认拒绝注册非模拟设备。
    命令必须先过白名单校验，由程序校验，模型只能请求，不能直接下命令。
    所有命令都经过 DeviceManager，进审计日志。

以后接真机：写一个 Transport（串口或网络）并把 allow_real 打开即可，
界面、工具与 Agent Core 都不用改。
"""

import json
import os
from abc import ABC, abstractmethod

from devices.interface import DeviceAdapter
from devices.ports import open_port, open_raw_serial

CAPABILITIES = ("servo", "led", "move", "distance")
DIRECTIONS = ("forward", "back", "left", "right")
SERVO_MIN = 0
SERVO_MAX = 180
SPEED_MIN = 0
SPEED_MAX = 100


class Esp32Transport(ABC):
    """ESP32 的传输层。模拟传输与真实传输的区别只有这一个标记。"""

    name = "base"
    is_simulated = False

    @abstractmethod
    def open(self):
        """打开传输。"""

    @abstractmethod
    def close(self):
        """关闭传输。"""

    @abstractmethod
    def write(self, line):
        """发一行命令。"""

    @abstractmethod
    def read(self):
        """读回设备发来的行。"""


class LoopbackEsp32Transport(Esp32Transport):
    """内存回环传输：命令只记下来，不开端口、不联网、不碰硬件。"""

    name = "loopback"
    is_simulated = True

    def __init__(self):
        self.opened = False
        self.sent = []
        self.inbox = []

    def open(self):
        self.opened = True
        return {"ok": True, "simulated": True, "state": "opened"}

    def close(self):
        self.opened = False
        return {"ok": True, "simulated": True, "state": "closed"}

    def write(self, line):
        if not self.opened:
            return {"ok": False, "simulated": True, "error": "传输还没有打开"}
        self.sent.append(str(line))
        return {"ok": True, "simulated": True, "sent": str(line), "count": len(self.sent)}

    def read(self):
        """把已经收到的行取走。读过的就不再留着，免得下一单把旧回复当成本次结果。"""
        lines, self.inbox = list(self.inbox), []
        return lines

    def push(self, line):
        """给测试用：假装设备回了一行。"""
        self.inbox.append(str(line))
        return line


# 老名字留着：外部（含设备清单那条路）还按这个名字引用。
# 真正的实现挪到了 devices/ports.py，串口枚举和打开放一起更好找。
_open_serial = open_raw_serial


class SerialEsp32Transport(Esp32Transport):
    """真实串口传输。没给端口名就什么都不连，绝不自己猜一个口。

    打开后读写都不阻塞：读不到数据返回空列表，不当成错误。
    pyserial 在的话用它开，波特率一并设好；不在就退回系统自带的开法，
    这时设不了波特率，会如实标出来而不是假装设过。
    """

    name = "serial"
    is_simulated = False

    def __init__(self, port="", baudrate=115200, opener=None, logger=None, chunk=4096):
        self.port = str(port or "").strip()
        self.baudrate = int(baudrate or 115200)
        self._opener = opener or open_port
        self.logger = logger
        self.chunk = max(64, int(chunk))
        self._raw = None
        self.opened = False
        self.non_blocking = False
        self.baud_set = False
        self.buffer = b""
        self.writes = 0
        self.reads = 0
        self.errors = 0
        self.last_error = ""

    def open(self):
        if self.opened:
            return {"ok": True, "port": self.port, "state": "already_open"}
        if not self.port:
            return {"ok": False, "error": "没有配置串口名，例如 COM3"}
        try:
            try:
                self._raw = self._opener(self.port, self.baudrate)
            except TypeError:
                # 调用方给的是老式只吃一个参数的 opener。
                self._raw = self._opener(self.port)
        except Exception as exc:
            self._fail("打不开串口 %s：%s" % (self.port, exc))
            return {"ok": False, "error": self.last_error}
        self.opened = True
        self.baud_set = bool(getattr(self._raw, "baud_set", False))
        marked = getattr(self._raw, "non_blocking", None)
        if marked is None:
            # pyserial 的对象没有这个标记，看它的 timeout：0 就是非阻塞。
            timeout = getattr(self._raw, "timeout", None)
            marked = timeout == 0
        self.non_blocking = bool(marked)
        if self.non_blocking:
            note = "波特率已设为 %d" % self.baudrate if self.baud_set else "波特率沿用系统设置"
        else:
            note = "系统不支持非阻塞读，已停用读取"
        return {
            "ok": True,
            "port": self.port,
            "baudrate": self.baudrate,
            "state": "opened",
            "non_blocking": self.non_blocking,
            "baud_set": self.baud_set,
            "backend": str(getattr(self._raw, "backend", "系统自带")),
            "note": note,
        }

    def close(self):
        handle, self._raw = self._raw, None
        self.opened = False
        if handle is None:
            return {"ok": True, "state": "already_closed"}
        try:
            handle.close()
        except Exception as exc:
            self._fail("关串口失败：%s" % exc)
            return {"ok": False, "error": self.last_error}
        return {"ok": True, "state": "closed"}

    def write(self, line):
        if not self.opened or self._raw is None:
            return {"ok": False, "error": "传输还没有打开"}
        payload = (str(line).strip() + "\n").encode("utf-8")
        try:
            self._raw.write(payload)
            self._raw.flush()
        except Exception as exc:
            self._fail("写串口失败：%s" % exc)
            return {"ok": False, "error": self.last_error}
        self.writes += 1
        return {"ok": True, "sent": str(line), "count": self.writes}

    def read(self):
        """读回设备发来的行。读不到就是空列表，不编数据。"""
        if not self.opened or self._raw is None:
            return []
        if not self.non_blocking:
            # 阻塞读会把桌宠卡住；宁可读不到，并把这个原因留在 health 里。
            return []
        try:
            chunk = self._raw.read(self.chunk)
        except BlockingIOError:
            return []
        except Exception as exc:
            self._fail("读串口失败：%s" % exc)
            return []
        if not chunk:
            return []
        self.buffer += chunk
        lines = []
        while b"\n" in self.buffer:
            raw, _, self.buffer = self.buffer.partition(b"\n")
            text = raw.decode("utf-8", errors="replace").strip()
            if text:
                lines.append(text)
        self.reads += len(lines)
        return lines

    def health(self):
        """通信状态。没有回应时明确说没有，不返回假读数。"""
        return {
            "port": self.port or "(未配置)",
            "baudrate": self.baudrate,
            "open": self.opened,
            "non_blocking": self.non_blocking,
            "baud_set": bool(self.baud_set),
            "backend": str(getattr(self._raw, "backend", "系统自带")) if self._raw else "",
            "writes": self.writes,
            "lines_read": self.reads,
            "errors": self.errors,
            "last_error": self.last_error,
            "note": "" if self.non_blocking or not self.opened else "系统不支持非阻塞读，已停用读取",
        }

    def describe(self):
        data = self.health()
        data["simulated"] = False
        return data

    def _fail(self, message):
        self.errors += 1
        self.last_error = message
        if self.logger:
            self.logger.warning("esp32 serial: %s", message)


def build_transport(options=None, logger=None):
    """按设备清单里的 options 造传输。没写端口就给模拟回环，绝不偷偷连真机。"""
    options = options if isinstance(options, dict) else {}
    port = str(options.get("port") or "").strip()
    if not port:
        return LoopbackEsp32Transport()
    try:
        baudrate = int(options.get("baudrate") or 115200)
    except (TypeError, ValueError):
        baudrate = 115200
    return SerialEsp32Transport(port=port, baudrate=baudrate, logger=logger)

def validate_command(command):
    """校验一条设备命令，返回 (发送内容, 错误)。程序负责校验，不靠模型自觉。"""
    if not isinstance(command, dict) or not command:
        return None, "命令必须是非空对象"
    name = str(command.get("cmd") or "").strip().lower()
    if name not in CAPABILITIES:
        return None, f"不支持的命令：{name or '(空)'}"
    payload = {"cmd": name}
    if name == "servo":
        try:
            angle = int(command.get("angle"))
        except (TypeError, ValueError):
            return None, "servo 需要 angle 数字"
        if not SERVO_MIN <= angle <= SERVO_MAX:
            return None, f"angle 必须在 {SERVO_MIN}-{SERVO_MAX} 之间"
        payload["angle"] = angle
    elif name == "led":
        payload["on"] = bool(command.get("on"))
    elif name == "move":
        direction = str(command.get("direction") or "").strip().lower()
        if direction not in DIRECTIONS:
            return None, f"direction 必须是 {'/'.join(DIRECTIONS)} 之一"
        try:
            speed = int(command.get("speed", 50))
        except (TypeError, ValueError):
            return None, "speed 需要数字"
        payload["direction"] = direction
        payload["speed"] = max(SPEED_MIN, min(SPEED_MAX, speed))
    return json.dumps(payload, ensure_ascii=False), ""


class Esp32Adapter(DeviceAdapter):
    """ESP32 设备。传输是模拟的就算模拟设备，真机必须显式允许。"""

    def __init__(self, name="esp32-1", transport=None, logger=None):
        self.name = str(name)
        self.transport = transport or LoopbackEsp32Transport()
        self.logger = logger
        self.connected = False
        self.commands = []
        self.last_command = None
        self.last_reading = None

    @property
    def is_mock(self):
        return bool(getattr(self.transport, "is_simulated", False))

    # ---------- DeviceAdapter ----------
    def connect(self):
        try:
            result = self.transport.open() or {}
        except Exception as exc:
            return {"ok": False, "device": self.name, "error": f"连接失败：{exc}"}
        self.connected = bool(result.get("ok"))
        return {
            "ok": self.connected,
            "device": self.name,
            "simulated": self.is_mock,
            "transport": getattr(self.transport, "name", "?"),
            "state": "connected" if self.connected else "failed",
        }

    def disconnect(self):
        try:
            result = self.transport.close() or {}
        except Exception as exc:
            return {"ok": False, "device": self.name, "error": f"断开失败：{exc}"}
        self.connected = False
        return {
            "ok": bool(result.get("ok")),
            "device": self.name,
            "simulated": self.is_mock,
            "state": "disconnected",
        }

    def get_state(self):
        return {
            "device": self.name,
            "kind": "esp32",
            "simulated": self.is_mock,
            "transport": getattr(self.transport, "name", "?"),
            "health": self.transport_health(),
            "connected": self.connected,
            "capabilities": list(CAPABILITIES),
            "command_count": len(self.commands),
            "last_command": dict(self.last_command) if self.last_command else None,
            "last_reading": dict(self.last_reading) if self.last_reading else None,
        }

    def transport_health(self):
        """传输层自己报的通信状态；没有这一层就返回空。"""
        probe = getattr(self.transport, "health", None)
        if not callable(probe):
            return None
        try:
            return dict(probe())
        except Exception as exc:
            return {"error": str(exc)}

    def execute(self, command):
        if not self.connected:
            return {"ok": False, "device": self.name, "error": "设备未连接"}
        line, error = validate_command(command)
        if error:
            return {"ok": False, "device": self.name, "error": error}
        try:
            sent = self.transport.write(line) or {}
        except Exception as exc:
            return {"ok": False, "device": self.name, "error": f"发送失败：{exc}"}
        if not sent.get("ok"):
            return {"ok": False, "device": self.name, "error": sent.get("error", "发送失败")}
        self.last_command = dict(command)
        self.commands.append(dict(command))
        result = {
            "ok": True,
            "device": self.name,
            "simulated": self.is_mock,
            "sent": line,
            "command_count": len(self.commands),
        }
        if str(command.get("cmd")).lower() == "distance":
            result.update(self._distance())
        return result

    # ---------- 内部 ----------
    def _distance(self):
        """读数只来自设备。读不到就如实说读不到，绝不编一个数字。"""
        lines = []
        try:
            lines = list(self.transport.read() or [])
        except Exception:
            lines = []
        for line in reversed(lines):
            try:
                data = json.loads(line)
            except Exception:
                continue
            if isinstance(data, dict) and "distance_cm" in data:
                self.last_reading = {"distance_cm": data.get("distance_cm")}
                return {"distance_cm": data.get("distance_cm")}
        self.last_reading = {"distance_cm": None}
        return {
            "distance_cm": None,
            "note": "设备没有回报距离，读数留空，不编造数值",
        }
