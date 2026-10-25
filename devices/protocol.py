"""ESP32 设备消息协议 v1：带版本号的 JSON 报文。

电脑只发结构化动作，设备只回结构化确认。两端各校验一遍：电脑端拦下明显不合规
的报文，设备端再拦一遍，谁都不替对方省这一步。

报文形状（只是协议定义，不代表设备已经支持这些动作）：
    电脑发：{"version": 1, "type": "command", "message_id": "cmd-000001",
            "device_id": "esp32-main", "command": "servo.set_angle",
            "params": {"channel": 0, "angle_deg": 90}}
    设备回：{"version": 1, "type": "response", "message_id": "cmd-000001",
            "device_id": "esp32-main", "status": "completed",
            "result": {"channel": 0, "angle_deg": 90}, "error": null,
            "error_code": ""}
    设备主动说：{"version": 1, "type": "event", "device_id": "esp32-main",
                "event": "sensor.changed", "data": {"channel": 0, "value": 42}}

一条消息一行，UTF-8，换行分帧。回复必须对得上请求的 message_id；
设备主动发的事件没有对应的请求，也不占 message_id。
"""

import json
import re
import uuid

PROTOCOL_VERSION = 1
# 一行最多多少字节。状态和能力这类回复带着整个范围表，512 装不下，
# 1024 在 115200 波特率下也就 90 毫秒左右，还留得住「一行一帧」这个约定。
MAX_LINE_BYTES = 1024
MAX_ID_CHARS = 64

# 消息类型。请求、回复之外，设备还能主动说话。
TYPE_COMMAND = "command"
TYPE_RESPONSE = "response"
TYPE_EVENT = "event"
# auth 只在连接刚建立时用一次，之后还是走 command / response 那一套。
TYPE_AUTH = "auth"
TYPES = (TYPE_COMMAND, TYPE_RESPONSE, TYPE_EVENT, TYPE_AUTH)

# 认证令牌长度上限。只是防手滑传进来一大坨东西，不是密码强度要求。
MAX_TOKEN_CHARS = 128

# 设备主动上报的事件名。没登记的名字一律当噪声丢掉，不当指令执行。
EVENT_SENSOR = "sensor.changed"
EVENT_BUTTON = "button.pressed"
EVENT_STATE = "device.state"
EVENTS = (EVENT_SENSOR, EVENT_BUTTON, EVENT_STATE)

STATUS_ACCEPTED = "accepted"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_REJECTED = "rejected"
STATUS_CANCELLED = "cancelled"
STATUS_TIMEOUT = "timeout"
STATUS_DISCONNECTED = "device_disconnected"

STATUSES = (
    STATUS_ACCEPTED,
    STATUS_RUNNING,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_REJECTED,
    STATUS_CANCELLED,
    STATUS_TIMEOUT,
    STATUS_DISCONNECTED,
)

# 收到这些状态就说明这一单结束了，不用再等确认。
FINAL_STATUSES = (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_REJECTED,
    STATUS_CANCELLED,
    STATUS_TIMEOUT,
    STATUS_DISCONNECTED,
)

STATUS_LABELS = {
    STATUS_ACCEPTED: "设备已接单",
    STATUS_RUNNING: "设备正在执行",
    STATUS_COMPLETED: "已完成",
    STATUS_FAILED: "执行失败",
    STATUS_REJECTED: "设备拒收",
    STATUS_CANCELLED: "已取消",
    STATUS_TIMEOUT: "超时没有确认",
    STATUS_DISCONNECTED: "设备已断开",
}

# 统一错误码：人话原因放 error，机器判断放 error_code。空字符串代表没有错。
ERROR_NONE = ""
ERROR_BAD_JSON = "bad_json"
ERROR_BAD_VERSION = "bad_version"
ERROR_BAD_TYPE = "bad_type"
ERROR_MISSING_FIELD = "missing_field"
ERROR_UNKNOWN_COMMAND = "unknown_command"
ERROR_BAD_PARAM = "bad_param"
ERROR_OUT_OF_RANGE = "out_of_range"
ERROR_UNSUPPORTED = "unsupported"
ERROR_BUSY = "busy"
ERROR_NOT_CONNECTED = "not_connected"
ERROR_TIMEOUT = "timeout"
ERROR_INTERNAL = "internal"

ERROR_CODES = (
    ERROR_BAD_JSON,
    ERROR_BAD_VERSION,
    ERROR_BAD_TYPE,
    ERROR_MISSING_FIELD,
    ERROR_UNKNOWN_COMMAND,
    ERROR_BAD_PARAM,
    ERROR_OUT_OF_RANGE,
    ERROR_UNSUPPORTED,
    ERROR_BUSY,
    ERROR_NOT_CONNECTED,
    ERROR_TIMEOUT,
    ERROR_INTERNAL,
)

ERROR_LABELS = {
    ERROR_NONE: "没有错误",
    ERROR_BAD_JSON: "报文不是合法 JSON",
    ERROR_BAD_VERSION: "协议版本对不上",
    ERROR_BAD_TYPE: "消息类型对不上",
    ERROR_MISSING_FIELD: "少了必需字段",
    ERROR_UNKNOWN_COMMAND: "没登记这个动作",
    ERROR_BAD_PARAM: "参数类型或名字不对",
    ERROR_OUT_OF_RANGE: "参数超出设备声明的范围",
    ERROR_UNSUPPORTED: "这台设备没有这个能力",
    ERROR_BUSY: "设备正忙，这一单没收",
    ERROR_NOT_CONNECTED: "设备没连上",
    ERROR_TIMEOUT: "设备没有在规定时间内确认",
    ERROR_INTERNAL: "设备内部出错",
}

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,%d}$" % MAX_ID_CHARS)
_COMMAND_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_EVENT_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")


class ProtocolError(ValueError):
    """报文不合协议。调用方一律按失败处理，绝不猜字段。"""


def new_message_id(prefix="msg"):
    """真唯一：每次调用都不一样，重复概率可忽略。"""
    return "%s-%s" % (str(prefix or "msg")[:16], uuid.uuid4().hex)


def make_request(device_id, command, params=None, message_id=None):
    """拼一帧请求。这里只保证形状，参数范围由动作注册表管。"""
    return {
        "version": PROTOCOL_VERSION,
        "type": TYPE_COMMAND,
        "message_id": str(message_id or new_message_id()),
        "device_id": str(device_id or ""),
        "command": str(command or ""),
        "params": dict(params or {}),
    }


def make_reply(message_id, device_id, status, result=None, error=None, error_code=""):
    """拼一帧回复。设备端与模拟设备都用这一个，免得两边形状写歪。"""
    return {
        "version": PROTOCOL_VERSION,
        "type": TYPE_RESPONSE,
        "message_id": str(message_id or ""),
        "device_id": str(device_id or ""),
        "status": str(status or ""),
        "result": result if isinstance(result, dict) else {},
        "error": str(error) if error else None,
        "error_code": str(error_code or ""),
    }


def make_event(device_id, event, data=None):
    """拼一帧设备主动上报。事件没有 message_id，也不等谁确认。"""
    return {
        "version": PROTOCOL_VERSION,
        "type": TYPE_EVENT,
        "device_id": str(device_id or ""),
        "event": str(event or ""),
        "data": dict(data or {}),
    }


def make_auth(device_id, token, message_id=None):
    """连上之后先报一次身份。只在第一条发，之后不再发。"""
    return {
        "version": PROTOCOL_VERSION,
        "type": TYPE_AUTH,
        "message_id": str(message_id or new_message_id("auth")),
        "device_id": str(device_id or ""),
        "token": str(token or ""),
    }


def kind_of(data):
    """这条消息是什么类型。认不出来就返回空串，调用方按噪声处理。"""
    if not isinstance(data, dict):
        return ""
    kind = data.get("type")
    return str(kind) if kind in TYPES else ""


def is_event(data):
    return kind_of(data) == TYPE_EVENT


def encode(message):
    """把一帧变成一行。超长的直接拒发，不靠设备端截断。"""
    try:
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ProtocolError("报文没法序列化：%s" % exc)
    size = len(line.encode("utf-8"))
    if size > MAX_LINE_BYTES:
        raise ProtocolError("报文太长（%d 字节，上限 %d）" % (size, MAX_LINE_BYTES))
    return line


def decode(line):
    """把一行变回一帧。不是合法 JSON 就抛，不返回半个结果。"""
    if isinstance(line, (bytes, bytearray)):
        text = bytes(line).decode("utf-8", errors="replace").strip()
    else:
        text = str(line or "").strip()
    if not text:
        raise ProtocolError("空报文")
    if len(text.encode("utf-8")) > MAX_LINE_BYTES:
        raise ProtocolError("报文太长（超过 %d 字节）" % MAX_LINE_BYTES)
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ProtocolError("不是合法 JSON：%s" % exc)
    if not isinstance(data, dict):
        raise ProtocolError("报文必须是一个对象")
    return data


def _problems_common(data, expected_version=PROTOCOL_VERSION, expected_type=None):
    problems = []
    version = data.get("version")
    if version != expected_version:
        problems.append("协议版本对不上：收到 %r，只认 %r" % (version, expected_version))
    message_id = data.get("message_id")
    if not isinstance(message_id, str) or not _ID_RE.match(message_id or ""):
        problems.append("message_id 不合法")
    device_id = data.get("device_id")
    if not isinstance(device_id, str) or not _ID_RE.match(device_id or ""):
        problems.append("device_id 不合法")
    if expected_type is not None:
        kind = data.get("type")
        if kind != expected_type:
            problems.append("消息类型对不上：收到 %r，应该是 %r" % (kind, expected_type))
    return problems


def check_request(data, expected_version=PROTOCOL_VERSION):
    """校验一帧请求，返回问题列表。空列表就是通过。"""
    if not isinstance(data, dict):
        return ["请求必须是一个对象"]
    problems = _problems_common(data, expected_version, TYPE_COMMAND)
    command = data.get("command")
    if not isinstance(command, str) or not _COMMAND_RE.match(command or ""):
        problems.append("command 必须是像 led.set 这样的动作名")
    params = data.get("params", {})
    if not isinstance(params, dict):
        problems.append("params 必须是对象")
    elif any(not isinstance(key, str) for key in params):
        problems.append("params 的键必须是字符串")
    else:
        for key, value in params.items():
            if not _ID_RE.match(str(key)):
                problems.append("参数名不合法：%r" % key)
                break
            if isinstance(value, (dict, list)):
                continue
            if not isinstance(value, (str, int, float, bool)) and value is not None:
                problems.append("参数 %s 的类型不支持" % key)
                break
    return problems


def check_reply(data, message_id=None, device_id=None, expected_version=PROTOCOL_VERSION):
    """校验一帧回复。带 message_id 就要求对上，防止串单。"""
    if not isinstance(data, dict):
        return ["回复必须是一个对象"]
    problems = _problems_common(data, expected_version, TYPE_RESPONSE)
    status = data.get("status")
    if status not in STATUSES:
        problems.append("status 不认识：%r" % (status,))
    result = data.get("result")
    if result is not None and not isinstance(result, dict):
        problems.append("result 必须是对象或 null")
    error = data.get("error")
    if error is not None and not isinstance(error, str):
        problems.append("error 必须是字符串或 null")
    code = data.get("error_code") or ""
    if not isinstance(code, str):
        problems.append("error_code 必须是字符串")
    elif code and code not in ERROR_CODES:
        problems.append("error_code 不认识：%r" % (code,))
    if status == STATUS_COMPLETED and error:
        problems.append("completed 的回复不该带 error")
    if status == STATUS_COMPLETED and code:
        problems.append("completed 的回复不该带错误码")
    if status in (STATUS_FAILED, STATUS_REJECTED, STATUS_TIMEOUT, STATUS_DISCONNECTED) and not error:
        problems.append("%s 的回复必须说明原因" % status)
    if message_id is not None and data.get("message_id") != message_id:
        problems.append("消息标识对不上，这条回复不是这次的")
    if device_id is not None and data.get("device_id") != device_id:
        problems.append("设备标识对不上：%r" % (data.get("device_id"),))
    return problems


def check_event(data, device_id=None, expected_version=PROTOCOL_VERSION):
    """校验一帧设备主动上报。事件没有 message_id，也没有结果状态。"""
    if not isinstance(data, dict):
        return ["事件必须是一个对象"]
    problems = []
    version = data.get("version")
    if version != expected_version:
        problems.append("协议版本对不上：收到 %r，只认 %r" % (version, expected_version))
    if data.get("type") != TYPE_EVENT:
        problems.append("消息类型对不上：收到 %r，应该是 %r" % (data.get("type"), TYPE_EVENT))
    device_id_value = data.get("device_id")
    if not isinstance(device_id_value, str) or not _ID_RE.match(device_id_value or ""):
        problems.append("device_id 不合法")
    event = data.get("event")
    if not isinstance(event, str) or not _EVENT_RE.match(event or ""):
        problems.append("event 必须是像 sensor.changed 这样的名字")
    payload = data.get("data", {})
    if not isinstance(payload, dict):
        problems.append("data 必须是对象")
    if device_id is not None and device_id_value != device_id:
        problems.append("设备标识对不上：%r" % (device_id_value,))
    return problems


def validate_request(data, expected_version=PROTOCOL_VERSION):
    problems = check_request(data, expected_version)
    if problems:
        raise ProtocolError("；".join(problems))
    return data


def validate_reply(data, message_id=None, device_id=None, expected_version=PROTOCOL_VERSION):
    problems = check_reply(data, message_id, device_id, expected_version)
    if problems:
        raise ProtocolError("；".join(problems))
    return data


def check_auth(data, device_id=None, expected_version=PROTOCOL_VERSION):
    """校验一帧认证消息。只在连上那一刻用，之后不该再出现。"""
    if not isinstance(data, dict):
        return ["认证消息必须是一个对象"]
    problems = _problems_common(data, expected_version, TYPE_AUTH)
    token = data.get("token")
    if not isinstance(token, str):
        problems.append("token 必须是字符串")
    elif len(token) > MAX_TOKEN_CHARS:
        problems.append("token 太长了")
    if device_id is not None and data.get("device_id") != device_id:
        problems.append("设备标识对不上：%r" % (data.get("device_id"),))
    return problems


def validate_auth(data, device_id=None, expected_version=PROTOCOL_VERSION):
    problems = check_auth(data, device_id, expected_version)
    if problems:
        raise ProtocolError("；".join(problems))
    return data


def validate_event(data, device_id=None, expected_version=PROTOCOL_VERSION):
    problems = check_event(data, device_id, expected_version)
    if problems:
        raise ProtocolError("；".join(problems))
    return data


def is_final(status):
    return str(status or "") in FINAL_STATUSES


def status_label(status):
    return STATUS_LABELS.get(str(status or ""), str(status or ""))


def error_label(code):
    return ERROR_LABELS.get(str(code or ""), str(code or ""))
