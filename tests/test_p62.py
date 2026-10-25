"""P62 阶段 H：桌宠的互动触发（长按、放下、拖动、点击）。

要点：
    按住不动够久才算长按，拖起来或松手就取消；
    拖到别处再松手会发「放下」，同时位置照样记住；
    每次触发都同时进行为层和形象层，两层各司其职；
    页面控制器认识的五种反应与宿主发的名字对得上，不存在发了没人认的反应。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from behavior import actions as behavior_actions  # noqa: E402
from security.config import Config  # noqa: E402

PAGE = ROOT / "resources" / "live2d" / "pet_page.js"


class TimerRoot:
    """把 after / after_cancel 换成手动触发，让长按在测试里是确定性的。"""

    def __init__(self, real):
        self.real = real
        self.scheduled = {}
        self.cancelled = []
        self._next = 0

    def after(self, ms, callback):
        self._next += 1
        self.scheduled[self._next] = (ms, callback)
        return self._next

    def after_cancel(self, timer):
        self.cancelled.append(timer)
        self.scheduled.pop(timer, None)

    def fire(self):
        "把到点的定时器全部跑一遍，返回每个的返回值。"
        pending = sorted(self.scheduled)
        callbacks = [self.scheduled.pop(key)[1] for key in pending]
        return [callback() for callback in callbacks]

    def __getattr__(self, name):
        return getattr(self.real, name)


class HostEventTests(unittest.TestCase):
    def setUp(self):
        try:
            import tkinter as tk
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        from ui.pet_host import PetHost

        try:
            self.window = tk.Tk()
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        self.window.withdraw()
        self.addCleanup(self._destroy)
        self.host = PetHost(
            self.window, build_canvas=lambda parent: tk.Frame(parent, width=10, height=10)
        )
        self.timers = TimerRoot(self.window)
        self.host.root = self.timers

    def _destroy(self):
        try:
            self.window.destroy()
        except Exception:
            pass

    def event(self, x, y):
        from types import SimpleNamespace

        return SimpleNamespace(x_root=x, y_root=y, x=x, y=y)

    def test_long_press_fires_only_after_the_delay(self):
        seen = []
        self.host.on_long_press = seen.append
        self.host._press(self.event(100, 100))
        self.assertEqual(seen, [])
        self.timers.fire()
        self.assertEqual(len(seen), 1)

    def test_releasing_early_cancels_the_long_press(self):
        seen = []
        clicks = []
        self.host.on_long_press = seen.append
        self.host.on_click = clicks.append
        self.host._press(self.event(100, 100))
        self.host._release(self.event(100, 100))
        self.assertEqual(seen, [])
        self.assertEqual(len(clicks), 1)
        self.assertTrue(self.timers.cancelled)

    def test_dragging_cancels_the_long_press(self):
        seen = []
        self.host.on_long_press = seen.append
        self.host._press(self.event(100, 100))
        self.host._motion(self.event(140, 160))
        self.timers.fire()
        self.assertEqual(seen, [])
        self.assertTrue(self.timers.cancelled)

    def test_a_fired_timer_does_nothing_after_the_mouse_is_up(self):
        seen = []
        self.host.on_long_press = seen.append
        self.host._press(self.event(100, 100))
        self.host._drag = None
        self.timers.fire()
        self.assertEqual(seen, [])

    def test_no_callback_means_no_timer_is_armed(self):
        self.host._press(self.event(100, 100))
        self.assertEqual(self.timers.scheduled, {})

    def test_dropping_the_pet_reports_both_the_reaction_and_the_new_place(self):
        seen = []
        moved = []
        self.host.on_drag_end = seen.append
        self.host.on_moved = moved.append
        self.host._press(self.event(100, 100))
        self.host._motion(self.event(160, 190))
        self.host._release(self.event(160, 190))
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(moved), 1)
        self.assertEqual(seen[0], moved[0])

    def test_a_plain_click_still_reports_only_a_click(self):
        clicks = []
        seen = []
        moved = []
        self.host.on_click = clicks.append
        self.host.on_drag_end = seen.append
        self.host.on_moved = moved.append
        self.host._press(self.event(100, 100))
        self.host._release(self.event(101, 101))
        self.assertEqual(len(clicks), 1)
        self.assertEqual(seen, [])
        self.assertEqual(moved, [])


class BehaviourTableTests(unittest.TestCase):
    def test_the_behaviour_layer_already_knows_long_press(self):
        action = behavior_actions.interaction_action("long_press")
        self.assertEqual(action.data.get("interaction"), "long_press")
        self.assertNotEqual(action.reason, "未知互动")

    def test_unknown_interactions_do_not_blow_up(self):
        action = behavior_actions.interaction_action("完全没有这个")
        self.assertEqual(action.reason, "未知互动")


class PageContractTests(unittest.TestCase):
    def test_every_reaction_the_host_sends_is_understood_by_the_page(self):
        text = PAGE.read_text(encoding="utf-8")
        for name in ("click", "double_click", "hover", "long_press", "drag_end"):
            self.assertIn("'%s'" % name, text, name)

    def test_the_host_only_sends_names_the_page_knows(self):
        desktop = (ROOT / "ui" / "desktop.py").read_text(encoding="utf-8")
        page = PAGE.read_text(encoding="utf-8")
        sent = set()
        marker = "_live2d_react("
        index = 0
        while True:
            index = desktop.find(marker, index)
            if index < 0:
                break
            tail = desktop[index + len(marker):]
            quote = tail[:1]
            if quote in ("'", '"'):
                end = tail.find(quote, 1)
                sent.add(tail[1:end])
            index += len(marker)
        self.assertTrue(sent)
        for name in sorted(sent):
            self.assertIn("'%s'" % name, page, name)


class DesktopWiringTests(unittest.TestCase):
    def setUp(self):
        try:
            import tkinter as tk
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        try:
            self.window = tk.Tk()
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        self.window.withdraw()
        self.addCleanup(self._destroy)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _destroy(self):
        try:
            self.window.destroy()
        except Exception:
            pass

    def _pet(self):
        from agent.context import ContextManager
        from agent.core import AgentCore
        from database.db import Database
        from memory.manager import MemoryManager
        from memory.repository import MemoryRepository
        from model.base import ChatResult, ModelProvider
        from ui.desktop import DesktopPet

        class Silent(ModelProvider):
            name = "silent"

            def is_available(self):
                return True

            def chat(self, messages, tools=None, temperature=0.7):
                return ChatResult(ok=True, content="嗯。", model="silent")

            def stream(self, messages, tools=None, temperature=0.7):
                yield ChatResult(ok=True, content="嗯。", model="silent")

        class RecordingHost:
            name = "recording"

            def __init__(self):
                self.reactions = []

            def react(self, name="click"):
                self.reactions.append(name)
                return {"ok": True, "reaction": name}

        class RecordingBehaviour:
            def __init__(self):
                self.interactions = []

            def on_interaction(self, name, now=None):
                self.interactions.append(name)
                return None

        class Events:
            def emit(self, *args, **kwargs):
                return None

        base = Path(self.tmp.name)
        config = Config(load_env=False)
        config.data_dir = base
        config.log_dir = base / "logs"
        config.db_path = base / "t.sqlite3"
        config.characters_dir = base / "characters"
        config.live2d_dir = base / "live2d"
        config.devices_dir = base / "devices"
        config.voices_dir = base / "voices"
        config.tray = False
        database = Database(config.db_path)
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        agent = AgentCore(Silent(), memory, ContextManager(token_budget=800))
        behaviour = RecordingBehaviour()
        events = Events()
        pet = DesktopPet(
            agent, config, memory=memory, root=self.window, behavior=behaviour, bus=events
        )
        pet.live2d = RecordingHost()
        self.behaviour = behaviour
        return pet

    def test_long_press_reaches_both_layers(self):
        pet = self._pet()
        pet._on_pet_long_press((10, 20))
        self.assertEqual(pet.live2d.reactions, ["long_press"])
        self.assertEqual(self.behaviour.interactions, ["long_press"])

    def test_dropping_reaches_the_picture_layer(self):
        pet = self._pet()
        pet._on_pet_drag_end((30, 40))
        self.assertEqual(pet.live2d.reactions, ["drag_end"])

    def test_dragging_still_saves_the_new_place(self):
        pet = self._pet()
        saved = []
        pet._save_position = lambda: saved.append(pet.host.position())
        pet._on_pet_moved((44, 55))
        self.assertEqual(self.behaviour.interactions, ["drag"])
        self.assertEqual(len(saved), 1)

    def test_a_missing_render_window_is_not_an_error(self):
        pet = self._pet()
        pet.live2d = None
        self.assertIsNone(pet._on_pet_long_press((1, 2)))
        self.assertIsNone(pet._on_pet_drag_end((1, 2)))


if __name__ == "__main__":
    unittest.main()