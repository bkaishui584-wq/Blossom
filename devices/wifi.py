"""Wi-Fi 设备：走 TCP，说的还是同一套 JSON 协议。

和串口那一条比，只换了传输，动作、权限、任务、事件那一整套照用，没有第二套逻辑。
连上之后先发一帧 auth；设备认了才继续，不认就断开并说清楚原因。

没有做加密。同一台机器上的别人能看见明文，所以只在你信得过的局域网里用，
真放到不可信网络里之前得先加 TLS。
"""

import socket
import time

from devices import protocol
from devices.provider import DeviceProvider, safe_device_id

DEFAULT_PORT = 8266
AUTH_TIMEOUT = 2.0
POLL_SECONDS = 0.02
DEFAULT_TIMEOUT = 3.0


class WifiProvider(DeviceProvider):
    """通过 TCP 连一台设备。默认不连，参数都得自己填。"""

    kind = "esp32-wifi"
    is_mock = False
    transport_name = "wifi"

    def __init__(
        self,
        name="esp32-wifi",
        host="",
        port=DEFAULT_PORT,
        token="",
        capabilities=(),
        limits=None,
        registry=None,
        logger=None,
        clock=time.monotonic,
        on_event=None,
        connect_timeout=3.0,
        socket_factory=None,
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
        self.host = str(host or "").strip()
        self.port = int(port or DEFAULT_PORT)
        self.token = str(token or "")
        self.connect_timeout = max(0.2, float(connect_timeout or 3.0))
        self._socket = None
        self._factory = socket_factory or socket.socket
        self.buffer = b""
        self.writes = 0
        self.reads = 0
        self.authenticated = False

    # ---------- 传输 ----------
    def _open_transport(self):
        if not self.host:
            return {"ok": False, "error": "没有配置设备地址，例如 192.168.1.50"}
        try:
            handle = self._factory()
            handle.settimeout(self.connect_timeout)
            handle.connect((self.host, self.port))
            handle.setblocking(False)
        except Exception as exc:
            self._note("连不上 %s:%s：%s" % (self.host, self.port, exc))
            return {"ok": False, "error": "连不上 %s:%s：%s" % (self.host, self.port, exc)}
        self._socket = handle
        self.buffer = b""
        self.authenticated = False
        if self.token:
            return self._authenticate()
        return {
            "ok": True,
            "state": "connected",
            "endpoint": "%s:%s" % (self.host, self.port),
            "note": "没有配置令牌，按无认证连接",
        }

    def _authenticate(self):
        """连上之后先报令牌。设备不认就断开，不带病往下走。"""
        frame = protocol.make_auth(self.device_id, self.token)
        written = self._write_line(protocol.encode(frame)) or {}
        if not written.get("ok"):
            self._close_transport()
            return {"ok": False, "error": str(written.get("error") or "认证帧没发出去")}
        deadline = self.clock() + AUTH_TIMEOUT
        while self.clock() <= deadline:
            for raw in self._read_lines() or []:
                try:
                    data = protocol.decode(raw)
                except protocol.ProtocolError:
                    continue
                if data.get("message_id") != frame["message_id"]:
                    continue
                if data.get("status") == protocol.STATUS_COMPLETED:
                    self.authenticated = True
                    return {
                        "ok": True,
                        "state": "authenticated",
                        "endpoint": "%s:%s" % (self.host, self.port),
                    }
                reason = str(data.get("error") or "设备不认这个令牌")
                self._close_transport()
                return {"ok": False, "error": reason}
            time.sleep(POLL_SECONDS)
        self._close_transport()
        return {"ok": False, "error": "认证没有回应，断开"}

    def _close_transport(self):
        handle, self._socket = self._socket, None
        self.authenticated = False
        if handle is None:
            return {"ok": True, "state": "already_closed"}
        try:
            handle.close()
        except Exception as exc:
            self._note("关连接失败：%s" % exc)
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "state": "closed"}

    def _write_line(self, line):
        if self._socket is None:
            return {"ok": False, "error": "连接还没打开"}
        payload = (str(line).strip() + "\n").encode("utf-8")
        try:
            self._socket.setblocking(True)
            try:
                self._socket.settimeout(self.connect_timeout)
                self._socket.sendall(payload)
            finally:
                self._socket.setblocking(False)
        except Exception as exc:
            self._note("发不出去：%s" % exc)
            return {"ok": False, "error": "发不出去：%s" % exc}
        self.writes += 1
        return {"ok": True, "sent": str(line), "count": self.writes}

    def _read_lines(self):
        """把已经到的行取走。读不到就是空列表，不当成错误。"""
        if self._socket is None:
            return []
        try:
            chunk = self._socket.recv(4096)
        except (BlockingIOError, socket.timeout):
            return []
        except Exception as exc:
            self._note("读连接失败：%s" % exc)
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

    def _transport_health(self):
        # 只报「有没有配令牌」，绝不把令牌本身写进状态或日志。
        return {
            "transport": self.transport_name,
            "endpoint": "%s:%s" % (self.host or "(未配置)", self.port),
            "open": self._socket is not None,
            "token_set": bool(self.token),
            "authenticated": bool(self.authenticated),
            "writes": self.writes,
            "lines_read": self.reads,
            "encrypted": False,
        }
