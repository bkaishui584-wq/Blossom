"""V8 收尾验收：贴边自动吸附、输入框占位提示、收起后的导航、角色画面优先。

覆盖：拖动途中碰到屏幕边就吸上去（不用松手，也不用右键菜单）、走远了自动松开、
输入框那行灰字不能被一个字一个字删掉、也不能被当成消息发出去、
导航收起时不画放不下的分组标题、配了模型时不摆占位形象（接不上才退回并说明原因）。
全程临时数据库，不碰真实用户数据。
"""

import sys
import tempfile
import tkinter as tk
import tkinter.font as tkfont
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.context import ContextManager
from agent.core import AgentCore
from database.db import Database
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatDelta, ChatResult, ModelProvider, Usage
from persona.interface import DefaultPersonaProvider
from security.config import Config
from ui import theme
from ui.chat_view import ChatInput
from avatar import web2d
from avatar import web2d_renderer
from ui.desktop import DesktopPet
from ui.pet_host import DOCK_GAP, HIT_TAG


class SilentProvider(ModelProvider):
    name = "silent"

    def is_available(self):
        return False

    def chat(self, messages, tools=None, temperature=0.7):
        return ChatResult(ok=False, error="no model", usage=Usage(), model="silent")

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


class FakeKey:
    def __init__(self, keysym, state=0):
        self.keysym = keysym
        self.state = state


class FakeEvent:
    def __init__(self, x_root, y_root):
        self.x_root = x_root
        self.y_root = y_root


class FakeModel:
    path = "C:/fake/live2D0.model3.json"
    id = "live2D0"


class FakeHost:
    """假的渲染宿主：只记下被调了哪些接口，不真开窗口。"""

    name = "web2d"

    def __init__(self, ok=True, available=True):
        self.ok = ok
        self.available = available
        self.sink = None
        self.loads = 0

    def is_available(self):
        return self.available

    def running(self):
        return bool(self.ok)

    def unavailable_reason(self):
        return "没有可用的渲染环境"

    def attach_window(self, handle):
        return handle

    def set_sink(self, sink):
        self.sink = sink
        return sink

    def load_model(self, model=None):
        self.loads += 1
        if not self.ok:
            return {"ok": False, "error": "渲染进程没起来"}
        return {"ok": True, "note": "正在加载角色"}

    def set_visible(self, visible):
        return visible

    def sync_geometry(self):
        return True

    def get_status(self):
        return {"host": "web2d", "state": "starting"}


class FakeAvatar:
    """冒充 Live2D 形象层：只实现桌面宿主认的那几个接口。"""

    name = "live2d"

    def __init__(self, host=None, model=None):
        self.host = host if host is not None else FakeHost()
        self.model = model if model is not None else FakeModel()
        self.state = {}

    def attach_fallback(self, fallback):
        return fallback

    def load_model(self):
        return self.host.load_model(self.model)

    def set_state(self, state):
        self.state = dict(state or {})
        return self.state


class TkCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(theme.reset_scale)
        theme.reset_scale()
        try:
            self.root = tk.Tk()
        except Exception as exc:  # pragma: no cover
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
        self.config.tray = False

    def _destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def build_pet(self, avatar=None):
        database = Database(self.config.db_path)
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        agent = AgentCore(
            SilentProvider(), memory, ContextManager(token_budget=800),
            persona=DefaultPersonaProvider(),
        )
        if avatar is not None:
            agent.avatar = avatar
        pet = DesktopPet(agent, self.config, memory=memory, root=self.root)
        self.addCleanup(pet.close)
        pet.show_chat()
        return pet, memory, database


class SnapWhileDraggingTests(TkCase):
    """拖着走碰到屏幕边就吸上去，走远了再松开。"""

    def drag(self, pet, to_x, to_y):
        """按住窗口里的一点，把整个窗口挪到目标位置。"""
        host = pet.host
        x0, y0 = host.position()
        host._press(FakeEvent(x0 + 30, y0 + 30))
        host._motion(FakeEvent(to_x + 30, to_y + 30))
        return host

    def test_touching_the_edge_snaps_before_the_mouse_is_released(self):
        pet, _memory, _db = self.build_pet()
        host = pet.host
        left = host.bounds()[0]
        self.drag(pet, left + 2, 500)
        # 还没松手，就已经贴上左边了。
        self.assertEqual(host.docked_edge(), "left")
        self.assertEqual(host.position()[0], left + DOCK_GAP)

    def test_dragging_away_releases_the_edge(self):
        pet, _memory, _db = self.build_pet()
        host = pet.host
        left = host.bounds()[0]
        self.drag(pet, left + 2, 500)
        self.assertEqual(host.docked_edge(), "left")
        host._motion(FakeEvent(left + 700 + 30, 520 + 30))
        self.assertEqual(host.docked_edge(), "")

    def test_a_small_nudge_does_not_count_as_a_drag(self):
        pet, _memory, _db = self.build_pet()
        host = pet.host
        x0, y0 = host.position()
        host._press(FakeEvent(x0 + 30, y0 + 30))
        host._motion(FakeEvent(x0 + 31, y0 + 31))
        self.assertEqual(host.docked_edge(), "")

    def test_snapping_while_dragging_does_not_slide_in(self):
        pet, _memory, _db = self.build_pet()
        host = pet.host
        left = host.bounds()[0]
        self.drag(pet, left + 2, 500)
        self.assertFalse(host.peeked())


class ComposerGhostTests(TkCase):
    """那行灰字只能整行让开，不能被啃掉一半。"""

    def input(self):
        frame = tk.Frame(self.root)
        widget = ChatInput(frame)
        widget.pack()
        self.root.update_idletasks()
        return widget

    def test_backspace_removes_the_whole_hint(self):
        widget = self.input()
        self.assertTrue(widget.has_placeholder())
        widget.delete("insert-1c")
        self.assertFalse(widget.has_placeholder())
        self.assertEqual(widget.get(), "")

    def test_backspace_leaves_the_hint_untouched(self):
        widget = self.input()
        self.assertEqual(widget._first_key(FakeKey("BackSpace")), "break")
        self.assertTrue(widget.has_placeholder())
        self.assertEqual(widget.get(), "")

    def test_the_delete_key_does_not_eat_the_hint_either(self):
        widget = self.input()
        self.assertEqual(widget._first_key(FakeKey("Delete")), "break")
        self.assertTrue(widget.has_placeholder())
        self.assertEqual(widget.get(), "")

    def test_the_hint_never_shrinks_one_letter_at_a_time(self):
        widget = self.input()
        widget.delete("1.0", "1.3")
        self.assertFalse(widget.has_placeholder())
        self.assertEqual(widget.get(), "")

    def test_the_first_letter_clears_the_hint_and_lands(self):
        widget = self.input()
        self.assertIsNone(widget._first_key(FakeKey("a")))
        self.assertFalse(widget.has_placeholder())
        widget.insert("1.0", "你")
        self.assertEqual(widget.get(), "你")

    def test_clicking_into_the_box_keeps_the_hint(self):
        widget = self.input()
        widget._focus_in()
        self.assertTrue(widget.has_placeholder())
        self.assertEqual(widget.get(), "")

    def test_emptying_the_box_brings_the_hint_back(self):
        widget = self.input()
        widget.insert("1.0", "你好")
        self.assertFalse(widget.has_placeholder())
        widget.delete("1.0", "end")
        widget._changed()
        self.assertTrue(widget.has_placeholder())
        self.assertEqual(widget.get(), "")

    def test_arrow_keys_do_not_walk_into_the_hint(self):
        widget = self.input()
        self.assertEqual(widget._first_key(FakeKey("Left")), "break")
        self.assertTrue(widget.has_placeholder())

    def test_the_hint_is_never_taken_as_content(self):
        widget = self.input()
        self.assertEqual(widget.get(), "")


class RendererDragTests(TkCase):
    """角色窗口转发来的拖动，松手也要按距离决定贴不贴边。"""

    def test_the_renderer_drag_docks_on_release(self):
        pet, _memory, _db = self.build_pet()
        left = pet.host.bounds()[0]
        factor = pet._live2d_factor or 1.0
        pet._apply_live2d_position(
            {"x": (left + 2) * factor, "y": 400 * factor}, final=True
        )
        self.assertEqual(pet.host.docked_edge(), "left")
        self.assertEqual(pet.host.position()[0], left + DOCK_GAP)
        self.assertEqual(pet.dock_text(), "贴在左边")

    def test_a_renderer_drag_in_the_middle_stays_put(self):
        pet, _memory, _db = self.build_pet()
        pet._apply_live2d_position({"x": 600, "y": 400}, final=True)
        self.assertEqual(pet.host.docked_edge(), "")

    def test_an_undocked_pet_stays_undocked_when_docking_is_off(self):
        pet, _memory, _db = self.build_pet()
        pet.set_dock(False)
        left = pet.host.bounds()[0]
        pet._apply_live2d_position({"x": left + 1, "y": 400}, final=True)
        self.assertEqual(pet.host.docked_edge(), "")


class NavCollapsedTests(TkCase):
    """收起之后那一列放不下标题，就别画。"""

    def test_collapsing_hides_the_section_headings(self):
        pet, _memory, _db = self.build_pet()
        view = pet.chat
        view.toggle_nav(force=True)
        self.assertFalse(view.nav_is_collapsed())
        view.toggle_nav(force=False)
        self.assertTrue(view.nav_is_collapsed())
        for label in view.nav_group_labels.values():
            self.assertNotEqual(str(label.winfo_manager()), "pack")

    def test_expanding_brings_them_back(self):
        pet, _memory, _db = self.build_pet()
        view = pet.chat
        view.toggle_nav(force=False)
        view.toggle_nav(force=True)
        for label in view.nav_group_labels.values():
            self.assertEqual(str(label.winfo_manager()), "pack")

    def test_the_item_labels_stay_hidden_while_collapsed(self):
        pet, _memory, _db = self.build_pet()
        view = pet.chat
        view.toggle_nav(force=False)
        for text in view.nav_texts.values():
            self.assertNotEqual(str(text.winfo_manager()), "pack")

    def test_the_collapsed_icons_are_bigger_than_the_expanded_ones(self):
        pet, _memory, _db = self.build_pet()
        view = pet.chat

        def point_size(key):
            widget = view.nav_icons[key]
            return int(tkfont.Font(root=self.root, font=widget.cget("font")).cget("size"))

        view.toggle_nav(force=True)
        expanded = point_size("tasks")
        view.toggle_nav(force=False)
        collapsed = point_size("tasks")
        self.assertGreater(collapsed, expanded)


class AvatarPriorityTests(TkCase):
    """配了模型就只显示 Live2D；接不上时保持透明载体并说清原因。"""

    def test_a_configured_model_clears_the_placeholder(self):
        host = FakeHost()
        pet, _memory, _db = self.build_pet(avatar=FakeAvatar(host=host))
        self.assertIsNotNone(host.sink)
        self.assertTrue(pet.pet.blank)
        self.assertEqual(host.loads, 1)

    def test_a_model_that_will_not_load_falls_back_and_says_why(self):
        pet, _memory, _db = self.build_pet(avatar=FakeAvatar(host=FakeHost(ok=False)))
        self.assertTrue(pet.pet.blank)
        text = pet.chat.transcript.get("1.0", "end-1c")
        self.assertIn("角色画面没起来", text)
        self.assertIn("渲染进程没起来", text)

    def test_no_render_environment_falls_back_and_says_why(self):
        pet, _memory, _db = self.build_pet(avatar=FakeAvatar(host=FakeHost(available=False)))
        self.assertTrue(pet.pet.blank)
        self.assertIn(
            "没有可用的渲染环境", pet.chat.transcript.get("1.0", "end-1c")
        )

    def test_without_a_model_only_the_transparent_hit_area_stays(self):
        pet, _memory, _db = self.build_pet()
        self.assertTrue(pet.pet.blank)
        self.assertTrue(pet.host.has_hit())

    def test_the_old_canvas_face_is_not_drawn_any_more(self):
        pet, _memory, _db = self.build_pet()
        pet.pet.render("happy")
        pet.pet.set_blank(False)
        for item in pet.pet.find_all():
            self.assertIn(HIT_TAG, pet.pet.gettags(item))


class FakeRecoveringHost(FakeHost):
    """假的渲染宿主：会照实回答「我现在画着没有」。"""

    def __init__(self, state="ready", **kw):
        super().__init__(**kw)
        self.state = state
        self.reacts = []

    def refresh_status(self, timeout=1.5):
        return {"host": "web2d", "state": self.state, "ready": self.state == "ready"}

    def react(self, name="click"):
        self.reacts.append(name)
        return {"ok": self.state == "ready"}


class RendererLoadVerdictTests(unittest.TestCase):
    """画面已经在画，就不能因为一次读不到状态就报「角色加载失败」。"""

    MODEL = ROOT / "resources" / "live2d" / "renderer.html"

    def renderer(self, answer):
        r = web2d_renderer.Renderer.__new__(web2d_renderer.Renderer)
        r.state = {
            "ready": False, "status": {}, "mask": None, "hwnd": 0,
            "size": (0, 0), "scale": 1.0, "visible": True,
        }
        r.dpi_scale = 1.0
        r.model = self.MODEL
        r.hotspot = mock.Mock()
        r._own_hwnd = lambda: 0
        r.run_js = answer
        self.events = []
        return r

    def emit_into(self):
        return mock.patch.object(
            web2d_renderer, "emit",
            lambda event, **kw: self.events.append((event, kw)),
        )

    def test_ready_wins_over_a_status_read_that_came_back_empty(self):
        def answer(script):
            if script.startswith("window.Pet.load"):
                return None
            if "status().ready" in script:
                return True
            return None  # 读状态的两条脚本都空手而回

        r = self.renderer(answer)
        with self.emit_into():
            r.load_model()
        self.assertEqual([event for event, _kw in self.events], ["ready"])
        self.assertTrue(r.state["ready"])

    def test_a_status_read_that_came_back_empty_keeps_the_last_answer(self):
        r = self.renderer(lambda script: None)
        r.state["status"] = {"ready": True, "host": [120, 200]}
        with self.emit_into():
            r._refresh_hit()
        self.assertTrue(r.state["status"].get("ready"))

    def test_a_model_that_is_really_missing_still_reports_failure(self):
        def answer(script):
            if script.startswith("window.Pet.load"):
                return None
            return False

        r = self.renderer(answer)
        r.model = Path(r"C:\definitely\not\here\x.model3.json")
        with self.emit_into():
            r.load_model()
        # 文件都没有，连问都不用问。
        self.assertEqual([event for event, _kw in self.events], ["loadfail"])
        self.assertFalse(r.state["ready"])


class HostStatusRecoveryTests(unittest.TestCase):
    """渲染进程自己报就绪，宿主就不能一直把它按在「加载失败」上。"""

    def host(self):
        with mock.patch.object(web2d, "_webview_ready", return_value=True):
            h = web2d.WebLive2DHost(runtime=ROOT / "resources" / "live2d")
        h._state = "failed"
        h._error = "角色加载失败"
        return h

    def test_a_ready_report_clears_a_stale_failure(self):
        h = self.host()
        h._dispatch({"ev": "status", "data": {"ready": True, "status": "ready"}})
        self.assertEqual(h._state, "ready")
        self.assertEqual(h._error, "")
        self.assertTrue(h.react("hover")["ok"])

    def test_a_still_failing_render_stays_failed(self):
        h = self.host()
        h._dispatch({"ev": "status", "data": {"ready": False}})
        self.assertEqual(h._state, "failed")
        self.assertFalse(h.react("hover")["ok"])


class AvatarErrorRecoveryTests(TkCase):
    """画面还在画时保留角色；真挂了也只留透明载体。"""

    def test_a_false_error_keeps_the_character(self):
        host = FakeRecoveringHost(state="ready")
        pet, _memory, _db = self.build_pet(avatar=FakeAvatar(host=host))
        pet._handle_live2d_event("live2d_error", {"error": "角色加载失败"})
        self.assertTrue(pet.pet.blank)
        self.assertEqual(pet._live2d_problem, "")
        self.assertEqual(host.reacts, [])

    def test_a_real_error_leaves_only_the_transparent_carrier(self):
        host = FakeRecoveringHost(state="failed")
        pet, _memory, _db = self.build_pet(avatar=FakeAvatar(host=host))
        pet._handle_live2d_event("live2d_error", {"error": "角色加载失败"})
        self.assertTrue(pet.pet.blank)


if __name__ == "__main__":
    unittest.main()
