"""设备适配接口：连接、断开、状态、发一个已登记的动作。

电脑端只发登记过的结构化动作，收到设备的真实确认才算完成。只发出去就当成功是
不允许的：从 accepted 到 completed 中间要等设备自己说话。

两个实现：
    LoopbackProvider  内存回环 + 一台模拟固件，没有硬件时用它把链路跑通
    SerialProvider    USB 串口，真机走这条；没配端口、没打开 allow_real 就连不上
"""

import time
from abc import abstractmethod

from devices import protocol
from devices.actions import DeviceCommandRegistry, copy_limits
from devices.esp32 import Esp32Transport, LoopbackEsp32Transport, _open_serial
from devices.interface import DeviceAdapter
from devices.simulator import DeviceSimulator

POLL_SECONDS = 0.02
DEFAULT_TIMEOUT = 3.0
HISTORY_LIMIT = 40
# 设备主动上报的事件最多留多少条，给界面和诊断用。
EVENT_LIMIT = 40
_ID_SAFE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def safe_device_id(name, fallback="esp32"):
    """协议里的 device_id 只认字母数字下划线连字符，中文名要转一道。"""
    text = "".join(char for char in str(name or "") if char in _ID_SAFE)
    text = text.strip("_-")
    return (text or fallback)[:64]


class DeviceProvider(DeviceAdapter):
    """统一设备适配接口。子类只要实现连、断、读、写，确认逻辑在这里统一走。"""

    kind = "provider"
    is_mock = True
    transport_name = "?"

    def __init__(
        self,
        name="device-1",
        capabilities=(),
        limits=None,
        registry=None,
        logger=None,
        clock=time.monotonic,
        on_event=None,
    ):
        self.name = str(name or "device-1")
        self.capabilities = tuple(str(item) for item in (capabilities or ()))
        self.limits = copy_limits(limits)
        self.registry = registry or DeviceCommandRegistry()
        self.logger = logger
        self.clock = clock
        # 设备主动上报交给外面处理（事件桥），这一层不自己广播。
        self.on_event = on_event
        self.events = []
        self.connected = False
        self.firmware_version = ""
        self.last_reply = None
        self.last_status = ""
        self.last_error = ""
        # 心跳与重连：只报事实，没连上就说没连上。
        self.heartbeats = 0
        self.heartbeat_failures = 0
        self.heartbeat_ok = False
        self.last_heartbeat_at = 0.0
        self.reconnects = 0
        self.reconnect_failures = 0
        self.history = []
        self.sent = 0
        self.stray = 0

    # ---------- 子类要实现的四件事 ----------
    @abstractmethod
    def _open_transport(self):
        """打开传输。返回 {ok, error}。"""

    @abstractmethod
    def _close_transport(self):
        """关掉传输。返回 {ok, error}。"""

    @abstractmethod
    def _write_line(self, line):
        """把一行发出去。返回 {ok, error}。"""

    @abstractmethod
    def _read_lines(self):
        """把设备已经发回来的行全部取走。读不到就是空列表。"""

    # ---------- 公共流程 ----------
    def connect(self):
        result = self._open_transport() or {}
        if not result.get("ok"):
            self.connected = False
            self.last_error = str(result.get("error") or "连接失败")
            return {"ok": False, "device": self.name, "error": self.last_error}
        self.connected = True
        probe = self._learn_from_device()
        return {
            "ok": True,
            "device": self.name,
            "simulated": bool(self.is_mock),
            "transport": self.transport_name,
            "firmware": self.firmware_version,
            "capabilities": list(self.capabilities),
            "limits": copy_limits(self.limits),
            "probe": probe,
        }

    def disconnect(self):
        result = self._close_transport() or {}
        self.connected = False
        return {
            "ok": bool(result.get("ok", True)),
            "device": self.name,
            "simulated": bool(self.is_mock),
            "state": "disconnected",
            "error": str(result.get("error") or ""),
        }

    def get_state(self):
        return {
            "device": self.name,
            "kind": self.kind,
            "simulated": bool(self.is_mock),
            "transport": self.transport_name,
            "connected": bool(self.connected),
            "firmware": self.firmware_version,
            "capabilities": list(self.capabilities),
            "limits": copy_limits(self.limits),
            "actions": self.registry.available_for(self.capabilities, self.limits),
            "last_status": self.last_status,
            "last_reply": dict(self.last_reply) if isinstance(self.last_reply, dict) else None,
            "last_error": self.last_error,
            "sent": self.sent,
            "stray": self.stray,
            "heartbeat": self.heartbeat_state(),
            "events": len(self.events),
            "health": self.health(),
        }

    def health(self):
        """传输层自报的通信状态；没有就如实返回空。"""
        probe = getattr(self, "_transport_health", None)
        if not callable(probe):
            return None
        try:
            return dict(probe())
        except Exception as exc:
            return {"error": str(exc)}

    def execute(self, command):
        """兼容老的 {"cmd": ...} 入口：一律转成结构化动作。"""
        if not isinstance(command, dict) or not command:
            return {"ok": False, "device": self.name, "error": "命令必须是非空对象"}
        if "action" in command:
            action = command.get("action")
            params = command.get("params") or {}
        else:
            action = command.get("cmd")
            params = {key: value for key, value in command.items() if key != "cmd"}
        return self.send(action, params)

    # ---------- 心跳与重连 ----------
    def heartbeat(self, timeout=None):
        """问一句设备还在不在。没连上就直说，不假装问过。"""
        self.last_heartbeat_at = self.clock()
        if not self.connected:
            self.heartbeat_ok = False
            self.heartbeat_failures += 1
            return {
                "ok": False,
                "device": self.name,
                "error": "设备未连接",
                "error_code": protocol.ERROR_NOT_CONNECTED,
            }
        spec = self.registry.get("device.get_status") if self.registry else None
        window = float(timeout or (spec.timeout if spec else DEFAULT_TIMEOUT))
        reply = self.send("device.get_status", {}, timeout=window)
        self.heartbeat_ok = bool(reply.get("ok"))
        if self.heartbeat_ok:
            self.heartbeats += 1
        else:
            self.heartbeat_failures += 1
        return {
            "ok": self.heartbeat_ok,
            "device": self.name,
            "status": reply.get("status", ""),
            "error": reply.get("error", ""),
            "error_code": reply.get("error_code", ""),
        }

    def heartbeat_state(self):
        return {
            "ok": bool(self.heartbeat_ok),
            "beats": self.heartbeats,
            "failures": self.heartbeat_failures,
            "last_at": round(float(self.last_heartbeat_at or 0.0), 3),
            "reconnects": self.reconnects,
            "reconnect_failures": self.reconnect_failures,
        }

    def reconnect(self, attempts=2, delay=0.3):
        """断线重连。次数有限，不在后台自己狂试。"""
        tries = max(1, int(attempts or 1))
        pause = max(0.0, min(2.0, float(delay or 0.0)))
        report = []
        for index in range(tries):
            if index and pause:
                time.sleep(pause)
            try:
                self._close_transport()
            except Exception:
                pass
            self.connected = False
            outcome = self.connect() or {}
            report.append(
                {
                    "attempt": index + 1,
                    "ok": bool(outcome.get("ok")),
                    "error": str(outcome.get("error") or ""),
                }
            )
            if outcome.get("ok"):
                self.reconnects += 1
                self.heartbeat_ok = False
                return {"ok": True, "device": self.name, "attempts": report}
        self.reconnect_failures += 1
        self._note("重连失败，试了 %d 次" % tries)
        return {
            "ok": False,
            "device": self.name,
            "attempts": report,
            "error": (report[-1]["error"] if report else "") or "重连没有成功",
        }

    # ---------- 发一条动作 ----------
    def send(self, action, params=None, timeout=None, check_capabilities=True):
        """发一个动作，等设备自己回确认。没等到就返回超时，绝不假装成功。"""
        if not self.connected:
            return self._failup("设备未连接", code=protocol.ERROR_NOT_CONNECTED)
        spec = self.registry.get(action)
        if spec is None:
            return self._failup(
                "没有登记这个动作：%s" % (action or "(空)"),
                code=protocol.ERROR_UNKNOWN_COMMAND,
            )
        clean, error = self.registry.validate(
            action,
            params,
            limits=self.limits,
            # 握手那一问要跳过能力校验：那会儿还不知道设备有什么能力。
            capabilities=self.capabilities if check_capabilities else None,
        )
        if error:
            return self._failup(error, action=action, code=protocol.ERROR_BAD_PARAM)
        frame = protocol.make_request(self.device_id, action, clean)
        try:
            line = protocol.encode(frame)
        except protocol.ProtocolError as exc:
            return self._failup(
                "报文没发出去：%s" % exc, action=action, code=protocol.ERROR_INTERNAL
            )
        written = self._write_line(line) or {}
        if not written.get("ok"):
            return self._failup(
                str(written.get("error") or "发送失败"),
                action=action,
                code=protocol.ERROR_NOT_CONNECTED,
            )
        self.sent += 1
        return self._await(frame, action, clean, timeout=timeout, fallback=float(spec.timeout or DEFAULT_TIMEOUT))

    def _await(self, frame, action, params, timeout=None, fallback=DEFAULT_TIMEOUT):
        deadline = self.clock() + max(0.05, float(timeout or fallback))
        progress = None
        while self.clock() <= deadline:
            for raw in self._read_lines() or []:
                data = self._match(raw, frame)
                if data is None:
                    continue
                status = str(data.get("status") or "")
                if not protocol.is_final(status):
                    progress = data
                    continue
                return self._settle(action, params, frame, status, data)
            if not self.connected:
                return self._settle(
                    action, params, frame, protocol.STATUS_DISCONNECTED, None,
                    error="通信中断，设备没有再回话",
                )
            time.sleep(POLL_SECONDS)
        return self._settle(
            action, params, frame, protocol.STATUS_TIMEOUT, progress,
            error="等了 %.1f 秒也没等到设备确认" % max(0.05, float(timeout or fallback)),
        )

    def _match(self, raw, frame):
        """认出这一单的回复。别人的回复只记一笔，不认领。"""
        try:
            data = protocol.decode(raw)
        except protocol.ProtocolError as exc:
            self.stray += 1
            self._note("收到看不懂的一行：%s" % exc)
            return None
        if protocol.is_event(data):
            # 设备主动说话。它不属于任何一单，别当成串单。
            self._accept_event(data)
            return None
        if data.get("message_id") != frame["message_id"]:
            self.stray += 1
            return None
        problems = protocol.check_reply(data, frame["message_id"], frame["device_id"])
        if problems:
            self.stray += 1
            self._note("回复不合协议：%s" % "；".join(problems))
            return None
        return data

    def _accept_event(self, data):
        """收下一帧设备主动上报。合协议才往外交，不合就记一笔。"""
        device = str(data.get("device_id") or "")
        if device and device != self.device_id:
            self.stray += 1
            self._note("事件不是这台设备发的：%s" % device)
            return None
        problems = protocol.check_event(data, self.device_id)
        if problems:
            self.stray += 1
            self._note("设备上报的事件不合协议：%s" % "；".join(problems))
            return None
        return self._deliver_event(data)

    def _deliver_event(self, data):
        frame = dict(data or {})
        self.events.append(frame)
        if len(self.events) > EVENT_LIMIT:
            del self.events[: len(self.events) - EVENT_LIMIT]
        handler = self.on_event
        if not callable(handler):
            return frame
        try:
            handler(self.name, frame)
        except Exception as exc:
            self._note("事件交出去时出错：%s" % exc)
        return frame

    def poll(self):
        """把设备已经发来的行读干净：事件交出去，对不上单的记一笔。

        没有正在等回复的时候也要偶尔收一次，不然设备按了按钮电脑端要等到下一次
        发指令才知道。
        """
        taken = []
        for raw in self._read_lines() or []:
            try:
                data = protocol.decode(raw)
            except protocol.ProtocolError as exc:
                self.stray += 1
                self._note("收到看不懂的一行：%s" % exc)
                continue
            if protocol.is_event(data):
                frame = self._accept_event(data)
                if frame is not None:
                    taken.append(frame)
                continue
            self.stray += 1
        return taken

    def _settle(self, action, params, frame, status, data, error=""):
        result = dict(data.get("result") or {}) if isinstance(data, dict) else {}
        message = error or str((data or {}).get("error") or "")
        code = str((data or {}).get("error_code") or "")
        if not code and status != protocol.STATUS_COMPLETED:
            code = (
                protocol.ERROR_TIMEOUT
                if status == protocol.STATUS_TIMEOUT
                else protocol.ERROR_NOT_CONNECTED
                if status == protocol.STATUS_DISCONNECTED
                else protocol.ERROR_INTERNAL
            )
        reply = {
            "ok": status == protocol.STATUS_COMPLETED,
            "device": self.name,
            "simulated": bool(self.is_mock),
            "action": action,
            "params": dict(params or {}),
            "status": status,
            "status_label": protocol.status_label(status),
            "message_id": frame["message_id"],
            "result": result,
            "error": message,
            "error_code": code,
        }
        self.last_reply = reply
        self.last_status = status
        self.last_error = "" if reply["ok"] else message
        self._record(reply)
        self._emit(reply)
        return reply

    def _record(self, reply):
        self.history.append(
            {
                "action": reply.get("action"),
                "params": reply.get("params"),
                "status": reply.get("status"),
                "error": reply.get("error"),
                "at": self.clock(),
            }
        )
        if len(self.history) > HISTORY_LIMIT:
            del self.history[: len(self.history) - HISTORY_LIMIT]

    def _emit(self, reply):
        """状态变化交给外面（事件桥）去广播，这里只留一个钩子。"""
        return reply

    def _failup(self, message, action="", code=protocol.ERROR_INTERNAL):
        self.last_error = str(message)
        reply = {
            "ok": False,
            "device": self.name,
            "simulated": bool(self.is_mock),
            "action": action,
            "status": protocol.STATUS_FAILED,
            "status_label": protocol.status_label(protocol.STATUS_FAILED),
            "result": {},
            "error": str(message),
            "error_code": str(code or ""),
        }
        self.last_reply = reply
        self.last_status = protocol.STATUS_FAILED
        self._record(reply)
        self._emit(reply)
        return reply

    def _learn_from_device(self):
        """连上之后问一句「你是谁」。问不到就如实说没问到，不编固件版本。"""
        reply = self.send("device.get_status", {}, timeout=1.5, check_capabilities=False)
        if not reply.get("ok"):
            return {"ok": False, "error": reply.get("error") or "设备没有回答能力查询"}
        info = reply.get("result") or {}
        # 固件报版本用的键是 firmware；模拟设备两样都给，这里两种都认。
        version = str(info.get("firmware") or info.get("version") or "")
        if version:
            self.firmware_version = version
        caps = info.get("capabilities")
        if isinstance(caps, list) and caps:
            self.capabilities = tuple(str(item) for item in caps)
        limits = info.get("limits")
        if isinstance(limits, dict):
            for key, value in limits.items():
                if isinstance(value, dict):
                    self.limits[str(key)] = dict(value)
                elif isinstance(value, (list, tuple)):
                    self.limits[str(key)] = [str(item) for item in value]
        return {"ok": True, "version": self.firmware_version, "capabilities": list(self.capabilities)}

    def _note(self, text):
        if self.logger:
            self.logger.info("device %s: %s", self.name, text)
        return text


class _SimulatorTransport(LoopbackEsp32Transport):
    """回环传输 + 一台模拟固件：写进去的报文交给假固件，回复落进收件箱。"""

    def __init__(self, simulator, name="loopback"):
        super().__init__()
        self.name = name
        self.simulator = simulator

    def write(self, line):
        result = super().write(line)
        if result.get("ok"):
            self.simulator.handle(line)
            for reply in self.simulator.read_all():
                self.inbox.append(reply)
        return result

    def close(self):
        self.simulator.set_disconnected(True)
        return super().close()


class LoopbackProvider(DeviceProvider):
    """没有硬件时的模拟设备。开发、测试、界面演示都用它。"""

    kind = "simulator"
    is_mock = True
    transport_name = "loopback"

    def __init__(
        self,
        name="sim-1",
        capabilities=None,
        limits=None,
        registry=None,
        logger=None,
        clock=time.monotonic,
        simulator=None,
        version="sim-1.0.0",
        sensor_reader=None,
        fail_actions=(),
        silent_actions=(),
        latency=0.0,
        on_event=None,
    ):
        self.device_id = safe_device_id(name, fallback="sim")
        caps = tuple(capabilities or DeviceSimulator().capabilities)
        super().__init__(
            name=name,
            capabilities=caps,
            limits=limits,
            registry=registry,
            logger=logger,
            clock=clock,
            on_event=on_event,
        )
        self.simulator = simulator or DeviceSimulator(
            device_id=self.device_id,
            capabilities=caps,
            limits=self.limits,
            registry=self.registry,
            version=version,
            sensor_reader=sensor_reader,
            fail_actions=fail_actions,
            silent_actions=silent_actions,
            latency=latency,
            clock=clock,
        )
        self.transport = _SimulatorTransport(self.simulator, name=self.transport_name)
        self.firmware_version = self.simulator.version

    def _open_transport(self):
        result = self.transport.open() or {}
        self.simulator.set_disconnected(False)
        return result

    def _close_transport(self):
        return self.transport.close() or {}

    def _write_line(self, line):
        return self.transport.write(line) or {}

    def _read_lines(self):
        return list(self.transport.read() or [])

    def _transport_health(self):
        return {
            "transport": self.transport_name,
            "opened": bool(getattr(self.transport, "opened", False)),
            "simulated": True,
            "rejected": self.simulator.rejected,
            "accepted": self.simulator.accepted,
        }


class SerialProvider(DeviceProvider):
    """USB 串口设备。没配端口就不连，allow_real 没打开 DeviceManager 直接拒收。"""

    kind = "esp32"
    is_mock = False
    transport_name = "serial"

    def __init__(
        self,
        name="esp32-main",
        port="",
        baudrate=115200,
        capabilities=(),
        limits=None,
        registry=None,
        logger=None,
        transport=None,
        opener=None,
        clock=time.monotonic,
        on_event=None,
    ):
        super().__init__(
            name=name,
            capabilities=capabilities,
            limits=limits,
            registry=registry,
            logger=logger,
            clock=clock,
            on_event=on_event,
        )
        self.device_id = safe_device_id(name, fallback="esp32")
        self.port = str(port or "").strip()
        self.baudrate = int(baudrate or 115200)
        self._transport = transport
        self._opener = opener or _open_serial

    @property
    def transport(self):
        if self._transport is None:
            from devices.esp32 import SerialEsp32Transport

            self._transport = SerialEsp32Transport(
                port=self.port, baudrate=self.baudrate, opener=self._opener, logger=self.logger
            )
        return self._transport

    def _open_transport(self):
        if not self.port:
            return {"ok": False, "error": "没有配置串口名，例如 COM3；没配就不连"}
        return self.transport.open() or {}

    def _close_transport(self):
        return self.transport.close() or {}

    def _write_line(self, line):
        return self.transport.write(line) or {}

    def _read_lines(self):
        try:
            return list(self.transport.read() or [])
        except Exception as exc:
            self.last_error = "读串口失败：%s" % exc
            return []

    def _transport_health(self):
        probe = getattr(self.transport, "health", None)
        if not callable(probe):
            return None
        return probe()


def build_provider(spec, registry=None, logger=None, on_event=None):
    """按设备清单里的一条记录造 Provider。没写地址就还是模拟设备，不偷偷连真机。"""
    options = dict(getattr(spec, "options", None) or {})
    limits = options.get("limits") if isinstance(options.get("limits"), dict) else {}
    kind = str(getattr(spec, "kind", "") or "mock").lower()
    name = str(getattr(spec, "name", "") or "device-1")
    capabilities = tuple(getattr(spec, "capabilities", ()) or ())
    port = str(options.get("port") or "").strip()
    host = str(options.get("host") or "").strip()
    if kind == "wifi" and host:
        from devices.wifi import WifiProvider

        return WifiProvider(
            name=name,
            host=host,
            port=int(options.get("tcp_port") or 8266),
            token=str(options.get("token") or ""),
            capabilities=capabilities,
            limits=limits,
            registry=registry,
            logger=logger,
            on_event=on_event,
        )
    if kind == "esp32" and port:
        return SerialProvider(
            name=name,
            port=port,
            baudrate=int(options.get("baudrate") or 115200),
            capabilities=capabilities,
            limits=limits,
            registry=registry,
            logger=logger,
            on_event=on_event,
        )
    return LoopbackProvider(
        name=name,
        capabilities=capabilities or None,
        limits=limits,
        registry=registry,
        logger=logger,
        version=str(options.get("firmware") or "sim-1.0.0"),
        on_event=on_event,
    )
