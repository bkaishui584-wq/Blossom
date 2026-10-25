"""模拟设备：内存里的假固件，用来在没有硬件时把链路跑通。

它做的事和真固件一样：校验协议版本、查白名单、独立验一遍参数、回一帧带
message_id 的确认。所以电脑端少验一样东西，测试就能发现。

它不会假装自己有硬件：读数拿不到就留空，能力没声明就说做不到。
"""

import time

from devices import protocol
from devices.actions import DeviceCommandRegistry, copy_limits

DEFAULT_CAPABILITIES = ("status", "led", "servo", "sensor", "audio", "stop")

# 记住多少条已经回复过的 message_id。超了就丢最旧的。
SEEN_LIMIT = 64


class DeviceSimulator:
    """一台假 ESP32。"""

    def __init__(
        self,
        device_id="esp32-sim",
        capabilities=DEFAULT_CAPABILITIES,
        limits=None,
        version="sim-1.0.0",
        registry=None,
        latency=0.0,
        fail_actions=(),
        silent_actions=(),
        sensor_reader=None,
        sensor_unit="mm",
        clock=time.monotonic,
    ):
        self.device_id = str(device_id or "esp32-sim")
        self.capabilities = tuple(str(item) for item in (capabilities or ()))
        self.limits = dict(limits or {})
        self.version = str(version)
        self.registry = registry or DeviceCommandRegistry()
        self.latency = max(0.0, float(latency or 0.0))
        self.fail_actions = {str(item) for item in (fail_actions or ())}
        self.silent_actions = {str(item) for item in (silent_actions or ())}
        self.sensor_reader = sensor_reader
        self.sensor_unit = str(sensor_unit or "mm")
        self.clock = clock
        self.started_at = clock()
        self.disconnected = False
        self.outbox = []
        self.log = []
        self.accepted = 0
        self.rejected = 0
        self.led = {}
        self.servo = {}
        self.last_status = None
        self.seen = {}
        self.seen_order = []
        self.events_sent = 0
        self.audio = {"track_id": "", "volume": None, "plays": 0}

    # ---------- 传输口径 ----------
    def set_disconnected(self, flag=True):
        self.disconnected = bool(flag)
        return self.disconnected

    def read_line(self):
        """取出下一帧要发回去的报文。没有就返回空。"""
        if not self.outbox:
            return ""
        return self.outbox.pop(0)

    def read_all(self):
        lines, self.outbox = list(self.outbox), []
        return lines

    # ---------- 固件逻辑 ----------
    def handle(self, line):
        """处理一行请求，把回复放进 outbox。返回是否回复了。"""
        if self.disconnected:
            self._note("设备已断开，收到报文也不回")
            return False
        try:
            data = protocol.decode(line)
        except protocol.ProtocolError as exc:
            self.rejected += 1
            self._note("报文不合法，没法对上单：%s" % exc)
            return False

        if protocol.kind_of(data) == protocol.TYPE_EVENT:
            # 事件是设备往外发的。设备端收到事件，说明对面发错了。
            self.rejected += 1
            self._note("收到事件消息，设备端不该收这一种")
            return False

        message_id = data.get("message_id")
        if not isinstance(message_id, str) or not protocol._ID_RE.match(message_id):
            self.rejected += 1
            self._note("没有可用的 message_id，不回复")
            return False

        # 同一单重发就不再执行一遍：把上次那一帧原样再还回去。
        cached = self.seen.get(message_id)
        if cached is not None:
            self._note("这一单已经回过，原样再发一次：%s" % message_id)
            self.outbox.append(cached)
            return True

        problems = protocol.check_request(data)
        if problems:
            self.rejected += 1
            self._finish(
                message_id,
                protocol.STATUS_REJECTED,
                error="；".join(problems),
                error_code=protocol.ERROR_BAD_TYPE,
            )
            return True

        if data.get("device_id") != self.device_id:
            # 不是发给我的就回一句，但绝不动自己的输出。
            self.rejected += 1
            self._finish(
                message_id,
                protocol.STATUS_REJECTED,
                error="这条不是发给这台设备的",
                error_code=protocol.ERROR_NOT_CONNECTED,
            )
            return True

        action = data["command"]
        params = data.get("params") or {}

        if action in self.silent_actions:
            self._note("装作没听见：%s" % action)
            return False

        if self.latency:
            time.sleep(self.latency)

        if action in self.fail_actions:
            self._note("按配置让这一次失败：%s" % action)
            self._finish(
                message_id,
                protocol.STATUS_FAILED,
                error="设备报告这次没做成",
                error_code=protocol.ERROR_INTERNAL,
            )
            return True

        spec = self.registry.get(action)
        if spec is None:
            self.rejected += 1
            self._finish(
                message_id,
                protocol.STATUS_REJECTED,
                error="固件不认这个动作：%s" % action,
                error_code=protocol.ERROR_UNKNOWN_COMMAND,
            )
            return True

        supported, reason = self.registry.supports(action, self.capabilities)
        if not supported:
            self.rejected += 1
            self._finish(
                message_id,
                protocol.STATUS_REJECTED,
                error=reason,
                error_code=protocol.ERROR_UNSUPPORTED,
            )
            return True

        clean, error = self.registry.validate(
            action, params, limits=self.limits, capabilities=self.capabilities
        )
        if error:
            self.rejected += 1
            self._finish(
                message_id,
                protocol.STATUS_REJECTED,
                error=error,
                error_code=self._param_code(error),
            )
            return True

        result, error = self._run(action, clean)
        if error:
            self.rejected += 1
            self._finish(
                message_id,
                protocol.STATUS_REJECTED,
                error=error,
                error_code=protocol.ERROR_UNSUPPORTED,
            )
            return True
        self.accepted += 1
        self.last_status = protocol.STATUS_COMPLETED
        self._finish(message_id, protocol.STATUS_COMPLETED, result=result)
        return True

    def _run(self, action, params):
        simulated = {"simulated": True}
        if action == "device.get_status":
            return dict(self.snapshot(), **simulated), ""
        if action == "device.get_capabilities":
            return dict(self.capabilities_payload(), **simulated), ""
        if action == "led.set":
            channel = int(params.get("channel", 0))
            on = bool(params.get("on"))
            self.led[channel] = on
            return {"channel": channel, "on": on, **simulated}, ""
        if action == "audio.play":
            track = str(params.get("track_id") or "")
            volume = params.get("volume")
            self.audio = {
                "track_id": track,
                "volume": volume,
                "plays": int(self.audio.get("plays") or 0) + 1,
            }
            return {"track_id": track, "volume": volume, "playing": True, **simulated}, ""
        if action == "servo.set_angle":
            channel = int(params.get("channel", 0))
            angle = int(params.get("angle_deg", 0))
            self.servo[channel] = angle
            return {"channel": channel, "angle_deg": angle, **simulated}, ""
        if action == "sensor.read":
            channel = int(params.get("channel", 0))
            value = None
            error = ""
            if self.sensor_reader is None:
                error = "这台设备没有接传感器，读不到数"
            else:
                try:
                    value = self.sensor_reader(channel)
                except Exception as exc:
                    value = None
                    error = "读传感器出错：%s" % exc
            # 读不到就如实说读不到，不拿 0 冒充测量值。
            return (
                {
                    "channel": channel,
                    "valid": value is not None,
                    "value": value,
                    "unit": self.sensor_unit,
                    "measured_at_ms": self._uptime_ms(),
                    "error": error,
                    **simulated,
                },
                "",
            )
        if action == "device.stop":
            # 停下的定义：所有灯关掉，运动窗口清掉。和固件里的 doStop 一个意思。
            for channel in list(self.led):
                self.led[channel] = False
            self.servo = {}
            self.audio = {"track_id": "", "volume": None, "plays": int(self.audio.get("plays") or 0)}
            return {"stopped": True, "leds_off": True, "audio_stopped": True, **simulated}, ""
        return None, "模拟设备没实现这个动作：%s" % action

    def snapshot(self):
        return {
            "device_id": self.device_id,
            "version": self.version,
            "firmware": self.version,
            "protocol_version": protocol.PROTOCOL_VERSION,
            "capabilities": list(self.capabilities),
            "limits": copy_limits(self.limits),
            "led": {str(key): bool(value) for key, value in self.led.items()},
            "servo": {str(key): int(value) for key, value in self.servo.items()},
            "audio": dict(self.audio),
            "uptime_s": round(max(0.0, self.clock() - self.started_at), 3),
        }

    def capabilities_payload(self):
        """设备自己报的会做什么。电脑端以这一份为准，不替设备猜。"""
        return {
            "device_id": self.device_id,
            "protocol_version": protocol.PROTOCOL_VERSION,
            "firmware": self.version,
            "capabilities": list(self.capabilities),
            "limits": copy_limits(self.limits),
        }

    def push_event(self, event, data=None):
        """设备主动上报一件事。没登记过的事件名不发，免得两端约定悄悄漂移。"""
        if event not in protocol.EVENTS:
            self._note("没有登记的事件名，不发：%s" % event)
            return None
        frame = protocol.make_event(self.device_id, event, data)
        try:
            self.outbox.append(protocol.encode(frame))
        except protocol.ProtocolError as exc:
            self._note("事件发不出去：%s" % exc)
            return None
        self.events_sent += 1
        return frame

    # ---------- 内部 ----------
    def _reply(self, message_id, status, result=None, error=None, error_code=""):
        frame = protocol.make_reply(
            message_id, self.device_id, status, result, error, error_code
        )
        try:
            line = protocol.encode(frame)
        except protocol.ProtocolError as exc:
            self._note("回复发不出去：%s" % exc)
            return None
        self.outbox.append(line)
        return line

    def _finish(self, message_id, status, result=None, error=None, error_code=""):
        """回一帧，并记进去重表。同一单再发过来就把这一帧原样还回去。"""
        line = self._reply(message_id, status, result, error, error_code)
        if line is None:
            return None
        self.seen[message_id] = line
        self.seen_order.append(message_id)
        while len(self.seen_order) > SEEN_LIMIT:
            oldest = self.seen_order.pop(0)
            self.seen.pop(oldest, None)
        return line

    def _param_code(self, error):
        """把参数校验的人话原因收成一个错误码。"""
        text = str(error or "")
        if "不能小于" in text or "不能大于" in text:
            return protocol.ERROR_OUT_OF_RANGE
        if "没有声明" in text:
            return protocol.ERROR_UNSUPPORTED
        return protocol.ERROR_BAD_PARAM

    def _uptime_ms(self):
        return int(max(0.0, self.clock() - self.started_at) * 1000)

    def _note(self, text):
        self.log.append(str(text))
        if len(self.log) > 200:
            del self.log[:100]
        return text
