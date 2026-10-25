"""阶段 P43 验收：Live2D 草稿接入与显示验证。

覆盖：资源导入与依赖校验（缺文件如实列出，绝不假装加载成功）、渲染宿主接口契约
与降级、绘制顺序按 Cubism 官方语义排序（本阶段修掉的「右眼不显示」根因）、运行
文件不执行外部脚本也不带密钥、配置项取值、角色缩放设置持久化、没有模型或没有
渲染环境时照常启动。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from avatar import web2d
from avatar.interface import AvatarProvider
from avatar.live2d import (
    NO_HOST,
    NO_MODEL,
    Live2DHost,
    Live2DModel,
    Live2DProvider,
    NullLive2DHost,
    build_provider,
    find_models,
    pick_host,
    referenced_files,
)
from security.config import Config
from ui.desktop import DesktopPet

RUNTIME = ROOT / "resources" / "live2d"
DRAFT_DIR = ROOT / "live2d"
DRAFT = DRAFT_DIR / "live2D" / "live2D.model3.json"
# 打包后由 EXE 自己当渲染器入口时用的命令行开关
RENDERER_FLAG = "--live2d-renderer"

# 用户那份草稿里 Core 报的绘制槽位。它就是这一版修掉「右眼不显示」的那份数据：
# 眼睛的底色图层（下标 26 / 27）槽位最小，要排在眼珠图层（下标 5 / 7）之前。
DRAFT_RENDER_ORDERS = [
    0, 1, 2, 3, 4, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17,
    18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 6, 5,
]
WANTED_SEQUENCE = [
    0, 1, 2, 3, 4, 27, 26, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14,
    15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25,
]

HARNESS = """
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const sandbox = {};
new Function('window', src)(sandbox);
const P = sandbox.PetRender;
const raw = JSON.parse(process.argv[3]);
process.stdout.write(JSON.stringify({
  version: P.version,
  sorted: P.sortDrawOrder(raw, raw.length),
  toy: P.sortDrawOrder([2, 0, 1], 3),
  offscreen: P.sortDrawOrder([1, 0, 2, 3], 2),
  one: P.sortDrawOrder([0], 1),
  none: P.sortDrawOrder([], 0),
  masks_nested: [P.masksOf([[7], [], [3, 4]], 0, 0, 1), P.masksOf([[7], [], [3, 4]], 1, 1, 1), P.masksOf([[7], [], [3, 4]], 2, 2, 4)],
  masks_flat: [P.masksOf([7, 3, 4], 0, 0, 1), P.masksOf([7, 3, 4], 1, 1, 3), P.masksOf(null, 0, 0, 0)],
}));
"""

_TMP = None


def setUpModule():
    global _TMP
    _TMP = tempfile.TemporaryDirectory(prefix="deskpet-p43-")


def tearDownModule():
    if _TMP is not None:
        _TMP.cleanup()


def node_exe():
    return shutil.which("node")


def renderer_sort(raw):
    """在 node 里跑一遍真实渲染器的排序函数，返回它给出的绘制顺序。"""
    harness = Path(_TMP.name) / "harness.js"
    if not harness.is_file():
        harness.write_text(HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [node_exe(), str(harness), str(RUNTIME / "pet_render.js"), json.dumps(list(raw))],
        capture_output=True,
        text=True,
        timeout=90,
    )
    if proc.returncode != 0:
        raise AssertionError("node 跑不动渲染器：%s" % (proc.stderr or "")[-400:])
    return json.loads(proc.stdout)


def write_model(folder, name, refs=None, extra=None, files=()):
    """造一个 model3.json（可指名要造的依赖文件），返回入口文件路径。"""
    base = Path(folder)
    base.mkdir(parents=True, exist_ok=True)
    data = {"Version": 3, "FileReferences": dict(refs or {})}
    if extra:
        data.update(extra)
    for item in files:
        target = base / item
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
    path = base / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


class RuntimeAssetTests(unittest.TestCase):
    """随程序分发的渲染资源必须是官方那份，而且只跑本地东西。"""

    def test_runtime_dir_ships_every_needed_file(self):
        base = Path(web2d.runtime_dir())
        self.assertEqual(base.name, "live2d")
        for name in web2d.RUNTIME_FILES:
            self.assertTrue((base / name).is_file(), name)

    def test_core_is_the_official_redistributable_build(self):
        text = (RUNTIME / "live2dcubismcore.min.js").read_text(encoding="utf-8", errors="replace")
        self.assertIn("Live2D Cubism Core", text)
        self.assertIn("Redistributable Code", text)
        self.assertIn("csmGetRenderOrders", text)

    def test_our_runtime_files_stay_local_and_passive(self):
        for name in ("pet_render.js", "pet_page.js", "renderer.html"):
            text = (RUNTIME / name).read_text(encoding="utf-8")
            for bad in ("http://", "https://", "eval(", "new Function", "document.write"):
                self.assertNotIn(bad, text, "%s 里有 %s" % (name, bad))

    def test_renderer_sorts_by_official_semantics(self):
        text = (RUNTIME / "pet_render.js").read_text(encoding="utf-8")
        self.assertIn("sortDrawOrder", text)
        self.assertRegex(text, r"sortDrawOrder\(state\.model\.getRenderOrders\(\)")
        # 修之前的那种「把顺序数组直接当绘制序列」不能再回来
        self.assertNotIn("Array.prototype.slice.call(state.model.getRenderOrders())", text)

    def test_no_secret_leaks_into_the_runtime_folder(self):
        for name in ("pet_render.js", "pet_page.js", "renderer.html", "README.txt"):
            text = (RUNTIME / name).read_text(encoding="utf-8", errors="replace")
            self.assertNotIn("sk-", text, name)
            self.assertNotIn("api_key", text.lower(), name)
        core = (RUNTIME / "live2dcubismcore.min.js").read_text(encoding="utf-8", errors="replace")
        self.assertNotIn("api_key", core.lower())


@unittest.skipUnless(node_exe(), "这台机器没有 node，跳过真实排序计算")
class DrawOrderTests(unittest.TestCase):
    """绘制顺序：按 Cubism 官方语义排序，这是「右眼不显示」的根因。"""

    def test_renderer_is_loading(self):
        data = renderer_sort([0])
        self.assertEqual(data["version"], "deskpet-live2d-1")

    def test_slots_are_sorted_not_followed(self):
        data = renderer_sort(DRAFT_RENDER_ORDERS)
        self.assertEqual(data["sorted"], WANTED_SEQUENCE)
        self.assertNotEqual(data["sorted"], DRAFT_RENDER_ORDERS)

    def test_small_inputs(self):
        data = renderer_sort([0])
        self.assertEqual(data["toy"], [1, 2, 0])
        self.assertEqual(data["one"], [0])
        self.assertEqual(data["none"], [])
        # 数组长度含 offscreen，这里只画 drawable
        self.assertEqual(data["offscreen"], [1, 0])

    def test_eye_underlay_draws_before_the_eye_itself(self):
        order = renderer_sort(DRAFT_RENDER_ORDERS)["sorted"]
        pos = {value: index for index, value in enumerate(order)}
        self.assertLess(pos[27], pos[5], "左眼底色必须在眼珠之前")
        self.assertLess(pos[26], pos[7], "右眼底色必须在眼珠之前")

    def test_masks_of_a_drawable_are_read_from_its_own_slot(self):
        """一个元素可能只用别的元素当裁剪形状，核心数据是"每个元素一串"。"""
        data = renderer_sort([0])
        self.assertEqual(data["masks_nested"], [[7], [], [3, 4]])

    def test_a_flat_mask_table_still_works(self):
        """有些 Core 版本给的是平铺的一串，这时候要按元素自己的范围去取。"""
        data = renderer_sort([0])
        self.assertEqual(data["masks_flat"], [[7], [3, 4], None])

    def test_the_renderer_honours_clipping_masks(self):
        """模型里的裁剪形状以前被整个忽略，眼睛和嘴巴就和 Cubism 里看到的不一样。"""
        src = (RUNTIME / "pet_render.js").read_text(encoding="utf-8")
        self.assertIn("stencil: true", src, "WebGL 上下文要先要到模板缓冲")
        self.assertIn("maskIndices(maskTable", src, "画之前要看这个元素自己的裁剪形状")
        self.assertIn("gl.enable(gl.STENCIL_TEST)", src)
        self.assertIn("gl.disable(gl.STENCIL_TEST)", src)
        self.assertIn("if (u_clip > 0.5 && c.a * u_opacity <= 0.01) discard;", src, "画裁剪形状时透明像素不能进模板")

    def test_following_the_raw_array_is_what_broke_the_right_eye(self):
        """把槽位数组当绘制序列用，右眼就会被自己的底色盖住。"""
        naive = {value: index for index, value in enumerate(DRAFT_RENDER_ORDERS)}
        proper = {value: index for index, value in enumerate(WANTED_SEQUENCE)}
        self.assertGreater(naive[26], naive[7])
        self.assertLess(proper[26], proper[7])


@unittest.skipUnless(DRAFT.is_file(), "草稿不在项目里，跳过")
class DraftModelTests(unittest.TestCase):
    """用户的实际草稿必须被认出来，依赖必须齐全。"""

    def test_draft_is_found(self):
        found = find_models(str(DRAFT_DIR))
        ids = [model.id for model in found]
        # 这个目录里放着用户自己的两个模型：早先的草稿，和后来重新导出的 live2D0。
        self.assertIn("live2D", ids, "用户自己的草稿必须被认出来")
        self.assertIn("live2D0", ids)
        self.assertEqual(Path(found[ids.index("live2D")].path), DRAFT)

    def test_draft_declares_only_what_it_has(self):
        model = find_models(str(DRAFT_DIR))[0]
        self.assertEqual(referenced_files(model.refs), ("live2D.moc3", "live2D.1024/texture_00.png"))
        self.assertEqual(model.missing_files(), ())
        self.assertTrue(model.complete())
        self.assertEqual(len(model.textures), 1)

    def test_draft_has_no_motions_or_expressions_yet(self):
        model = find_models(str(DRAFT_DIR))[0]
        self.assertEqual(model.expressions, ())
        self.assertEqual(model.motions, {})
        self.assertEqual(model.physics, "")
        self.assertEqual(model.pose, "")
        self.assertEqual(model.describe()["missing"], [])


class MissingFileTests(unittest.TestCase):
    """缺文件就说缺什么，不假装能加载。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_missing_moc_is_listed(self):
        path = write_model(self.root / "a", "a.model3.json",
                           {"Moc": "a.moc3", "Textures": ["t/texture_00.png"]})
        model = Live2DModel.from_file(path)
        self.assertEqual(model.missing_files(), ("a.moc3", "t/texture_00.png"))
        self.assertFalse(model.complete())
        self.assertIn("a.moc3", model.describe()["missing"])

    def test_complete_model_reports_nothing(self):
        path = write_model(self.root / "b", "b.model3.json",
                           {"Moc": "b.moc3", "Textures": ["t/texture_00.png"]},
                           files=["b.moc3", "t/texture_00.png"])
        model = Live2DModel.from_file(path)
        self.assertEqual(model.missing_files(), ())
        self.assertTrue(model.complete())

    def test_lowercase_keys_and_side_files_are_checked(self):
        path = write_model(
            self.root / "c",
            "c.model.json",
            {
                "moc": "c.moc3",
                "textures": ["tex/texture_00.png"],
                "physics": "c.physics3.json",
                "pose": "c.pose3.json",
                "expressions": [{"Name": "smile", "File": "exp/smile.exp.json"}, "exp/plain.exp.json"],
                "motions": {"Idle": [{"File": "m/idle.motion3.json"}, "m/plain.motion3.json"]},
            },
        )
        model = Live2DModel.from_file(path)
        self.assertEqual(
            model.missing_files(),
            ("c.moc3", "tex/texture_00.png", "exp/smile.exp.json", "exp/plain.exp.json",
             "m/idle.motion3.json", "m/plain.motion3.json", "c.physics3.json",
             "c.pose3.json"),
        )

    def test_wrong_relative_path_counts_as_missing(self):
        path = write_model(self.root / "d", "d.model3.json", {"Moc": "sub/../nope.moc3"})
        model = Live2DModel.from_file(path)
        self.assertEqual(model.missing_files(), ("sub/../nope.moc3",))

    def test_broken_entry_file_is_skipped_not_faked(self):
        base = self.root / "e"
        base.mkdir()
        (base / "e.model3.json").write_text("{ 不是 json", encoding="utf-8")
        self.assertEqual(find_models(str(self.root)), [])

    def test_model_without_file_references_is_still_a_model(self):
        path = write_model(self.root / "f", "f.model3.json")
        model = Live2DModel.from_file(path)
        self.assertTrue(model.complete())
        self.assertEqual(referenced_files({}), ())


class ListLogger:
    def __init__(self):
        self.lines = []

    def warning(self, *args):
        self.lines.append(" ".join(str(item) for item in args))

    def info(self, *args):
        self.lines.append(" ".join(str(item) for item in args))

    def error(self, *args):
        self.lines.append(" ".join(str(item) for item in args))

    def debug(self, *args):
        pass

    def text(self):
        return " ".join(self.lines)


class NullAvatar(AvatarProvider):
    name = "placeholder"

    def __init__(self):
        self.state = {}

    def set_expression(self, expression):
        return expression

    def play_animation(self, animation):
        return animation

    def set_state(self, state):
        self.state = dict(state or {})
        return self.state


class FakeHost(Live2DHost):
    """记下被调用了什么，方便断言"程序真的下发了"。"""

    name = "fake"

    def __init__(self, available=True, ok=True, boom=False):
        self.available = available
        self.ok = ok
        self.boom = boom
        self.calls = []

    def _reply(self, **kwargs):
        if self.boom:
            raise RuntimeError("渲染器炸了")
        data = {"ok": self.ok, "host": self.name}
        if not self.ok:
            data["error"] = "模型装不上"
        data.update(kwargs)
        return data

    def is_available(self):
        return self.available

    def load(self, model):
        self.calls.append(("load", getattr(model, "id", None)))
        return self._reply()

    def set_expression(self, expression):
        self.calls.append(("set_expression", expression))
        return self._reply(expression=expression)

    def play_motion(self, group, index=0):
        self.calls.append(("play_motion", group, index))
        return self._reply(group=group)

    def set_emotion(self, emotion):
        self.calls.append(("set_emotion", emotion))
        return self._reply(emotion=emotion)

    def unload_model(self):
        self.calls.append(("unload_model",))
        return self._reply()

    def snapshot(self):
        return {"host": self.name, "available": self.available, "calls": list(self.calls)}


class ProviderTests(unittest.TestCase):
    """模型到没到、渲染器到没到，都要如实反映。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = write_model(
            self.root / "g",
            "g.model3.json",
            {
                "Moc": "g.moc3",
                "Textures": ["t/texture_00.png"],
                "Expressions": [{"Name": "smile", "File": "exp/smile.exp.json"}],
                "Motions": {"Idle": [{"File": "m/idle.motion3.json"}],
                            "TapBody": [{"File": "m/tap.motion3.json"}]},
            },
            files=["g.moc3", "t/texture_00.png", "exp/smile.exp.json", "m/idle.motion3.json",
                   "m/tap.motion3.json"],
        )
        self.model = Live2DModel.from_file(self.path)

    def test_without_model_nothing_is_rendered(self):
        provider = Live2DProvider(host=FakeHost())
        provider.set_state({"emotion": "happy"})
        self.assertFalse(provider.rendering())
        self.assertIn(NO_MODEL, provider.reason)
        self.assertEqual(provider.snapshot()["model"], None)

    def test_without_host_the_model_is_not_pretended_to_render(self):
        provider = Live2DProvider(model=self.model)
        result = provider.load()
        self.assertFalse(result["ok"])
        self.assertIn(NO_HOST, result["error"])
        self.assertFalse(provider.rendering())

    def test_with_model_and_host_it_reports_rendering(self):
        host = FakeHost()
        provider = Live2DProvider(model=self.model, host=host)
        self.assertTrue(provider.load()["ok"])
        self.assertTrue(provider.rendering())
        self.assertEqual(host.calls[0], ("load", "g"))

    def test_emotion_maps_to_declared_expression_and_motion(self):
        host = FakeHost()
        provider = Live2DProvider(model=self.model, host=host)
        provider.load()
        provider.set_state({"emotion": "happy"})
        self.assertIn(("set_expression", "smile"), host.calls)
        self.assertIn(("play_motion", "TapBody", 0), host.calls)
        self.assertEqual(provider.expressions_sent, 1)
        self.assertEqual(provider.motions_sent, 1)

    def test_unknown_expression_is_counted_not_invented(self):
        host = FakeHost()
        provider = Live2DProvider(model=self.model, host=host)
        provider.load()
        provider.set_expression("grumpy")
        self.assertFalse(any(call[0] == "set_expression" for call in host.calls))
        self.assertEqual(provider.missed, 1)
        self.assertIn("grumpy", str(provider.last_miss))

    def test_a_model_without_resources_degrades_quietly(self):
        plain = Live2DModel.from_file(
            write_model(self.root / "h", "h.model3.json", {"Moc": "h.moc3"}, files=["h.moc3"])
        )
        host = FakeHost()
        provider = Live2DProvider(model=plain, host=host)
        provider.load()
        provider.set_state({"emotion": "happy"})
        self.assertTrue(provider.rendering())
        self.assertEqual(provider.expressions_sent, 0)
        self.assertEqual(provider.motions_sent, 0)
        self.assertEqual(provider.missed, 2)

    def test_unload_releases_and_stops_rendering(self):
        host = FakeHost()
        provider = Live2DProvider(model=self.model, host=host)
        provider.load()
        self.assertTrue(provider.unload_model()["ok"])
        self.assertIn(("unload_model",), host.calls)
        self.assertFalse(provider.rendering())

    def test_host_failure_is_reported_never_faked(self):
        logger = ListLogger()
        provider = Live2DProvider(model=self.model, host=FakeHost(boom=True), logger=logger)
        self.assertFalse(provider.load()["ok"])
        self.assertIn("炸了", provider.reason)
        self.assertIn("炸了", logger.text())

    def test_placeholder_still_gets_the_state(self):
        fallback = NullAvatar()
        provider = Live2DProvider(model=self.model, host=FakeHost(), fallback=fallback)
        provider.load()
        provider.set_state({"emotion": "sad"})
        self.assertEqual(fallback.state, {"emotion": "sad"})

    def test_window_controls_the_host_lacks_are_reported(self):
        host = FakeHost()
        provider = Live2DProvider(model=self.model, host=host)
        provider.load()
        for result in (provider.set_visible(False), provider.set_scale(1.2)):
            self.assertFalse(result["ok"])
            self.assertIn(NO_HOST, result["error"])
        self.assertEqual(provider.host_status()["host"], "fake")

    def test_probe_failure_is_not_a_crash(self):
        class Bad(FakeHost):
            def is_available(self):
                raise RuntimeError("探测不了")

        logger = ListLogger()
        provider = Live2DProvider(model=self.model, host=Bad(), logger=logger)
        self.assertFalse(provider.host_available())
        self.assertFalse(provider.rendering())
        self.assertIn("live2d host probe failed", logger.text())


class HostTests(unittest.TestCase):
    """渲染宿主的接口契约：缺东西就说缺，调不动就报错，不崩。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_host_interface_is_abstract(self):
        with self.assertRaises(TypeError):
            Live2DHost()

    def test_web_host_keeps_the_whole_contract(self):
        for name in ("is_available", "load", "set_expression", "play_motion", "snapshot",
                     "load_model", "unload_model", "set_visible", "set_scale", "set_emotion",
                     "get_status", "describe", "missing_files", "runtime_files",
                     "refresh_status", "attach_window", "set_sink", "sync_geometry",
                     "unavailable_reason", "stop", "running"):
            self.assertTrue(callable(getattr(web2d.WebLive2DHost, name, None)), name)
        self.assertTrue(issubclass(web2d.WebLive2DHost, Live2DHost))

    def test_missing_runtime_files_are_listed(self):
        host = web2d.WebLive2DHost(runtime=self.root)
        self.assertFalse(host.is_available())
        self.assertEqual(len(host.missing_files()), len(web2d.RUNTIME_FILES))
        self.assertEqual(host.unavailable_reason(), web2d.NO_RUNTIME)

    def test_host_without_webview_is_unavailable(self):
        with mock.patch.object(web2d, "_webview_ready", return_value=False):
            host = web2d.WebLive2DHost(runtime=RUNTIME)
            self.assertFalse(host.is_available())
            self.assertTrue(host.unavailable_reason())

    def test_status_is_honest_before_anything_starts(self):
        with mock.patch.object(web2d, "_webview_ready", return_value=True):
            host = web2d.WebLive2DHost(runtime=RUNTIME)
            self.assertTrue(host.is_available())
            status = host.get_status()
            self.assertFalse(status["running"])
            self.assertEqual(status["state"], "idle")
            self.assertFalse(host.snapshot()["rendering"])
            self.assertEqual(host.describe()["state"], "idle")
            result = host.load_model()
            self.assertFalse(result["ok"])
            self.assertIn(NO_MODEL, result["error"])

    def test_calls_before_ready_are_refused_not_faked(self):
        with mock.patch.object(web2d, "_webview_ready", return_value=True):
            host = web2d.WebLive2DHost(runtime=RUNTIME)
            for result in (host.set_expression("smile"), host.play_motion("Idle"),
                           host.set_emotion("happy")):
                self.assertFalse(result["ok"])

    def test_scale_is_bounded(self):
        with mock.patch.object(web2d, "_webview_ready", return_value=True):
            host = web2d.WebLive2DHost(runtime=RUNTIME)
            self.assertEqual(host.set_scale(9), 4.0)
            self.assertEqual(host.set_scale(0.01), 0.2)
            self.assertEqual(host.set_scale("nonsense"), 0.2)
            self.assertEqual(host.scale(), 0.2)

    def test_stop_is_safe_when_nothing_runs(self):
        host = web2d.WebLive2DHost(runtime=self.root)
        self.assertFalse(host.running())
        host.stop()
        host.stop(timeout=0.1)
        self.assertFalse(host.running())
        self.assertTrue(host.unload_model()["ok"])

    def test_sink_errors_do_not_escape(self):
        host = web2d.WebLive2DHost(runtime=RUNTIME)

        def boom(name, payload):
            raise RuntimeError("界面挂了")

        host.set_sink(boom)
        host._notify("live2d_ready", {})
        self.assertTrue(True)


class DegradeTests(unittest.TestCase):
    """没有模型、关掉渲染、运行文件缺失，都要退到占位形象而不是崩。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def config(self, **kwargs):
        config = Config(load_env=False)
        config.live2d_dir = str(self.root / "live2d")
        for key, value in kwargs.items():
            setattr(config, key, value)
        return config

    def test_no_model_means_no_render_probe(self):
        provider = build_provider(self.config())
        self.assertIsNone(provider.model)
        self.assertIsInstance(provider.host, NullLive2DHost)
        self.assertFalse(provider.rendering())

    def test_render_can_be_switched_off(self):
        self.assertIsInstance(pick_host(self.config(live2d_render=False)), NullLive2DHost)

    def test_unusable_runtime_falls_back(self):
        config = self.config(live2d_runtime_dir=self.root / "nope")
        with mock.patch.object(web2d, "_webview_ready", return_value=True):
            self.assertIsInstance(pick_host(config), NullLive2DHost)

    def test_provider_still_works_without_any_renderer(self):
        write_model(self.root / "live2d" / "i", "i.model3.json", {"Moc": "i.moc3"}, files=["i.moc3"])
        provider = build_provider(self.config(live2d_render=False))
        self.assertIsNotNone(provider.model)
        self.assertIsInstance(provider.host, NullLive2DHost)
        provider.set_state({"emotion": "happy"})
        self.assertFalse(provider.rendering())
        self.assertIn(NO_HOST, provider.reason)

    def test_build_host_returns_none_when_unavailable(self):
        self.assertIsNone(web2d.build_host(self.config(live2d_runtime_dir=self.root / "nope")))


class ConfigTests(unittest.TestCase):
    """新增的配置项要有边界，也要进脱敏视图。"""

    def env(self, **kwargs):
        keys = {
            "DESKPET_LIVE2D_RENDER": "",
            "DESKPET_LIVE2D_SCALE": "",
            "DESKPET_LIVE2D_HEIGHT": "",
            "DESKPET_LIVE2D_RUNTIME": "",
            "DESKPET_LIVE2D_MODEL": "",
        }
        keys.update(kwargs)
        return mock.patch.dict(os.environ, keys, clear=False)

    def test_scale_is_bounded(self):
        with self.env(DESKPET_LIVE2D_SCALE="9"):
            self.assertEqual(Config(load_env=False).live2d_scale, 4.0)
        with self.env(DESKPET_LIVE2D_SCALE="0.01"):
            self.assertEqual(Config(load_env=False).live2d_scale, 0.2)
        with self.env(DESKPET_LIVE2D_SCALE="abc"):
            self.assertEqual(Config(load_env=False).live2d_scale, 1.0)

    def test_height_is_bounded(self):
        with self.env(DESKPET_LIVE2D_HEIGHT="5000"):
            self.assertEqual(Config(load_env=False).live2d_height, 1200)
        with self.env(DESKPET_LIVE2D_HEIGHT="10"):
            self.assertEqual(Config(load_env=False).live2d_height, 80)

    def test_render_switch_and_runtime_override(self):
        with self.env(DESKPET_LIVE2D_RENDER="0"):
            self.assertFalse(Config(load_env=False).live2d_render)
        with self.env(DESKPET_LIVE2D_RENDER="1"):
            self.assertTrue(Config(load_env=False).live2d_render)
        with self.env(DESKPET_LIVE2D_RUNTIME=str(RUNTIME)):
            self.assertEqual(Config(load_env=False).live2d_runtime_dir, RUNTIME.resolve())

    def test_model_dir_defaults_under_the_data_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"DESKPET_ROOT": tmp}):
                config = Config(load_env=False)
                self.assertTrue(str(config.live2d_dir).endswith("live2d"))
                self.assertTrue(config.live2d_dir.is_dir())

    def test_redacted_view_lists_the_new_keys(self):
        with self.env():
            view = Config(load_env=False).redacted()
        for key in ("live2d_dir", "live2d_model", "live2d_render", "live2d_scale"):
            self.assertIn(key, view)

    def test_preferences_are_kept_out_of_the_model_context(self):
        from memory import manager as memory_manager

        self.assertFalse(memory_manager._visible_preference("live2d.scale"))
        self.assertFalse(memory_manager._visible_preference("live2d.visible"))


class ScalePreferenceTests(unittest.TestCase):
    """角色缩放设置要真的存下来、读回来（存的名字和读的名字必须是同一个）。"""

    class Stub:
        live2d_scale = DesktopPet.live2d_scale
        _remember = DesktopPet._remember
        _saved_scale = DesktopPet._saved_scale

        def __init__(self, store=None):
            self.store = dict(store or {})
            self.live2d = None
            self._live2d_scale_value = 1.0
            self.hints = []
            self.warnings = []

        def _preference(self, key):
            return self.store.get(key)

        def _save_preference(self, key, value):
            self.store[key] = str(value)
            return True

        def _hint(self, text):
            self.hints.append(text)

        def _warn(self, message, *args):
            self.warnings.append(message % args if args else message)

    def test_scale_is_written_under_the_key_it_is_read_from(self):
        pet = self.Stub()
        value = pet.live2d_scale(0.1)
        self.assertAlmostEqual(value, 1.1, places=6)
        self.assertEqual(pet.store, {"live2d.scale": "1.100"})

    def test_saved_scale_comes_back_after_a_restart(self):
        pet = self.Stub()
        pet.live2d_scale(0.2)
        fresh = self.Stub(pet.store)
        self.assertAlmostEqual(fresh._saved_scale(1.0), 1.2, places=6)

    def test_scale_is_clamped_both_ways(self):
        pet = self.Stub()
        pet._live2d_scale_value = 3.0
        self.assertEqual(pet.live2d_scale(1.0), 3.0)
        pet._live2d_scale_value = 0.4
        self.assertEqual(pet.live2d_scale(-1.0), 0.4)

    def test_broken_saved_value_falls_back(self):
        pet = self.Stub({"live2d.scale": "坏了"})
        self.assertEqual(pet._saved_scale(1.0), 1.0)
        pet = self.Stub()
        self.assertEqual(pet._saved_scale(0.7), 0.7)

    def test_remember_keeps_the_live2d_namespace(self):
        pet = self.Stub()
        pet._remember("scale", "1.5")
        self.assertEqual(pet.store, {"live2d.scale": "1.5"})


class RendererProcessTests(unittest.TestCase):
    """渲染子进程那支笔：只读本地文件，不执行模型包里的东西。"""

    def setUp(self):
        self.source = (ROOT / "avatar" / "web2d_renderer.py").read_text(encoding="utf-8")

    def test_renderer_module_imports_cleanly(self):
        from avatar import web2d_renderer

        self.assertTrue(callable(web2d_renderer.main))
        self.assertEqual(web2d_renderer.WINDOW_TITLE, "Blossom Live2D")

    def test_no_shell_or_dynamic_code(self):
        for bad in ("os.system", "eval(", "exec(", "importlib", "pickle.load", "shell=True"):
            self.assertNotIn(bad, self.source, bad)

    def test_window_sides_are_bounded(self):
        from avatar import web2d_renderer

        self.assertGreaterEqual(web2d_renderer.MIN_SIDE, 1)
        self.assertLess(web2d_renderer.MAX_SIDE, 100000)

    def test_renderer_entry_is_wired_in_the_launcher(self):
        text = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn(RENDERER_FLAG, text)
        self.assertIn("web2d_renderer", text)

    def test_agent_core_still_knows_nothing_about_live2d(self):
        text = (ROOT / "agent" / "core.py").read_text(encoding="utf-8").lower()
        for word in ("live2d", "cubism", "avatarprovider", "webview", "tkinter", "desktop"):
            self.assertNotIn(word, text, word)


class SideEffectTests(unittest.TestCase):
    """接渲染不能让程序乱写用户的库或日志。"""

    def test_only_reading_the_draft_leaves_it_untouched(self):
        if not DRAFT.is_file():
            self.skipTest("草稿不在项目里")
        before = {item: item.stat().st_mtime_ns for item in DRAFT.parent.rglob("*") if item.is_file()}
        find_models(str(DRAFT_DIR))
        Live2DModel.from_file(DRAFT).missing_files()
        after = {item: item.stat().st_mtime_ns for item in DRAFT.parent.rglob("*") if item.is_file()}
        self.assertEqual(before, after)

    def test_live2d_layer_does_not_hard_import_the_renderer(self):
        text = (ROOT / "avatar" / "live2d.py").read_text(encoding="utf-8")
        self.assertNotIn("import webview", text)


if __name__ == "__main__":
    unittest.main()
