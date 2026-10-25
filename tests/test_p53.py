"""阶段 P53 验收：桌宠真的能点、能演、失败也能自己爬起来。

这一版修的三件事，都在这里用真实行为兜住：

1. 启动失败只会有一个弹窗出口（app/main.py 的 _message_box）。
   以前测试会真的调用 user32.MessageBoxW 弹出模态框，把测试进程和用户一起卡住。
2. 角色画面不再是一张静态贴图：resources/live2d/pet_page.js 按显示器的节奏跑参数
   循环（60fps，不是卡死的 30fps），做眨眼、呼吸、摇头、视线跟随，点一下有反应。
   视线只有一个来源（宿主进程推的全局光标）；页面自己也收得到鼠标事件，但绝不能再
   拿窗口坐标换算一遍，否则光标一进窗口头就会蹿一下。
   这一项用 node 起一个假 DOM，跑真实页面代码，看参数有没有真的被写出来。
3. 渲染子进程假死、白屏、被关掉时，宿主自己重启它，重启次数过多就退回占位形象，
   而不是让桌面上只剩一块空白或者多出一个看不见的僵尸进程。
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import main as app_main  # noqa: E402
from avatar import web2d  # noqa: E402
from avatar import web2d_renderer  # noqa: E402

RUNTIME = ROOT / "resources" / "live2d"
PAGE = RUNTIME / "pet_page.js"
RENDER = RUNTIME / "pet_render.js"
SPEC = ROOT / "Blossom.spec"
DRAFT_CDI = ROOT / "live2d" / "live2D" / "live2D.cdi3.json"

# 用户那份草稿的参数表（min / max / 默认值按 Cubism 导出）。页面控制器只动
# 真实存在的参数，所以假 DOM 必须给出同一批 id。
PARAMS = [
    {"id": "ParamAngleX", "min": -30, "max": 30, "def": 0},
    {"id": "ParamAngleY", "min": -30, "max": 30, "def": 0},
    {"id": "ParamAngleZ", "min": -30, "max": 30, "def": 0},
    {"id": "ParamEyeLOpen", "min": 0, "max": 1, "def": 1},
    {"id": "ParamEyeLSmile", "min": 0, "max": 1, "def": 0},
    {"id": "ParamEyeROpen", "min": 0, "max": 1, "def": 1},
    {"id": "ParamEyeRSmile", "min": 0, "max": 1, "def": 0},
    {"id": "ParamEyeBallX", "min": -1, "max": 1, "def": 0},
    {"id": "ParamEyeBallY", "min": -1, "max": 1, "def": 0},
    {"id": "ParamBrowLY", "min": -1, "max": 1, "def": 0},
    {"id": "ParamBrowRY", "min": -1, "max": 1, "def": 0},
    {"id": "ParamBrowLX", "min": -1, "max": 1, "def": 0},
    {"id": "ParamBrowRX", "min": -1, "max": 1, "def": 0},
    {"id": "ParamBrowLAngle", "min": -1, "max": 1, "def": 0},
    {"id": "ParamBrowRAngle", "min": -1, "max": 1, "def": 0},
    {"id": "ParamBrowLForm", "min": -1, "max": 1, "def": 0},
    {"id": "ParamBrowRForm", "min": -1, "max": 1, "def": 0},
    {"id": "ParamMouthForm", "min": -1, "max": 1, "def": 0},
    {"id": "ParamMouthOpenY", "min": 0, "max": 1, "def": 0},
    {"id": "ParamCheek", "min": 0, "max": 1, "def": 0},
    {"id": "ParamBodyAngleX", "min": -10, "max": 10, "def": 0},
    {"id": "ParamBodyAngleY", "min": -10, "max": 10, "def": 0},
    {"id": "ParamBodyAngleZ", "min": -10, "max": 10, "def": 0},
    {"id": "Parambodyupper", "min": -1, "max": 1, "def": 0},
    {"id": "ParamBreath", "min": 0, "max": 1, "def": 0},
    {"id": "ParamHairFront", "min": -1, "max": 1, "def": 0},
    {"id": "ParamHairSide", "min": -1, "max": 1, "def": 0},
    {"id": "ParamHairBack", "min": -1, "max": 1, "def": 0},
]

_TMP = None


def setUpModule():
    global _TMP
    _TMP = tempfile.TemporaryDirectory(prefix="deskpet-p53-")


def tearDownModule():
    if _TMP is not None:
        _TMP.cleanup()


def cursor_samples():
    """宿主真正会推给页面的那串光标位置：光标扫过屏幕，代码照常节流。

    用的是 Hotspot._push_pointer 本人，不是另抄一份公式，这样节流、视场、
    死区三件事哪一件被改回去，下面的测试都会红。
    """
    import math

    width, height = 450, 630      # 桌宠窗口客户区，和真机一个量级
    left, top = 2214, 663         # 窗口在屏幕上的位置
    span_seconds = 3.0
    pushed = []
    hotspot = web2d_renderer.Hotspot(user32=None, on_hover=lambda *_: None)
    hotspot.on_pointer = lambda nx, ny: pushed.append((nx, ny))
    steps = int(span_seconds / web2d_renderer.POLL_SECONDS)
    tick_ms = int(web2d_renderer.POLL_SECONDS * 1000)
    for i in range(steps):
        ratio = min(1.0, (i * web2d_renderer.POLL_SECONDS) / span_seconds)
        screen_x = 100 + (2450 - 100) * ratio
        screen_y = 900 + 60 * math.sin(ratio * 6.0)
        hotspot._push_pointer(screen_x - left, screen_y - top, (width, height))
    per_push = tick_ms * web2d_renderer.POINTER_EVERY
    return [[(index + 1) * per_push, nx, ny] for index, (nx, ny) in enumerate(pushed)]


def node_exe():
    return shutil.which("node")


class PageHarness:
    """在 node 里跑一遍真实页面代码，把观察到的参数写回 Python。"""

    _cache = None

    @classmethod
    def run(cls):
        if cls._cache is not None:
            return cls._cache
        exe = node_exe()
        if not exe:
            raise unittest.SkipTest("这台机器上没有 node，跳过页面行为检查")
        harness = Path(_TMP.name) / "p53_harness.js"
        harness.write_text(
            HARNESS.replace("__CURSOR_SAMPLES__", json.dumps(cursor_samples(), ensure_ascii=False)),
            encoding="utf-8",
        )
        proc = subprocess.run(
            [exe, str(harness), str(PAGE), json.dumps(PARAMS, ensure_ascii=False)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
        )
        if proc.returncode != 0:
            raise AssertionError("node 跑不动页面控制器：%s" % (proc.stderr or "")[-500:])
        data = json.loads(proc.stdout)
        if "failed" in data:
            raise AssertionError("页面控制器抛错：%s" % data["failed"])
        cls._cache = data
        return data


def run_page():
    try:
        return PageHarness.run()
    except unittest.SkipTest:
        raise
    except FileNotFoundError:
        raise unittest.SkipTest("没有 node")


class PageAnimatesTests(unittest.TestCase):
    """角色必须自己在动，不再是一张贴图。"""

    @classmethod
    def setUpClass(cls):
        cls.out = run_page()

    def test_the_page_loaded_the_model_and_knows_its_parameters(self):
        self.assertTrue(self.out["ready"])
        self.assertEqual(self.out["paramCount"], len(PARAMS))

    def test_idle_breathing_writes_real_parameters(self):
        self.assertGreater(self.out["breathSpan"], 0.5)

    def test_it_blinks_on_both_eyes(self):
        self.assertLess(self.out["eyeLeftMin"], 0.4)
        self.assertLess(self.out["eyeRightMin"], 0.4)
        self.assertGreaterEqual(self.out["blinks"], 1)

    def test_the_head_and_eyes_follow_the_pointer(self):
        self.assertGreater(self.out["gazeRightX"], 0.2)
        self.assertLess(self.out["gazeLeftX"], -0.2)
        self.assertGreater(self.out["headRightX"], 2.0)
        self.assertLess(self.out["headLeftX"], -2.0)

    def test_the_loop_reports_a_real_frame_rate(self):
        self.assertGreaterEqual(self.out["metrics"]["frames"], 100)
        self.assertGreater(self.out["metrics"]["fps"], 20)

    def test_the_loop_keeps_up_with_the_display(self):
        # 显示器 16.7 毫秒一帧，页面就得出 60fps 的帧；以前固定砍到 30fps，
        # 帧间隔在 30 和 40 毫秒之间来回，光标匀速划过时看着是一格一格。
        self.assertGreaterEqual(self.out["displayFrames"], 100)

    def test_the_page_does_not_invent_its_own_pointer(self):
        self.assertFalse(self.out["pointerAfterOwnMouseMove"])


class PageReducedMotionTests(unittest.TestCase):
    """开了减少动态效果：不再自己摆，也不再呼吸，但还眨眼睛。"""

    @classmethod
    def setUpClass(cls):
        cls.out = run_page()

    def test_the_page_reports_which_mode_it_is_in(self):
        self.assertEqual(self.out["motionOff"], "reduced")
        self.assertEqual(self.out["motionBackOn"], "full")

    def test_breathing_stops(self):
        self.assertGreater(self.out["motionOnBreathSpan"], 0.5)
        self.assertLess(self.out["calmBreathSpan"], 0.05)

    def test_the_idle_sway_stops(self):
        self.assertLess(self.out["calmSwaySpan"], 0.02)

    def test_it_still_blinks(self):
        self.assertLess(self.out["calmEyeMin"], 0.4)
        self.assertGreaterEqual(self.out["calmBlinks"], 1)


class PageHeadMotionTests(unittest.TestCase):
    """头部怎么动：光标来得稀也连着走，不动的时候自己轻轻摆，身体跟着转。"""

    @classmethod
    def setUpClass(cls):
        cls.out = run_page()

    def test_the_head_keeps_walking_between_cursor_updates(self):
        out = self.out
        self.assertGreater(out["headFeedFrames"], 40)
        # 光标一次跨过大半屏，也不许出现一格跳好几度的台阶
        self.assertLess(out["headFeedJump"], 0.8, out["headFeedJump"])

    def test_the_head_still_covers_a_real_range(self):
        self.assertGreater(self.out["headFeedTravel"], 2.0)

    def test_the_idle_sway_is_visible_and_never_frozen(self):
        out = self.out
        self.assertGreater(out["swaySpanX"], 0.5)
        self.assertGreater(out["swaySpanZ"], 0.5)
        self.assertGreater(out["swayMovingPct"], 50)
        self.assertLess(out["swayMaxJump"], 0.1)

    def test_the_body_turns_with_the_head(self):
        self.assertGreater(self.out["swayBodySpan"], 0.2)

    def test_the_follow_does_not_overshoot(self):
        out = self.out
        self.assertGreater(out["headSettleLast"], 2.0)
        self.assertLess(out["headSettleOvershoot"], 0.5)

    def test_reduced_motion_stops_the_whole_sway(self):
        out = self.out
        self.assertLess(out["calmSwaySpanX"], 0.02)
        self.assertLess(out["calmSwaySpanBody"], 0.02)


class PointerCadenceTests(unittest.TestCase):
    """宿主把光标推给页面的节奏与视场：左右摆不顺的根子在这一层。"""

    SIZE = (450, 630)

    def setUp(self):
        self.pushed = []
        self.hot = web2d_renderer.Hotspot(user32=None, on_hover=lambda *_: None)
        self.hot.on_pointer = lambda nx, ny: self.pushed.append((nx, ny))

    def push(self, px, py):
        """推一次光标。节流是每 N 次轮询发一次，这里对齐到会发的那一次。"""
        self.hot._pointer_tick = web2d_renderer.POINTER_EVERY - 1
        self.hot._push_pointer(px, py, self.SIZE)

    def test_the_page_hears_about_the_cursor_often_enough(self):
        width, height = self.SIZE
        steps = 50
        for index in range(steps):
            self.hot._push_pointer(-200 + index * 20, 0, self.SIZE)
        per_second = len(self.pushed) / (steps * web2d_renderer.POLL_SECONDS)
        self.assertGreater(per_second, 8.0, per_second)

    def test_the_pointer_arrives_at_a_smooth_cadence(self):
        steps = 50
        for index in range(steps):
            self.hot._push_pointer(-200 + index * 20, 0, self.SIZE)
        per_second = len(self.pushed) / (steps * web2d_renderer.POLL_SECONDS)
        self.assertGreater(per_second, 20.0, per_second)

    def test_the_field_reaches_further_than_the_pet_window(self):
        self.assertGreater(web2d_renderer.POINTER_FIELD, 2.0)

    def test_the_window_edge_is_not_the_extreme(self):
        # 光标停在窗口边上：老算法这里已经是 ±1，头贴死极限；现在只有三分之一。
        self.push(self.SIZE[0], self.SIZE[1] / 2)
        nx = self.pushed[-1][0]
        self.assertAlmostEqual(nx, 0.5 / (web2d_renderer.POINTER_FIELD / 2.0), places=2)
        self.assertLess(nx, 0.6)

    def test_the_direction_points_at_the_cursor(self):
        self.push(self.SIZE[0], self.SIZE[1] / 2)
        self.assertGreater(self.pushed[-1][0], 0)
        self.push(0, self.SIZE[1] / 2)
        self.assertLess(self.pushed[-1][0], 0)

    def test_a_still_cursor_is_not_resent(self):
        self.hot._pointer_tick = web2d_renderer.POINTER_EVERY - 1
        for _ in range(3):
            self.hot._push_pointer(300, 100, self.SIZE)
        self.assertEqual(len(self.pushed), 1)


class PageEmotionTests(unittest.TestCase):
    """情绪走参数演出，不认识的情绪如实说没有。"""

    @classmethod
    def setUpClass(cls):
        cls.out = run_page()

    def test_happy_and_sad_look_different(self):
        self.assertGreater(self.out["cheekHappy"], 0.4)
        self.assertLess(self.out["mouthSad"], -0.3)

    def test_an_unknown_emotion_is_refused_not_invented(self):
        result = self.out["emotionUnknown"]
        self.assertFalse(result["ok"])
        self.assertIn("没有这个情绪", result["error"])

    def test_a_click_has_a_visible_reaction(self):
        self.assertGreater(self.out["reactCheek"], 0.2)

    def test_long_press_has_its_own_visible_reaction(self):
        out = self.out
        self.assertEqual(out["reactLongPress"]["reaction"], "long_press")
        self.assertGreater(out["longPressSwing"], out["idleSwing"])
        self.assertGreater(out["longPressSwing"], 0.15)

    def test_dropping_the_pet_has_its_own_visible_reaction(self):
        out = self.out
        self.assertEqual(out["reactDragEnd"]["reaction"], "drag_end")
        self.assertGreater(out["dragEndTilt"], out["idleTilt"])
        self.assertGreater(out["dragEndTilt"], 0.15)

    def test_coming_close_only_tilts_the_head_a_little(self):
        out = self.out
        self.assertEqual(out["reactHover"]["reaction"], "hover")
        self.assertGreater(out["hoverSwing"], out["idleSwing"])
        # 靠近不是长按，摆幅要明显小一截，不然就成抽搐了。
        self.assertLess(out["hoverSwing"], out["longPressSwing"] * 0.6, out["hoverSwing"])

    def test_missing_expression_and_motion_are_reported_plainly(self):
        self.assertFalse(self.out["expression"]["ok"])
        self.assertIn("表情", self.out["expression"]["error"])
        self.assertFalse(self.out["motion"]["ok"])
        self.assertIn("动作", self.out["motion"]["error"])


class PageInteractionTests(unittest.TestCase):
    """点得中、拖得动、右键出菜单，碰撞体积跟着姿势更新。"""

    @classmethod
    def setUpClass(cls):
        cls.out = run_page()

    def test_the_hit_mask_covers_the_body_and_not_the_empty_corners(self):
        self.assertTrue(self.out["hitInside"])
        self.assertFalse(self.out["hitOutside"])

    def test_click_drag_and_right_click_reach_the_host(self):
        for name in ("click", "drag_begin", "drag", "drag_end", "right_click", "double_click"):
            self.assertIn(name, self.out["events"])

    def test_the_mask_is_rebuilt_after_the_pose_moves(self):
        self.assertTrue(self.out["maskReady"])

    def test_the_page_can_hand_over_a_picture_of_itself(self):
        self.assertEqual(self.out["capture"], "data:image/png;base64,VEVTVA==")

    def test_stop_is_clean(self):
        self.assertTrue(self.out["stopped"])


class RendererLiteralTests(unittest.TestCase):
    """%r 会把 None 写成 JS 的 None，那是一次语法错误。这里锁死。"""

    def test_none_becomes_null(self):
        self.assertEqual(web2d_renderer.Renderer._js(None), "null")

    def test_chinese_survives(self):
        text = web2d_renderer.Renderer._js("微笑")
        self.assertIn("微笑", text)
        self.assertEqual(json.loads(text), "微笑")

    def test_numbers_booleans_and_objects_round_trip(self):
        self.assertEqual(json.loads(web2d_renderer.Renderer._js(1.5)), 1.5)
        self.assertEqual(web2d_renderer.Renderer._js(True), "true")
        self.assertEqual(json.loads(web2d_renderer.Renderer._js({"a": 1})), {"a": 1})

    def test_something_unserialisable_degrades_to_null(self):
        self.assertEqual(web2d_renderer.Renderer._js(object()), "null")


class ProbeCommandTests(unittest.TestCase):
    """开发者面板与自检脚本用的探针命令，不能把渲染进程问崩。"""

    def renderer(self):
        r = web2d_renderer.Renderer.__new__(web2d_renderer.Renderer)
        r.state = {"ready": True, "visible": True, "scale": 1.0}
        r.forwarded = []
        r.scripts = []
        r.quit = mock.Mock()
        r._forward = lambda name, script: r.forwarded.append((name, script))
        r._send = lambda message: r.scripts.append(message)
        return r

    def test_capture_asks_the_page_for_a_picture(self):
        r = self.renderer()
        self.assertTrue(r.handle({"cmd": "capture"}))
        self.assertEqual(r.forwarded[0][0], "capture")
        self.assertIn("Pet.capture", r.forwarded[0][1])

    def test_metrics_and_samples_are_answered(self):
        r = self.renderer()
        r.metrics = lambda: {"fps": 29}
        self.assertTrue(r.handle({"cmd": "samples"}))
        self.assertEqual(r.forwarded[0], ("samples", "window.Pet.samples()"))
        self.assertTrue(r.handle({"cmd": "metrics"}))

    def test_mask_without_a_mask_says_so(self):
        r = self.renderer()
        r.state["mask"] = None
        self.assertTrue(r.handle({"cmd": "mask"}))

    def test_an_unknown_command_is_ignored(self):
        r = self.renderer()
        self.assertTrue(r.handle({"cmd": "nonsense"}))

    def test_quit_stops_the_loop(self):
        r = self.renderer()
        self.assertFalse(r.handle({"cmd": "quit"}))
        r.quit.set.assert_called_once()


class StartupWatchdogTests(unittest.TestCase):
    """界面迟迟不出现时只说一句人话，并且只走一个出口。"""

    def test_a_missing_window_gets_one_plain_sentence(self):
        shown = []
        with mock.patch.object(app_main, "deskpet_window_exists", return_value=False), mock.patch.object(
            app_main, "_fatal_reported", False
        ), mock.patch.object(app_main, "_message_box", side_effect=lambda *a, **k: shown.append(a) or True):
            thread = app_main.start_startup_watchdog(None, seconds=1)
            thread.join(timeout=10)
        self.assertEqual(len(shown), 1)
        self.assertIn("日志", shown[0][0])
        self.assertNotIn("Traceback", shown[0][0])


class MessageOutletTests(unittest.TestCase):
    """弹窗只有一个出口。测试替掉它，用户就不会被模态框卡住。"""

    def test_no_module_pops_a_raw_system_dialog(self):
        offenders = []
        for path in ROOT.rglob("*.py"):
            parts = set(path.parts)
            if parts & {"tests", "__pycache__", "build", "dist"}:
                continue
            if path.resolve() == Path(app_main.__file__).resolve():
                continue
            if "MessageBoxW" in path.read_text(encoding="utf-8", errors="replace"):
                offenders.append(path.name)
        self.assertEqual(offenders, [])

    def test_non_windows_writes_to_stderr_and_says_it_did_not_show(self):
        buffer = io.StringIO()
        with mock.patch.object(app_main.os, "name", "posix"), mock.patch.object(app_main.sys, "stderr", buffer):
            shown = app_main._message_box("测试消息")
        self.assertFalse(shown)
        self.assertIn("测试消息", buffer.getvalue())

    def test_windows_goes_through_one_system_call(self):
        fake = mock.MagicMock()
        with mock.patch.object(app_main.os, "name", "nt"), mock.patch.object(app_main.ctypes, "windll", fake, create=True):
            self.assertTrue(app_main._message_box("消息", flags=0x40, title="DeskPet"))
        fake.user32.MessageBoxW.assert_called_once_with(None, "消息", "DeskPet", 0x40)

    def test_a_failing_dialog_never_raises(self):
        fake = mock.MagicMock()
        fake.user32.MessageBoxW.side_effect = OSError("没有桌面")
        with mock.patch.object(app_main.os, "name", "nt"), mock.patch.object(app_main.ctypes, "windll", fake, create=True):
            self.assertFalse(app_main._message_box("消息"))

    def test_every_notice_uses_the_outlet(self):
        seen = []
        stub = mock.Mock()
        stub.secret_values.return_value = []
        stub.log_dir = Path(_TMP.name) / "logs"
        with mock.patch.object(app_main, "Config", return_value=stub), mock.patch.object(
            app_main, "_message_box", side_effect=lambda message, **k: seen.append(message) or True
        ):
            app_main.notify_already_running()
            app_main.notify_startup_stalled()
            app_main.report_fatal(ValueError("随便什么错"))
        self.assertEqual(len(seen), 3)
        self.assertTrue(all(message.strip() for message in seen))
        self.assertIn("已经在运行", seen[0])
        self.assertIn("启动失败", seen[2])

    def test_a_crash_is_written_down_and_the_user_is_told_where(self):
        log_dir = Path(_TMP.name) / "fatal-logs"
        stub = mock.Mock()
        stub.secret_values.return_value = []
        stub.log_dir = log_dir
        seen = []
        with mock.patch.object(app_main, "Config", return_value=stub), mock.patch.object(
            app_main, "_message_box", side_effect=lambda message, **k: seen.append(message) or True
        ):
            app_main.report_fatal(RuntimeError("模型配置读不出来"))
        files = list(log_dir.glob("*.log"))
        self.assertEqual(len(files), 1)
        text = files[0].read_text(encoding="utf-8")
        self.assertIn("启动失败", text)
        self.assertIn("模型配置读不出来", text)
        self.assertIn(str(files[0]), seen[0])

    def test_a_crash_before_the_config_is_readable_still_leaves_evidence(self):
        fallback = Path(_TMP.name) / "temp-home"
        fallback.mkdir(exist_ok=True)
        with mock.patch.object(app_main, "Config", side_effect=RuntimeError("配置都读不出来")), mock.patch.dict(
            os.environ, {"TEMP": str(fallback)}
        ), mock.patch.object(app_main, "_message_box", return_value=True):
            app_main.report_fatal(ValueError("启动时就炸了"))
        written = fallback / "deskpet-startup-error.log"
        self.assertTrue(written.is_file())
        self.assertIn("启动时就炸了", written.read_text(encoding="utf-8"))

HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const spec = JSON.parse(process.argv[3]);

let rafQueue = [];
const canvasHandlers = {};
const winHandlers = {};
const posted = [];
const writes = {};
const captured = 'data:image/png;base64,VEVTVA==';

const canvas = {
  style: {}, width: 0, height: 0, clientWidth: 420, clientHeight: 640,
  addEventListener: function (name, fn) { (canvasHandlers[name] = canvasHandlers[name] || []).push(fn); },
};
const doc = { getElementById: function () { return canvas; } };
const win = {
  innerWidth: 420, innerHeight: 640, devicePixelRatio: 1, document: doc,
  requestAnimationFrame: function (fn) { rafQueue.push(fn); return rafQueue.length; },
  cancelAnimationFrame: function () { },
  addEventListener: function (name, fn) { (winHandlers[name] = winHandlers[name] || []).push(fn); },
  fetch: function (url, opts) {
    posted.push(JSON.parse(opts.body));
    return { catch: function () { return this; }, then: function () { return this; } };
  },
  Live2DCubismCore: { Version: { csmGetVersion: function () { return 100663297; } } },
  PetRender: {
    version: 'probe',
    create: function () {
      return {
        load: function () { return Promise.resolve({ status: 'ready' }); },
        setView: function () { }, draw: function () { },
        status: function () {
          return { status: 'ready', url: '/model/x', expressions: [], motionGroups: [],
                   drawables: spec.length, parameters: spec.length };
        },
        parameters: function () {
          return spec.map(function (p) { return { id: p.id, min: p.min, max: p.max, default: p.def, value: p.def }; });
        },
        setParameter: function (id, value) { writes[id] = value; },
        mask: function () { return { cols: 16, rows: 16, data: Buffer.alloc(32, 255).toString('base64') }; },
        capture: function () { return captured; },
        unload: function () { },
      };
    },
  },
};

function fire(name, ev) { (canvasHandlers[name] || []).forEach(function (fn) { fn(ev); }); }
function fireWin(name, ev) { (winHandlers[name] || []).forEach(function (fn) { fn(ev); }); }
function mev(x, y, extra) {
  const o = { button: 0, clientX: x, clientY: y, screenX: x, screenY: y,
              preventDefault: function () { }, bubbles: true, cancelable: true };
  Object.keys(extra || {}).forEach(function (k) { o[k] = extra[k]; });
  return o;
}
function span(values) {
  if (!values.length) { return 0; }
  return Math.max.apply(null, values) - Math.min.apply(null, values);
}
function wait(ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); }

const atobFn = (typeof atob === 'function') ? atob
  : function (b64) { return Buffer.from(b64, 'base64').toString('binary'); };
const build = new Function('window', 'document', 'PetRender', 'atob', 'fetch', src + '\n;return window.Pet;');
const Pet = build(win, doc, win.PetRender, atobFn, win.fetch);

let ts = 0;
function pump(frames, step) {
  const size = step || 34;
  for (let i = 0; i < frames; i++) {
    ts += size;
    const queue = rafQueue;
    rafQueue = [];
    for (let n = 0; n < queue.length; n++) { queue[n](ts); }
  }
}
function track(frames, ids, step) {
  const seen = {};
  ids.forEach(function (id) { seen[id] = []; });
  for (let i = 0; i < frames; i++) {
    pump(1, step);
    ids.forEach(function (id) { if (writes[id] !== undefined) { seen[id].push(writes[id]); } });
  }
  return seen;
}

(async function () {
  const out = {};
  const loaded = await Pet.load('/model/x');
  out.ready = !!loaded.ready;
  out.paramCount = Object.keys(Pet.samples()).length;
  out.startable = Pet.start();
  pump(3);

  out.breathSpan = span(track(140, ['ParamBreath']).ParamBreath);

  Pet.setPointer(0.9, 0.2);
  pump(45);
  out.gazeRightX = writes.ParamEyeBallX;
  out.headRightX = writes.ParamAngleX;
  Pet.clearPointer();
  Pet.setPointer(-0.9, -0.6);
  pump(70);
  out.gazeLeftX = writes.ParamEyeBallX;
  out.headLeftX = writes.ParamAngleX;

  const eyes = track(400, ['ParamEyeLOpen', 'ParamEyeROpen']);
  out.eyeLeftMin = Math.min.apply(null, eyes.ParamEyeLOpen);
  out.eyeRightMin = Math.min.apply(null, eyes.ParamEyeROpen);
  out.blinks = Pet.metrics().blinks;

  Pet.setEmotion('neutral');
  pump(90);
  out.cheekNeutral = writes.ParamCheek;
  Pet.setEmotion('happy');
  pump(120);
  out.cheekHappy = writes.ParamCheek;
  Pet.setEmotion('sad');
  pump(120);
  out.mouthSad = writes.ParamMouthForm;
  out.emotionUnknown = Pet.setEmotion('curious');
  Pet.setEmotion('neutral');
  pump(60);

  const beforeCheek = writes.ParamCheek;
  out.react = Pet.react('click');
  await wait(120);
  pump(1);
  out.reactCheek = writes.ParamCheek - beforeCheek;

  // 长按与放下：这两种反应各自要写出参数，并且明显大过同一时间窗的静止漂移，
  // 否则就是写了个没人看得见的死分支。
  Pet.react('');
  pump(30);
  let still = writes.ParamAngleZ;
  await wait(150);
  pump(1);
  out.idleSwing = Math.abs(writes.ParamAngleZ - still);

  Pet.react('');
  pump(30);
  still = writes.ParamAngleZ;
  out.reactLongPress = Pet.react('long_press');
  await wait(150);
  pump(1);
  out.longPressSwing = Math.abs(writes.ParamAngleZ - still);

  Pet.react('');
  pump(30);
  let stillX = writes.ParamAngleX;
  await wait(150);
  pump(1);
  out.idleTilt = Math.abs(writes.ParamAngleX - stillX);

  Pet.react('');
  pump(30);
  stillX = writes.ParamAngleX;
  out.reactDragEnd = Pet.react('drag_end');
  await wait(150);
  pump(1);
  out.dragEndTilt = Math.abs(writes.ParamAngleX - stillX);

  // 鼠标只是靠近：不许摆出大动作，轻轻歪一下就该结束。
  Pet.react('');
  pump(30);
  const hoverStill = writes.ParamAngleZ;
  out.reactHover = Pet.react('hover');
  await wait(200);
  pump(1);
  out.hoverSwing = Math.round(Math.abs(writes.ParamAngleZ - hoverStill) * 1000) / 1000;

  out.expression = Pet.setExpression('smile');
  out.motion = Pet.playMotion('Idle');
  out.capture = Pet.capture();
  out.hitInside = Pet.hit(210, 320);
  out.hitOutside = Pet.hit(-4, -4);

  fire('mousedown', mev(210, 320));
  fireWin('mouseup', mev(210, 320));
  fire('mousedown', mev(210, 320));
  fireWin('mousemove', mev(210, 320, { screenX: 260, screenY: 350 }));
  fireWin('mouseup', mev(210, 320, { screenX: 260, screenY: 350 }));
  fireWin('contextmenu', mev(210, 320, { button: 2 }));
  fire('dblclick', mev(210, 320));
  out.events = posted.map(function (e) { return e.name; });

  posted.length = 0;
  await wait(2600);
  Pet.setPointer(0.95, 0.1);
  pump(50);
  out.maskReady = posted.some(function (e) { return e.name === 'mask_ready'; });

  // 减少动态效果：自动摆动与呼吸要停，眨眼要照旧（不然看着像卡住了）。
  Pet.setMotion(false);
  pump(90);
  out.motionOff = Pet.metrics().motion;
  out.calmBreathSpan = span(track(140, ['ParamBreath']).ParamBreath);
  out.calmSwaySpan = span(track(140, ['ParamAngleZ']).ParamAngleZ);
  out.calmSwaySpanX = span(track(140, ['ParamAngleX']).ParamAngleX);
  out.calmSwaySpanBody = span(track(140, ['ParamBodyAngleX']).ParamBodyAngleX);
  const calmBlinkStart = Pet.metrics().blinks;
  const calmEyes = track(400, ['ParamEyeLOpen']);
  out.calmEyeMin = Math.min.apply(null, calmEyes.ParamEyeLOpen);
  out.calmBlinks = Pet.metrics().blinks - calmBlinkStart;
  Pet.setMotion(true);
  out.motionBackOn = Pet.metrics().motion;
  out.motionOnBreathSpan = span(track(140, ['ParamBreath']).ParamBreath);

  // 光标位置由宿主按固定节奏推进（真实代码算出来的那一串），这里一口气喂进去：
  // 不管它推得多稀，头都要连着走，不能一格一格跳。
  const CURSOR_SAMPLES = __CURSOR_SAMPLES__;
  Pet.clearPointer();
  pump(90);
  const headBefore = writes.ParamAngleX;
  const feedRows = [];
  const feedBase = ts;
  let feedIdx = 0;
  const feedEnd = CURSOR_SAMPLES[CURSOR_SAMPLES.length - 1][0] + feedBase;
  while (ts < feedEnd) {
    while (feedIdx < CURSOR_SAMPLES.length && CURSOR_SAMPLES[feedIdx][0] + feedBase <= ts) {
      Pet.setPointer(CURSOR_SAMPLES[feedIdx][1], CURSOR_SAMPLES[feedIdx][2]);
      feedIdx++;
    }
    pump(1, 33.34);
    feedRows.push(writes.ParamAngleX);
  }
  let feedJump = 0;
  let feedPrev = headBefore;
  for (let i = 0; i < feedRows.length; i++) {
    feedJump = Math.max(feedJump, Math.abs(feedRows[i] - feedPrev));
    feedPrev = feedRows[i];
  }
  out.headFeedFrames = feedRows.length;
  out.headFeedJump = Math.round(feedJump * 1000) / 1000;
  out.headFeedTravel = Math.round(span(feedRows.concat([headBefore])) * 100) / 100;
  out.headFeedPushes = CURSOR_SAMPLES.length;

  // 不动鼠标时的待机摆动：幅度看得见、每一帧都在走、身体跟着转。
  Pet.clearPointer();
  pump(60);
  const swaySeen = track(300, ['ParamAngleX', 'ParamAngleZ', 'ParamBodyAngleX']);
  out.swaySpanX = Math.round(span(swaySeen.ParamAngleX) * 100) / 100;
  out.swaySpanZ = Math.round(span(swaySeen.ParamAngleZ) * 100) / 100;
  out.swayBodySpan = Math.round(span(swaySeen.ParamBodyAngleX) * 100) / 100;
  let swayJump = 0;
  let swayMoving = 0;
  for (let i = 1; i < swaySeen.ParamAngleX.length; i++) {
    const d = Math.abs(swaySeen.ParamAngleX[i] - swaySeen.ParamAngleX[i - 1]);
    swayJump = Math.max(swayJump, d);
    if (d > 0.001) { swayMoving++; }
  }
  out.swayMaxJump = Math.round(swayJump * 1000) / 1000;
  out.swayMovingPct = Math.round(swayMoving * 100 / (swaySeen.ParamAngleX.length - 1));

  // 弹簧不过冲：给一个固定目标，走过站就不对。
  Pet.setMotion(false);
  Pet.clearPointer();
  pump(120);
  Pet.setPointer(0.6, 0);
  const settle = track(120, ['ParamAngleX']).ParamAngleX;
  out.headSettleMax = Math.max.apply(null, settle);
  out.headSettleLast = settle[settle.length - 1];
  out.headSettleOvershoot = Math.round((out.headSettleMax - out.headSettleLast) * 100) / 100;
  Pet.setMotion(true);
  Pet.clearPointer();

  // 页面自己也会收到鼠标事件，但视线只认宿主推来的那把光标：
  // 两套换算不一样，页面再喂一遍，光标一进窗口头就会蹿一下。
  Pet.clearPointer();
  pump(30);
  fireWin('mousemove', mev(2, 2));
  out.pointerAfterOwnMouseMove = Pet.metrics().pointer;

  // 帧率：按显示器的 16.7 毫秒喂 rAF，就该按 60fps 出帧，而不是砍成 30fps。
  const frameBase = Pet.metrics().frames;
  pump(120, 16.7);
  out.displayFrames = Pet.metrics().frames - frameBase;

  out.status = Pet.status();
  out.metrics = Pet.metrics();
  Pet.stop();
  out.stopped = true;
  process.stdout.write(JSON.stringify(out));
})().catch(function (err) {
  process.stdout.write(JSON.stringify({ failed: String((err && err.stack) || err) }));
});
"""

class FakeProc:
    """只为了让宿主以为自己起了个子进程；两个管道立刻 EOF。"""

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


class ChildWatchdogTests(unittest.TestCase):
    """子进程假死、被关掉、进程没退干净，宿主都要自己收场。"""

    def setUp(self):
        self.hosts = []

    def tearDown(self):
        # 让这一轮起的看门狗线程退出，别把线程数留给下一个用例。
        for host in self.hosts:
            host._stopped = True
            host._proc = None
        deadline = time.time() + 5
        while time.time() < deadline and any(
            thread.name == "live2d-watch" for thread in threading.enumerate()
        ):
            time.sleep(0.02)

    def host(self):
        with mock.patch.object(web2d, "_webview_ready", return_value=True):
            host = web2d.WebLive2DHost(runtime=RUNTIME)
        self.hosts.append(host)
        return host

    def model(self):
        return mock.Mock(path=str(RUNTIME / "renderer.html"))

    def test_the_heartbeat_gives_the_child_time_to_answer_the_ping(self):
        self.assertLess(web2d.PING_SECONDS, web2d.HEARTBEAT_SECONDS)
        self.assertLess(web2d.WATCH_INTERVAL, web2d.PING_SECONDS)

    def test_only_one_watcher_thread_is_spawned(self):
        host = self.host()
        release = threading.Event()
        host._watch_loop = lambda: release.wait(3.0)
        # 同一个进程里可能还有别的宿主（别的测试留下的），只看自己这一只多了几个。
        before = [t for t in threading.enumerate() if t.name == "live2d-watch"]
        first = host._start_watcher()
        second = host._start_watcher()
        self.assertIs(first, second)
        alive = [t for t in threading.enumerate() if t.name == "live2d-watch"]
        self.assertEqual(len(alive), len(before) + 1)
        release.set()

    def test_a_silent_child_is_pinged_and_then_restarted(self):
        host = self.host()
        host._model_path = "live2D.model3.json"
        host._proc = mock.Mock()
        host._proc.poll.return_value = None
        host._last_beat = time.time() - 100
        sent = []
        calls = []

        class StopLoop(Exception):
            pass

        def recover(reason, model=None):
            calls.append((reason, model))
            raise StopLoop()

        with mock.patch.object(web2d, "WATCH_INTERVAL", 0.01), mock.patch.object(
            host, "_send", side_effect=lambda message: sent.append(message)
        ), mock.patch.object(host, "_recover", side_effect=recover):
            with self.assertRaises(StopLoop):
                host._watch_loop()
        self.assertEqual(sent, [{"cmd": "status"}])
        self.assertEqual(calls, [("渲染子进程没有回应", "live2D.model3.json")])

    def test_a_child_that_dies_is_started_again(self):
        host = self.host()
        notes = []
        host.set_sink(lambda name, payload: notes.append(name))
        host._model_path = "live2D.model3.json"
        host._proc = FakeProc()
        with mock.patch.object(host, "_recover") as recover:
            host._read_loop(host._proc)
            deadline = time.time() + 2
            while time.time() < deadline and not recover.called:
                time.sleep(0.02)
        self.assertIn("live2d_exit", notes)
        self.assertTrue(recover.called)
        self.assertEqual(recover.call_args.args[1], "live2D.model3.json")

    def test_a_deliberate_stop_does_not_start_anything_again(self):
        host = self.host()
        host._model_path = "live2D.model3.json"
        host._proc = FakeProc()
        with mock.patch.object(host, "_recover") as recover:
            host._stopped = True
            host._read_loop(host._proc)
        recover.assert_not_called()

    def test_it_gives_up_after_the_restart_cap_and_says_what_it_did(self):
        host = self.host()
        notes = []
        host.set_sink(lambda name, payload: notes.append(name))
        with mock.patch.object(host, "stop"), mock.patch.object(host, "start", return_value={"ok": True}):
            results = [host._recover("测试", "live2D.model3.json") for _ in range(web2d.MAX_RESTARTS + 1)]
        self.assertEqual(results.count(True), web2d.MAX_RESTARTS)
        self.assertFalse(results[-1])
        self.assertEqual(notes.count("live2d_restarting"), web2d.MAX_RESTARTS)
        self.assertIn("live2d_error", notes)
        self.assertIn("透明拖动区域", host._error)

    def test_a_restart_window_forgets_old_failures(self):
        host = self.host()
        host._restarts = [time.time() - web2d.RESTART_WINDOW - 10] * web2d.MAX_RESTARTS
        with mock.patch.object(host, "stop"), mock.patch.object(host, "start", return_value={"ok": True}):
            self.assertTrue(host._recover("测试", "live2D.model3.json"))
        self.assertEqual(host.restart_count(), 1)

    def test_the_child_gets_unbuffered_pipes(self):
        host = self.host()
        with mock.patch.object(web2d.subprocess, "Popen", return_value=FakeProc()) as popen, mock.patch.object(
            host, "_start_watcher"
        ), mock.patch.object(host, "_recover"):
            self.assertTrue(host.start(self.model())["ok"])
        env = popen.call_args.kwargs["env"]
        self.assertEqual(env["PYTHONUNBUFFERED"], "1")
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
        host.stop(timeout=0.1)

    def test_start_clears_the_stopped_flag_so_a_later_exit_is_still_reported(self):
        host = self.host()
        notes = []
        host.set_sink(lambda name, payload: notes.append(name))
        host._stopped = True
        with mock.patch.object(web2d.subprocess, "Popen", return_value=FakeProc()), mock.patch.object(
            host, "_start_watcher"
        ), mock.patch.object(host, "_recover"):
            self.assertTrue(host.start(self.model())["ok"])
            self.assertFalse(host._stopped)
            deadline = time.time() + 5
            while time.time() < deadline and "live2d_exit" not in notes:
                time.sleep(0.02)
        self.assertIn("live2d_exit", notes)


class RuntimeFileTests(unittest.TestCase):
    """页面端的关键东西必须在，页面不在浏览器里也能查。"""

    def test_the_page_controller_is_a_real_idle_loop(self):
        text = PAGE.read_text(encoding="utf-8")
        for marker in (
            "requestAnimationFrame", "ParamBreath", "ParamEyeLOpen", "ParamEyeROpen",
            "setPointer", "samples", "capture", "mask_ready", "drag_begin",
            "dblclick", "contextmenu", "clamp",
        ):
            self.assertIn(marker, text)

    def test_the_renderer_can_read_back_the_pixels(self):
        text = RENDER.read_text(encoding="utf-8")
        self.assertIn("readPixels", text)
        self.assertIn("preserveDrawingBuffer", text)
        self.assertIn("capture:", text)

    def test_the_harness_parameter_list_matches_the_draft(self):
        if not DRAFT_CDI.is_file():
            self.skipTest("本机没有放草稿")
        ids = {item["Id"] for item in json.loads(DRAFT_CDI.read_text(encoding="utf-8"))["Parameters"]}
        self.assertEqual(ids, {item["id"] for item in PARAMS})


class PackagingTests(unittest.TestCase):
    """打包只带程序，不带用户数据，也不漏资源。"""

    def test_the_spec_ships_the_page_controller_and_the_skill_library(self):
        text = SPEC.read_text(encoding="utf-8")
        self.assertIn("SKILL_LIBRARY", text)
        self.assertIn("sorted(SKILL_LIBRARY.rglob", text)
        self.assertIn("__pycache__", text)
        self.assertIn("resources/live2d", text)

    def test_the_spec_never_adds_a_database(self):
        self.assertNotIn("sqlite3", SPEC.read_text(encoding="utf-8"))

    def test_the_packaged_page_controller_matches_the_source(self):
        packed = ROOT / "dist" / "Blossom" / "_internal" / "resources" / "live2d" / "pet_page.js"
        if not packed.is_file():
            self.skipTest("还没有打包产物")
        self.assertEqual(packed.read_bytes(), PAGE.read_bytes())


if __name__ == "__main__":
    unittest.main()
