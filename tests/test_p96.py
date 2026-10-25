"""V11 验收补充：设备中心接进主工作台后，能开、能刷新、能跑真实动作。

界面只负责把状态摆出来。连接、执行、急停都走设备栈里的权限服务和任务队列，
界面不替权限服务放行。这里不碰真硬件：用的是模拟设备，结果里 simulated 一定是 True。

后台线程把结果送回界面靠 root.after，所以这里跑的是真的 Tk 事件循环。
"""

import sys
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.context import ContextManager  # noqa: E402
from agent.core import AgentCore  # noqa: E402
from behavior.manager import BehaviorManager  # noqa: E402
from database.db import Database  # noqa: E402
from devices.stack import build_stack  # noqa: E402
from memory.manager import MemoryManager  # noqa: E402
from memory.repository import MemoryRepository  # noqa: E402
from model.base import ChatDelta, ChatResult, ModelProvider, Usage  # noqa: E402
from persona.interface import DefaultPersonaProvider  # noqa: E402
from persona.training import LocalTrainingCollector  # noqa: E402
from security.config import Config  # noqa: E402
from tools.permission import GrantStore, PermissionManager  # noqa: E402
from ui.desktop import DesktopPet  # noqa: E402
from ui.device_view import DeviceWindow  # noqa: E402


class StubProvider(ModelProvider):
    name = "stub"

    def is_available(self):
        return True

    def chat(self, messages, tools=None, temperature=0.7):
        return ChatResult(
            ok=True, content="在的。", usage=Usage(prompt_tokens=1, completion_tokens=1), model="stub"
        )

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


def pump(root, until=None, seconds=5.0):
    """跑真的 Tk 事件循环，等后台线程把结果送回来。

    后台线程只能用 root.after 排队；主线程不在 mainloop 里的时候这一步会被 Tkinter
    拒掉，所以这里不能拿 update() 转圈。
    """
    deadline = time.time() + float(seconds)
    state = {"hit": False}

    def ready():
        return bool(until()) if until is not None else True

    def tick():
        if ready():
            state["hit"] = True
            root.quit()
            return
        if time.time() >= deadline:
            root.quit()
            return
        root.after(20, tick)

    root.after(0, tick)
    try:
        root.mainloop()
    except tk.TclError:
        pass
    return state["hit"] or ready()


class DeviceCenterUiTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        self.root.withdraw()
        self.addCleanup(self._destroy)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.config = Config(load_env=False)
        self.config.data_dir = base
        self.config.log_dir = base / "logs"
        self.config.db_path = base / "t.sqlite3"
        self.config.characters_dir = base / "characters"
        self.config.live2d_dir = base / "live2d"
        self.config.devices_dir = base / "devices"
        database = Database(base / "t.sqlite3")
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        self.agent = AgentCore(
            StubProvider(),
            memory,
            ContextManager(token_budget=800),
            persona=DefaultPersonaProvider(),
            training=LocalTrainingCollector(database),
        )
        self.pet = DesktopPet(
            self.agent,
            self.config,
            memory=memory,
            behavior=BehaviorManager(persona=self.agent.persona),
            root=self.root,
        )
        self.addCleanup(self._close_pet)
        self.panel = None
        self.stack = None

    def _destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _close_pet(self):
        try:
            self.pet.close_panel()
        except Exception:
            pass

    # ---------- 装配 ----------
    def attach_stack(self, confirm=True, start_worker=True):
        """装一条真的设备栈，接上一台模拟设备并连好。"""
        grants = GrantStore(Path(self.tmp.name) / "grants.json")
        manager = PermissionManager(
            confirm_callback=(lambda *a, **k: True) if confirm else None,
            grants=grants,
        )
        stack = build_stack(permissions=manager, start_worker=start_worker)
        stack.manager.connect("sim-1")
        self.agent.device_stack = stack
        self.stack = stack
        return stack

    def open_panel(self):
        self.panel = self.pet.open_devices()
        self.assertIsNotNone(self.panel)
        return self.panel

    def wait_task(self, task_id):
        return pump(self.root, lambda: self.stack.tasks.get(task_id).final, seconds=6.0)

    # ---------- 打开与刷新 ----------
    def test_panel_opens_embedded_and_is_reused(self):
        self.attach_stack()
        first = self.open_panel()
        self.assertIsInstance(first, DeviceWindow)
        self.assertTrue(first.embedded)
        self.assertTrue(first.win.winfo_exists())
        second = self.pet.open_devices()
        self.assertIs(second, first, "同一页应该只建一次，来回切不丢状态")

    def test_refresh_reports_simulated_connection(self):
        self.attach_stack()
        panel = self.open_panel()
        rows = panel.refresh()
        self.assertTrue(rows, "接了一台模拟设备，列表不该是空的")
        sim = [row for row in rows if row["name"] == "sim-1"]
        self.assertEqual(len(sim), 1)
        self.assertTrue(sim[0]["connected"])
        self.assertTrue(sim[0]["simulated"], "模拟设备必须自报是模拟的")
        self.assertIn("连着 1 台", panel.state_label.cget("text"))

    def test_action_panel_offers_registered_actions_only(self):
        self.attach_stack()
        panel = self.open_panel()
        rows = panel.refresh()
        names = {item["name"] for item in rows[0]["actions"]}
        self.assertIn("led.set", names)
        self.assertIn("servo.set_angle", names)
        self.assertNotIn("motor.spin", names, "没登记的动作不该出现在面板上")
        keys = {key for key in panel.fields}
        self.assertIn(("sim-1", "led.set", "channel"), keys)
        self.assertIn(("sim-1", "led.set", "on"), keys)

    # ---------- 执行 ----------
    def test_led_action_reaches_completed(self):
        stack = self.attach_stack()
        panel = self.open_panel()
        panel.refresh()
        panel.fields[("sim-1", "led.set", "channel")].variable.set("0")
        panel.fields[("sim-1", "led.set", "on")].variable.set(True)
        result = panel.run_action("sim-1", "led.set")
        self.assertEqual(result.get("status"), "queued", result)
        self.assertTrue(self.wait_task(result["id"]), "任务应该跑到收尾状态")
        task = stack.tasks.get(result["id"]).to_dict()
        self.assertEqual(task["status"], "completed", task)
        self.assertTrue(task["result"].get("simulated"))
        self.assertTrue(panel.refresh())

    def test_servo_out_of_range_is_refused_by_device(self):
        stack = self.attach_stack()
        panel = self.open_panel()
        panel.refresh()
        panel.fields[("sim-1", "servo.set_angle", "channel")].variable.set("0")
        panel.fields[("sim-1", "servo.set_angle", "angle_deg")].variable.set("200")
        result = panel.run_action("sim-1", "servo.set_angle")
        self.assertTrue(self.wait_task(result["id"]))
        task = stack.tasks.get(result["id"]).to_dict()
        self.assertNotEqual(task["status"], "completed", "范围外的角度不该算做成功")
        self.assertTrue(task.get("error"), "被拒要有原因")

    def test_unregistered_action_is_refused(self):
        stack = self.attach_stack()
        panel = self.open_panel()
        outcome = stack.tasks.submit(device="sim-1", action="motor.spin", params={}, wait=True)
        self.assertFalse(outcome.get("ok"))
        self.assertIn("没有登记", outcome.get("error") or "")
        self.assertIsNotNone(panel)

    # ---------- 权限 ----------
    def test_servo_needs_confirmation_and_led_does_not(self):
        stack = self.attach_stack(confirm=False)
        led = stack.tasks.submit(device="sim-1", action="led.set", params={"channel": 0, "on": True})
        self.assertTrue(led.get("ok"), led)
        servo = stack.tasks.submit(
            device="sim-1", action="servo.set_angle", params={"channel": 0, "angle": 90}
        )
        self.assertFalse(servo.get("ok"), "没有确认通道时舵机不能自己过")
        self.assertIn("权限", servo.get("error") or "")

    # ---------- 急停 ----------
    def test_emergency_stop_reports_each_device(self):
        self.attach_stack()
        panel = self.open_panel()
        panel.emergency_stop()
        hit = pump(self.root, lambda: "不是断电急停" in panel.hint.cget("text"), seconds=6.0)
        self.assertTrue(hit, panel.hint.cget("text"))
        self.assertIn("sim-1", panel.hint.cget("text"))
        self.assertIn("已停下", panel.hint.cget("text"))
        self.assertIn("不是断电急停", panel.hint.cget("text"))

    # ---------- 定时刷新 ----------
    def test_hidden_page_does_not_rebuild_widgets(self):
        self.attach_stack()
        panel = self.open_panel()
        panel.refresh()
        self.assertTrue(panel._on_screen(), "开着的时候该认这是当前页")
        panel._tick_once()
        panel.win.after_cancel(panel._tick)
        before = len(panel.body.winfo_children())
        self.assertGreater(before, 0)
        self.pet.close_panel()
        self.assertFalse(panel._on_screen(), "回到聊天后这页就不在屏幕上了")
        panel._tick_once()
        panel.win.after_cancel(panel._tick)
        self.assertEqual(
            len(panel.body.winfo_children()), before, "页面收起来后不该接着重建控件"
        )
        self.pet.open_devices()
        self.assertTrue(panel._on_screen(), "再打开要接着刷")

    # ---------- 串口与授权 ----------
    def test_the_port_row_lists_what_the_system_reports(self):
        self.attach_stack()
        panel = self.open_panel()
        fake = [
            {"port": "COM7", "description": "USB-SERIAL", "hwid": "", "source": "pyserial"},
            {"port": "COM9", "description": "", "hwid": "", "source": "pyserial"},
        ]
        with mock.patch("devices.ports.list_ports", lambda: fake):
            rows = panel.refresh_ports()
        self.assertEqual([row["port"] for row in rows], ["COM7", "COM9"])
        self.assertEqual(panel.port_var.get(), "COM7", "默认挑第一个列出来的口")
        self.assertIn("2 个", panel.port_note.cget("text"))

    def test_connecting_a_port_that_is_not_listed_is_refused(self):
        self.attach_stack()
        panel = self.open_panel()
        panel.ports = []
        panel.port_var.set("COM99")
        panel.use_serial_port()
        self.assertIn("先挑一个", panel.hint.cget("text"))

    def test_a_grant_can_be_seen_and_revoked_from_the_panel(self):
        stack = self.attach_stack()
        panel = self.open_panel()
        remembered = stack.permissions.remember("sim-1", "led.set")
        self.assertTrue(remembered["ok"], remembered)
        panel.refresh()
        self.assertEqual(len(stack.permissions.action_grants()), 1)
        removed = panel.revoke_grant({"tool": "device.action:sim-1:led.set", "mode": "always"})
        self.assertEqual(removed, 1)
        self.assertEqual(stack.permissions.action_grants(), [])

    # ---------- 降级 ----------
    def test_panel_degrades_without_device_stack(self):
        self.agent.device_stack = None
        panel = self.open_panel()
        self.assertIsNone(panel.refresh(), "没有设备模块时不该硬编出设备")
        self.assertIn("未接入", panel.state_label.cget("text"))

    def test_disconnect_then_refresh_shows_offline(self):
        self.attach_stack()
        panel = self.open_panel()
        panel.refresh()
        panel.disconnect("sim-1")
        rows = panel.refresh()
        sim = [row for row in rows if row["name"] == "sim-1"][0]
        self.assertFalse(sim["connected"])
        self.assertEqual(self.stack.tasks.counts().get("completed", 0), 0)


if __name__ == "__main__":
    unittest.main()
