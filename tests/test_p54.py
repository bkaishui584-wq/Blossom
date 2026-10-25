"""阶段 P54 验收：角色挡得住鼠标、点得到、渲染进程挂了能自己回来。

这一版修的两件事：

1. 角色不再是一块点不到的贴图。渲染进程读出角色当前画面的形状（掩码），
   交给透明宿主当碰撞体积：鼠标落在角色身上才算点中它，透明处照旧点得到桌面。
2. 渲染进程被杀掉、WebView2 崩掉时，宿主拿到的是一条路径字符串，
   重起必须认这个字符串。以前只认带 path 的模型对象，于是每次自愈都失败，
   桌面上只剩占位形象，角色看着就像摆设。
"""

import base64
import io
import os
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
from app import main as app_main  # noqa: E402
from avatar import web2d  # noqa: E402
from avatar import web2d_renderer  # noqa: E402
from database.db import Database  # noqa: E402
from memory.manager import MemoryManager  # noqa: E402
from memory.repository import MemoryRepository  # noqa: E402
from model.base import ChatDelta, ChatResult, ModelProvider, Usage  # noqa: E402
from security.config import Config  # noqa: E402
from ui.desktop import DesktopPet  # noqa: E402
from ui.pet_host import HIT_ALPHA, HIT_FILL, HIT_TAG, TRANSPARENT_KEY  # noqa: E402

RUNTIME = ROOT / "resources" / "live2d"


class StubProvider(ModelProvider):
    name = "stub"

    def is_available(self):
        return True

    def chat(self, messages, tools=None, temperature=0.7):
        return ChatResult(
            ok=True, content="在的。", usage=Usage(prompt_tokens=3, completion_tokens=2), model="stub"
        )

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


def pack_mask(cols, rows, cells):
    """把格子列表打成渲染器给出的那种掩码：按行排、位序从低到高。"""
    bits = bytearray((cols * rows + 7) // 8)
    for col, row in cells:
        index = row * cols + col
        bits[index >> 3] |= 1 << (index & 7)
    return base64.b64encode(bytes(bits)).decode("ascii"), bytes(bits)


class FakeProc:
    """替身子进程：两个管道立刻 EOF，宿主不会真的等到超时。"""

    def __init__(self):
        self.stdout = io.BytesIO(b"")
        self.stderr = io.BytesIO(b"")
        self.stdin = io.BytesIO()
        self.returncode = 0

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        return None


class ModelFileTests(unittest.TestCase):
    """界面传模型对象，自愈重起时手上只有路径字符串，两种都要认。"""

    def test_a_model_object_gives_its_path(self):
        model = mock.Mock(path=str(RUNTIME / "renderer.html"))
        self.assertEqual(web2d.model_file(model), str(RUNTIME / "renderer.html"))

    def test_a_plain_path_stands_on_its_own(self):
        self.assertEqual(web2d.model_file("D:/live2d/a.model3.json"), "D:/live2d/a.model3.json")
        self.assertEqual(
            web2d.model_file(Path("D:/live2d/a.model3.json")), str(Path("D:/live2d/a.model3.json"))
        )

    def test_nothing_and_junk_give_an_empty_path(self):
        self.assertEqual(web2d.model_file(None), "")
        self.assertEqual(web2d.model_file(mock.Mock()), "")
        self.assertEqual(web2d.model_file(mock.Mock(path=7)), "")

    def test_start_accepts_the_path_string_the_watchdog_has(self):
        with mock.patch.object(web2d, "_webview_ready", return_value=True), mock.patch.object(
            web2d.subprocess, "Popen", return_value=FakeProc()
        ) as popen, mock.patch.object(web2d.WebLive2DHost, "_start_watcher"):
            host = web2d.WebLive2DHost(runtime=RUNTIME)
            result = host.start(str(RUNTIME / "renderer.html"))
            self.assertTrue(result["ok"], result)
            self.assertIn(str(RUNTIME / "renderer.html"), popen.call_args.args[0])
            host.stop(timeout=0.1)

    def test_start_says_plainly_when_there_is_no_model(self):
        with mock.patch.object(web2d, "_webview_ready", return_value=True):
            host = web2d.WebLive2DHost(runtime=RUNTIME)
            self.assertEqual(host.start(None)["error"], web2d.NO_MODEL)
            self.assertEqual(host.start("")["error"], web2d.NO_MODEL)

    def test_a_path_that_is_not_there_is_reported_as_such(self):
        with mock.patch.object(web2d, "_webview_ready", return_value=True):
            host = web2d.WebLive2DHost(runtime=RUNTIME)
            result = host.start(str(RUNTIME / "not-here.model3.json"))
        self.assertFalse(result["ok"])
        self.assertIn("模型文件不存在", result["error"])


class RestartAfterCrashTests(unittest.TestCase):
    """渲染进程被杀掉之后必须真的重新起来，而不是留个占位形象。"""

    def setUp(self):
        self.hosts = []
        self.addCleanup(self._stop_watchers)

    def _stop_watchers(self):
        for host in self.hosts:
            host._stopped = True
            host._proc = None

    def host(self):
        with mock.patch.object(web2d, "_webview_ready", return_value=True):
            host = web2d.WebLive2DHost(runtime=RUNTIME)
        self.hosts.append(host)
        return host

    def test_the_watchdog_restarts_with_the_path_it_already_has(self):
        host = self.host()
        notes = []
        host.set_sink(lambda name, payload: notes.append(name))
        path = str(RUNTIME / "renderer.html")
        with mock.patch.object(web2d.subprocess, "Popen", return_value=FakeProc()) as popen, mock.patch.object(
            host, "_start_watcher"
        ):
            self.assertTrue(host._recover("角色画面已经关闭", path))
        self.assertIn(path, popen.call_args.args[0])
        self.assertEqual(host._model_path, path)
        self.assertIn("live2d_restarting", notes)

    def test_a_restart_without_a_path_touches_nothing(self):
        host = self.host()
        with mock.patch.object(web2d.subprocess, "Popen") as popen, mock.patch.object(host, "start") as start:
            self.assertFalse(host._recover("角色画面已经关闭", None))
        start.assert_not_called()
        popen.assert_not_called()

    def test_a_model_file_that_vanished_is_reported_in_the_log(self):
        host = self.host()
        logs = []
        host.logger = mock.Mock()
        host.logger.warning.side_effect = lambda *a: logs.append(a[0] % a[1:])
        with mock.patch.object(web2d.subprocess, "Popen") as popen:
            self.assertFalse(host._recover("角色画面已经关闭", str(RUNTIME / "gone.model3.json")))
        self.assertTrue(logs[-1].startswith("live2d restart failed:"))
        self.assertIn("模型文件不存在", logs[-1])
        popen.assert_not_called()


class TkPetTestCase(unittest.TestCase):
    """真开一个（隐身的）Tk 根窗口，宿主和碰撞体积都是真的。"""

    def setUp(self):
        try:
            self.root = tk.Tk()
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        self.root.withdraw()
        self.addCleanup(self._safe_destroy)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.config = Config(load_env=False)
        self.config.data_dir = base
        self.config.log_dir = base / "logs"
        self.config.db_path = base / "t.sqlite3"
        self.config.voices_dir = base / "voices"
        self.config.characters_dir = base / "characters"
        self.config.live2d_dir = base / "live2d"
        self.config.devices_dir = base / "devices"
        self.config.tray = False
        database = Database(self.config.db_path)
        database.migrate()
        self.memory = MemoryManager(MemoryRepository(database))
        agent = AgentCore(StubProvider(), self.memory, ContextManager(token_budget=800))
        self.pet = DesktopPet(agent, self.config, memory=self.memory, root=self.root)

    def _safe_destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def alpha(self):
        return float(self.root.wm_attributes("-alpha"))

    def hit_rects(self):
        return list(self.pet.host.canvas.find_withtag(HIT_TAG))

    def mask_payload(self, cols, rows, cells):
        data, bits = pack_mask(cols, rows, cells)
        return {"data": {"cols": cols, "rows": rows, "data": data}}, bits


class HitVolumeTests(TkPetTestCase):
    """角色形状决定哪里接得住鼠标，别的地方照旧穿到桌面。"""

    def test_the_mask_becomes_the_body_outline(self):
        payload, bits = self.mask_payload(4, 4, [(0, 0), (1, 0), (2, 1)])
        self.assertTrue(self.pet._apply_hit_volume(payload))
        cols, rows, stored = self.pet.host.hit_mask()
        self.assertEqual((cols, rows), (4, 4))
        self.assertEqual(bytes(stored), bits)
        self.assertTrue(self.pet.host._hit_on(0, 0))
        self.assertTrue(self.pet.host._hit_on(1, 0))
        self.assertTrue(self.pet.host._hit_on(2, 1))
        self.assertFalse(self.pet.host._hit_on(3, 0))
        self.assertFalse(self.pet.host._hit_on(0, 1))

    def test_the_body_is_painted_as_rows_not_one_block_per_cell(self):
        payload, _bits = self.mask_payload(4, 4, [(0, 0), (1, 0), (2, 1)])
        self.pet._apply_hit_volume(payload)
        # (0,0)+(1,0) 连成一条，(2,1) 单独一条。
        self.assertEqual(len(self.hit_rects()), 2)

    def test_the_window_goes_nearly_invisible_but_still_takes_the_mouse(self):
        payload, _bits = self.mask_payload(4, 4, [(1, 1)])
        self.pet._apply_hit_volume(payload)
        self.assertAlmostEqual(self.alpha(), HIT_ALPHA, places=6)

    def test_a_mask_event_from_the_renderer_reaches_the_host(self):
        payload, _bits = self.mask_payload(2, 2, [(0, 0)])
        self.pet._handle_live2d_event("live2d_mask", payload)
        self.assertIsNotNone(self.pet.host.hit_mask())

    def test_junk_is_refused_and_leaves_the_old_body_alone(self):
        payload, _bits = self.mask_payload(2, 2, [(0, 0)])
        self.pet._apply_hit_volume(payload)
        for junk in (None, {}, {"data": {}}, {"data": {"cols": 0, "rows": 4, "data": "AA=="}}):
            self.assertFalse(self.pet._apply_hit_volume(junk))
        self.assertEqual(self.pet.host.hit_mask()[2], bytes([1]))

    def test_clearing_the_mask_gives_the_placeholder_its_clicks_back(self):
        payload, _bits = self.mask_payload(4, 4, [(0, 0)])
        self.pet._apply_hit_volume(payload)
        self.pet.host.clear_hit_mask()
        self.assertIsNone(self.pet.host.hit_mask())
        self.assertEqual(self.hit_rects(), [])
        self.assertEqual(self.alpha(), 1.0)

    def test_hiding_the_pet_hides_its_body_outline_too(self):
        payload, _bits = self.mask_payload(4, 4, [(0, 0), (1, 0)])
        self.pet._apply_hit_volume(payload)
        self.pet.host.set_hidden(True)
        self.assertEqual(self.hit_rects(), [])
        self.assertEqual(self.alpha(), 1.0)
        self.pet.host.set_hidden(False)
        self.assertTrue(self.hit_rects())

    def test_a_dead_renderer_leaves_a_transparent_drag_area(self):
        payload, _bits = self.mask_payload(4, 4, [(0, 0), (1, 0)])
        self.pet._apply_hit_volume(payload)
        for name in ("live2d_restarting", "live2d_exit", "live2d_error"):
            self.pet._apply_hit_volume(payload)
            self.pet._handle_live2d_event(name, {"reason": "角色画面已经关闭"})
            self.assertIsNotNone(self.pet.host.hit_mask(), name)
            self.assertTrue(self.hit_rects(), name)

    def test_redrawing_the_placeholder_keeps_the_body_outline(self):
        payload, _bits = self.mask_payload(4, 4, [(0, 0), (1, 0)])
        self.pet._apply_hit_volume(payload)
        self.pet.pet.render("happy")
        self.pet.pet.set_blank(False)
        self.pet.pet.set_blank(True)
        self.assertEqual(len(self.hit_rects()), 1)


class TransparencyWatchTests(TkPetTestCase):
    """载体的"隐形"会丢：丢了要自己补回来，补不回来也不能在桌上留一块黑。"""

    def test_the_body_layer_is_never_painted_in_the_key_color(self):
        # 碰撞层一旦和色键同色，系统就当那里是透明区，鼠标直接穿过去，桌宠点不动了。
        self.assertNotEqual(HIT_FILL, TRANSPARENT_KEY)

    def test_the_self_check_leaves_a_healthy_window_alone(self):
        payload, _bits = self.mask_payload(4, 4, [(0, 0), (1, 0)])
        self.pet._apply_hit_volume(payload)
        before = self.hit_rects()
        self.assertTrue(self.pet.host.assert_alpha())
        self.assertEqual(self.hit_rects(), before)
        self.assertAlmostEqual(self.alpha(), HIT_ALPHA, places=6)

    def test_an_alpha_wiped_by_the_system_is_put_back(self):
        payload, _bits = self.mask_payload(4, 4, [(0, 0), (1, 0)])
        self.pet._apply_hit_volume(payload)
        self.root.wm_attributes("-alpha", 1.0)  # 系统把透明度清掉了
        self.assertTrue(self.pet.host.assert_alpha())
        self.assertAlmostEqual(self.alpha(), HIT_ALPHA, places=6)
        self.assertTrue(self.hit_rects())

    def test_a_window_that_cannot_be_made_invisible_drops_the_black_layer(self):
        payload, _bits = self.mask_payload(4, 4, [(0, 0), (1, 0)])
        self.pet._apply_hit_volume(payload)
        with mock.patch.object(self.pet.host, "_layered_state", return_value=(False, False, 1.0)):
            self.assertFalse(self.pet.host.assert_alpha())
        self.assertEqual(self.hit_rects(), [])  # 一层黑都不留在桌面上
        self.assertIsNotNone(self.pet.host.hit_mask())  # 掩码留着，恢复后还能补回来

    def test_the_layer_comes_back_once_transparency_returns(self):
        payload, _bits = self.mask_payload(4, 4, [(0, 0), (1, 0)])
        self.pet._apply_hit_volume(payload)
        with mock.patch.object(self.pet.host, "_layered_state", return_value=(False, False, 1.0)):
            self.pet.host.assert_alpha()
        self.assertEqual(self.hit_rects(), [])
        self.assertTrue(self.pet.host.assert_alpha())
        self.assertTrue(self.hit_rects())
        self.assertAlmostEqual(self.alpha(), HIT_ALPHA, places=6)


class ClickReactionTests(TkPetTestCase):
    """点一下桌宠，角色当场有反应；反应接口坏了也不能影响点击本身。"""

    class Recorder:
        def __init__(self, result=None, boom=False):
            self.calls = []
            self.result = {"ok": True} if result is None else result
            self.boom = boom

        def react(self, name="click"):
            self.calls.append(name)
            if self.boom:
                raise RuntimeError("渲染进程不在")
            return self.result

    def test_a_click_asks_the_character_to_react(self):
        live2d = self.Recorder()
        self.pet.live2d = live2d
        self.pet.host.on_click(None)
        self.assertEqual(live2d.calls, ["click"])

    def test_a_double_click_and_a_hover_have_their_own_reactions(self):
        live2d = self.Recorder()
        self.pet.live2d = live2d
        self.pet.host.on_double_click(None)
        self.pet.host.on_pointer_near(None)
        self.assertEqual(live2d.calls, ["double_click", "hover"])

    def test_a_reaction_that_raises_never_breaks_the_click(self):
        self.pet.live2d = self.Recorder(boom=True)
        self.assertIsNone(self.pet._live2d_react("click"))
        self.pet.host.on_click(None)

    def test_a_renderer_that_is_not_there_is_not_an_error(self):
        self.pet.live2d = None
        self.assertIsNone(self.pet._live2d_react("click"))

    def test_a_refused_reaction_is_reported_as_refused(self):
        self.pet.live2d = self.Recorder(result={"ok": False, "error": web2d.NOT_READY})
        result = self.pet._live2d_react("click")
        self.assertFalse(result["ok"])


class RendererReactCommandTests(unittest.TestCase):
    """父进程发的 react 命令要真的落到页面里的 window.Pet.react。"""

    def renderer(self):
        r = web2d_renderer.Renderer.__new__(web2d_renderer.Renderer)
        r.state = {"ready": True, "visible": True, "scale": 1.0}
        r.forwarded = []
        r.quit = mock.Mock()
        r._forward = lambda name, script: r.forwarded.append((name, script))
        return r

    def test_a_click_becomes_a_page_call(self):
        r = self.renderer()
        self.assertTrue(r.handle({"cmd": "react", "name": "click"}))
        self.assertEqual(r.forwarded, [("react", 'window.Pet.react("click")')])

    def test_an_empty_name_falls_back_to_a_click(self):
        r = self.renderer()
        r.handle({"cmd": "react"})
        self.assertEqual(r.forwarded[0][1], 'window.Pet.react("click")')


class StartupWatchdogPatienceTests(unittest.TestCase):
    """启动慢不等于启动失败：日志还在写就继续等，不再弹失败提示。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_file = Path(self.tmp.name) / "deskpet.log"
        self.log_file.write_text("boot\n", encoding="utf-8")

    def run_watchdog(self, seconds=1, rounds=4, fresh=True):
        shown = []
        freshness = mock.Mock(return_value=fresh)
        with mock.patch.object(
            app_main, "deskpet_window_exists", return_value=False
        ), mock.patch.object(
            app_main, "_fatal_reported", False
        ), mock.patch.object(
            app_main, "_log_is_fresh", freshness
        ), mock.patch.object(
            app_main, "notify_startup_stalled", side_effect=lambda: shown.append(1)
        ):
            thread = app_main.start_startup_watchdog(
                None, seconds=seconds, log_file=self.log_file, rounds=rounds
            )
            thread.join(timeout=30)
        return shown, freshness.call_count

    def test_a_fresh_log_counts_as_still_working(self):
        self.assertTrue(app_main._log_is_fresh(self.log_file, 30))

    def test_an_old_log_does_not(self):
        old = time.time() - 600
        os.utime(self.log_file, (old, old))
        self.assertFalse(app_main._log_is_fresh(self.log_file, 30))

    def test_a_missing_or_unknown_log_is_not_a_reason_to_wait(self):
        missing = Path(self.tmp.name) / "nope.log"
        self.assertFalse(app_main._log_is_fresh(missing, 30))
        self.assertFalse(app_main._log_is_fresh(None, 30))

    def test_a_writing_start_is_given_more_windows_before_it_is_reported(self):
        # rounds=4 时先等三个额外窗口，最后一轮才开口。
        shown, calls = self.run_watchdog(seconds=1, rounds=4, fresh=True)
        self.assertEqual(shown, [1])
        self.assertEqual(calls, 3)

    def test_a_quiet_start_is_reported_after_one_window(self):
        shown, calls = self.run_watchdog(seconds=1, rounds=4, fresh=False)
        self.assertEqual(shown, [1])
        self.assertEqual(calls, 1)

    def test_patience_is_bounded(self):
        # 日志一直在写也不能永远等下去，轮数用完照样开口。
        shown, calls = self.run_watchdog(seconds=1, rounds=2, fresh=True)
        self.assertEqual(shown, [1])
        self.assertEqual(calls, 1)

    def test_the_entry_point_hands_over_the_real_log_file(self):
        captured = {}
        config = mock.Mock()
        config.log_dir = Path(self.tmp.name)

        def fake_watchdog(logger=None, **kwargs):
            captured.update(kwargs)
            return None

        with mock.patch.object(
            app_main, "bootstrap", return_value=(config, mock.Mock(), mock.Mock(), mock.Mock())
        ), mock.patch.object(
            app_main, "acquire_single_instance", return_value=True
        ), mock.patch.object(
            app_main, "start_startup_watchdog", side_effect=fake_watchdog
        ), mock.patch.object(
            app_main, "start_ui", return_value=0
        ):
            self.assertEqual(app_main.main([]), 0)
        self.assertEqual(captured.get("log_file"), Path(self.tmp.name) / "deskpet.log")


if __name__ == "__main__":
    unittest.main()
