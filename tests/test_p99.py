"""P7 验收：Wi-Fi（TCP）设备。

同一套协议换一条传输，动作、权限、任务都不重来一遍。
设备端用本机回环上的假固件顶着，不开真网络、不连真硬件。
"""

import socket
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from devices import protocol  # noqa: E402
from devices.manifest import DeviceSpec  # noqa: E402
from devices.provider import build_provider  # noqa: E402
from devices.simulator import DeviceSimulator  # noqa: E402
from devices.wifi import WifiProvider  # noqa: E402

CAPS = ("status", "led", "servo", "sensor", "audio", "stop")
LIMITS = {
    "led_channels": {"min": 0, "max": 1},
    "servo_channels": {"min": 0, "max": 1},
    "servo_angle": {"min": 10, "max": 170},
    "sensor_channels": {"min": 0, "max": 0},
}
DEVICE_ID = "esp32-wifi"


class FakeWifiDevice:
    """把模拟固件挂在 TCP 上。认令牌，不认就不干活。"""

    def __init__(self, token="", device_id=DEVICE_ID):
        self.token = str(token or "")
        self.sim = DeviceSimulator(
            device_id=device_id, capabilities=CAPS, limits=dict(LIMITS)
        )
        self.device_id = device_id
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(2)
        self.port = self.sock.getsockname()[1]
        self.auth_ok = 0
        self.auth_bad = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass

    def _serve(self):
        while not self._stop.is_set():
            self.sock.settimeout(0.2)
            try:
                conn, _addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn):
        conn.settimeout(0.2)
        buffer = b""
        authed = not self.token
        while not self._stop.is_set():
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                # 真设备推事件不用等主机先说话，这里也照做。
                self._flush(conn)
                continue
            except OSError:
                break
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                raw, _, buffer = buffer.partition(b"\n")
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    frame = protocol.decode(line)
                except protocol.ProtocolError:
                    continue
                if protocol.kind_of(frame) == protocol.TYPE_AUTH:
                    authed = self._check_token(conn, frame)
                    continue
                if not authed:
                    self._refuse(conn, frame, "还没认证，先发 auth")
                    continue
                self.sim.handle(line)
                self._flush(conn)
            self._flush(conn)
        try:
            conn.close()
        except OSError:
            pass

    def _check_token(self, conn, frame):
        message_id = str(frame.get("message_id") or "")
        if str(frame.get("token") or "") == self.token and self.token:
            self.auth_ok += 1
            reply = protocol.make_reply(message_id, self.device_id, protocol.STATUS_COMPLETED, {})
            conn.sendall((protocol.encode(reply) + "\n").encode("utf-8"))
            return True
        self.auth_bad += 1
        reply = protocol.make_reply(
            message_id,
            self.device_id,
            protocol.STATUS_REJECTED,
            error="令牌不对",
            error_code=protocol.ERROR_UNSUPPORTED,
        )
        conn.sendall((protocol.encode(reply) + "\n").encode("utf-8"))
        return False

    def _refuse(self, conn, frame, why):
        message_id = str(frame.get("message_id") or "")
        if not message_id:
            return
        reply = protocol.make_reply(
            message_id,
            self.device_id,
            protocol.STATUS_REJECTED,
            error=why,
            error_code=protocol.ERROR_NOT_CONNECTED,
        )
        conn.sendall((protocol.encode(reply) + "\n").encode("utf-8"))

    def _flush(self, conn):
        for line in self.sim.read_all():
            conn.sendall((line + "\n").encode("utf-8"))


def make_provider(server, token=""):
    return WifiProvider(
        name=DEVICE_ID,
        host="127.0.0.1",
        port=server.port,
        token=token,
        limits=dict(LIMITS),
    )


class WifiProviderTests(unittest.TestCase):
    def setUp(self):
        self.servers = []
        self.providers = []

    def tearDown(self):
        for provider in self.providers:
            try:
                provider.disconnect()
            except Exception:
                pass
        for server in self.servers:
            server.close()

    def server(self, token=""):
        server = FakeWifiDevice(token=token)
        self.servers.append(server)
        return server

    def provider(self, server, token=""):
        provider = make_provider(server, token=token)
        self.providers.append(provider)
        return provider

    def test_the_same_actions_work_over_tcp(self):
        server = self.server(token="s3cret")
        provider = self.provider(server, token="s3cret")
        result = provider.connect()
        self.assertTrue(result["ok"], result)
        self.assertTrue(provider.authenticated)
        reply = provider.send("led.set", {"channel": 0, "on": True})
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["status"], protocol.STATUS_COMPLETED)
        self.assertEqual(server.auth_ok, 1)

    def test_a_wrong_token_is_refused_and_the_link_is_dropped(self):
        server = self.server(token="s3cret")
        provider = self.provider(server, token="猜的")
        result = provider.connect()
        self.assertFalse(result["ok"], result)
        self.assertIn("令牌", result["error"])
        self.assertFalse(provider.connected)
        self.assertEqual(server.auth_bad, 1)

    def test_a_guarded_device_does_not_answer_unauthenticated_commands(self):
        server = self.server(token="s3cret")
        provider = self.provider(server, token="")
        provider.connect()
        reply = provider.send("led.set", {"channel": 0, "on": True})
        self.assertFalse(reply["ok"])
        self.assertTrue(reply["error"], "失败要给原因，不能空着")
        self.assertEqual(server.sim.accepted, 0, "没认证的设备一件活都不该做")

    def test_a_missing_host_is_refused_before_any_socket(self):
        provider = WifiProvider(name=DEVICE_ID, host="", port=1)
        result = provider.connect()
        self.assertFalse(result["ok"])
        self.assertIn("设备地址", result["error"])

    def test_health_never_leaks_the_token(self):
        server = self.server(token="s3cret")
        provider = self.provider(server, token="s3cret")
        provider.connect()
        health = provider.health()
        self.assertTrue(health["token_set"])
        self.assertFalse(health["encrypted"], "没做加密就照实说")
        self.assertNotIn("s3cret", str(health))
        self.assertNotIn("s3cret", str(provider.get_state()))

    def test_servo_range_still_comes_from_the_device(self):
        server = self.server()
        provider = self.provider(server)
        provider.connect()
        self.assertEqual(provider.limits["servo_angle"]["max"], 170)
        refused = provider.send("servo.set_angle", {"channel": 0, "angle_deg": 200})
        self.assertFalse(refused["ok"])
        self.assertIn("170", refused["error"], "越界要在本机就被拦住，说清楚上限")
        self.assertEqual(server.sim.servo, {}, "越界的角度不该真的发出去")

    def test_device_events_come_through_the_same_bridge(self):
        server = self.server()
        seen = []
        provider = WifiProvider(
            name=DEVICE_ID,
            host="127.0.0.1",
            port=server.port,
            limits=dict(LIMITS),
            on_event=lambda device, frame: seen.append(frame),
        )
        self.providers.append(provider)
        provider.connect()
        server.sim.push_event(protocol.EVENT_BUTTON, {"index": 1})
        deadline = time.time() + 3
        while time.time() < deadline and not seen:
            provider.poll()
            time.sleep(0.05)
        self.assertTrue(seen, "设备主动上报应该走上同一条通路")
        self.assertEqual(seen[0]["event"], protocol.EVENT_BUTTON)


class ManifestWifiTests(unittest.TestCase):
    def test_a_wifi_entry_builds_a_wifi_provider(self):
        spec = DeviceSpec(
            name="esp32-wifi", kind="wifi", options={"host": "192.168.1.50", "tcp_port": 8266}
        )
        provider = build_provider(spec)
        self.assertIsInstance(provider, WifiProvider)
        self.assertEqual(provider.host, "192.168.1.50")
        self.assertEqual(provider.port, 8266)

    def test_a_wifi_entry_without_a_host_stays_simulated(self):
        spec = DeviceSpec(name="esp32-wifi", kind="wifi", options={})
        provider = build_provider(spec)
        self.assertTrue(provider.is_mock, "没写地址就不该去连一个不存在的机器")


if __name__ == "__main__":
    unittest.main()
