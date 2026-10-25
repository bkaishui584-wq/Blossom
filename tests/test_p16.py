"""P16 验收：行为层 —— 待机、互动、打扰门禁、开关与免打扰、无模型参与。"""

import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.context import ContextManager
from agent.core import AgentCore
from avatar.interface import DefaultAvatarProvider
from behavior import actions
from behavior.manager import (
    INTERACT,
    SILENT,
    WAIT,
    BehaviorManager,
    NullWindowProvider,
    WindowProvider,
)
from database.db import Database
from events import bus as bus_module
from events.bus import Event, EventBus
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatResult, ModelProvider
from persona.interface import DefaultPersonaProvider
from persona.state import EmotionState


class Clock:
    def __init__(self, start=0.0):
        self.t = float(start)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds
        return self.t


class FixedRandom:
    """固定随机：待机永远取第一个动作，间隔永远取下限。"""

    def __init__(self, value=0):
        self.value = value

    def choice(self, seq):
        return seq[self.value % len(seq)]

    def randint(self, low, high):
        return low


class FakePreferences:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get_preference(self, key):
        return self.values.get(key)

    def set_preference(self, key, value):
        self.values[key] = value


class BusyWindow(WindowProvider):
    def __init__(self, busy=True):
        self.busy = busy

    def is_busy(self, now=None):
        return self.busy


class BrokenWindow(WindowProvider):
    def is_busy(self, now=None):
        raise RuntimeError("probe failed")


class RecordingAvatar(DefaultAvatarProvider):
    def __init__(self, explode=False):
        super().__init__()
        self.explode = explode
        self.events = []

    def set_expression(self, expression):
        if self.explode:
            raise RuntimeError("avatar down")
        self.events.append(("expression", expression))
        return super().set_expression(expression)

    def play_animation(self, animation):
        if self.explode:
            raise RuntimeError("avatar down")
        self.events.append(("animation", animation))
        return super().play_animation(animation)


class ScriptProvider(ModelProvider):
    name = "script"

    def __init__(self, reply="嗯。", available=True):
        self.reply = reply
        self.available = available
        self.calls = []

    def is_available(self):
        return self.available

    def chat(self, messages, tools=None, temperature=0.7):
        self.calls.append([dict(m) for m in messages])
        return ChatResult(ok=True, content=self.reply, model=self.name)

    def stream(self, messages, tools=None, temperature=0.7):
        raise NotImplementedError


def build(prefs=None, hour_of=None, window=None, clock=None, avatar=None, **kwargs):
    clock = clock or Clock()
    manager = BehaviorManager(
        emotion=EmotionState(),
        avatar=avatar if avatar is not None else DefaultAvatarProvider(),
        preferences=prefs,
        clock=clock,
        hour_of=hour_of or (lambda ts: 12),
        random_source=FixedRandom(),
        window=window,
        **kwargs,
    )
    return manager, clock


class ActionTableTests(unittest.TestCase):
    def test_kinds_cover_required_actions(self):
        for kind in (
            actions.IDLE,
            actions.SPEAK,
            actions.ANIMATE,
            actions.SLEEP,
            actions.REMIND,
            actions.INTERACT,
        ):
            self.assertIn(kind, actions.KINDS)

    def test_action_roundtrip(self):
        data = actions.interaction_action("click").to_dict()
        self.assertEqual(data["kind"], actions.INTERACT)
        self.assertEqual(data["data"]["interaction"], "click")

    def test_unknown_interaction_still_safe(self):
        action = actions.interaction_action("teleport")
        self.assertEqual(action.kind, actions.INTERACT)
        self.assertEqual(action.data["interaction"], "teleport")

    def test_speak_action_keeps_text(self):
        action = actions.speak_action("该喝水了")
        self.assertEqual(action.kind, actions.SPEAK)
        self.assertEqual(action.data["text"], "该喝水了")

    def test_interactions_cover_required_gestures(self):
        for name in (
            "click",
            "double_click",
            "drag",
            "long_press",
            "right_click",
            "pointer_near",
            "pointer_away",
        ):
            self.assertIn(name, actions.INTERACTIONS)


class ShouldInteractTests(unittest.TestCase):
    def test_daytime_allows(self):
        manager, _ = build()
        self.assertEqual(manager.should_interact(), INTERACT)

    def test_quiet_hours_are_silent(self):
        manager, _ = build(hour_of=lambda ts: 3)
        self.assertEqual(manager.should_interact(), SILENT)

    def test_urgent_pierces_quiet_hours(self):
        manager, _ = build(hour_of=lambda ts: 3)
        self.assertEqual(manager.should_interact(urgent=True), INTERACT)

    def test_high_priority_pierces_quiet_hours(self):
        manager, _ = build(hour_of=lambda ts: 3)
        self.assertEqual(manager.should_interact(priority="high"), INTERACT)

    def test_user_switch_wins_even_over_urgent(self):
        prefs = FakePreferences({"proactive_enabled": "0"})
        manager, _ = build(prefs=prefs, hour_of=lambda ts: 3)
        self.assertEqual(manager.should_interact(urgent=True), SILENT)

    def test_quiet_hours_from_preference(self):
        prefs = FakePreferences({"quiet_hours": "9-18"})
        manager, _ = build(prefs=prefs, hour_of=lambda ts: 10)
        self.assertEqual(manager.should_interact(), SILENT)

    def test_fullscreen_window_waits(self):
        manager, _ = build(window=BusyWindow(True))
        self.assertEqual(manager.should_interact(), WAIT)

    def test_broken_window_probe_is_safe(self):
        manager, _ = build(window=BrokenWindow())
        self.assertEqual(manager.should_interact(), INTERACT)

    def test_recent_activity_waits(self):
        manager, _ = build()
        manager.note_activity()
        self.assertEqual(manager.should_interact(), WAIT)

    def test_cooldown_waits_then_expires(self):
        manager, clock = build()
        self.assertEqual(manager.should_interact(), INTERACT)
        self.assertEqual(manager.should_interact(), WAIT)
        clock.advance(manager.cooldown + 1)
        self.assertEqual(manager.should_interact(), INTERACT)


class IdleTests(unittest.TestCase):
    def test_idle_returns_small_motion(self):
        manager, _ = build()
        action = manager.idle()
        self.assertIsNotNone(action)
        self.assertEqual(action.kind, actions.IDLE)

    def test_idle_is_throttled(self):
        manager, _ = build()
        self.assertIsNotNone(manager.idle())
        self.assertIsNone(manager.idle())

    def test_idle_returns_after_gap(self):
        manager, clock = build()
        manager.idle()
        clock.advance(manager.idle_gap[1] + 5)
        self.assertIsNotNone(manager.idle())

    def test_no_idle_during_quiet_hours(self):
        manager, _ = build(hour_of=lambda ts: 3)
        self.assertIsNone(manager.idle())

    def test_deep_night_long_silence_sleeps(self):
        manager, clock = build(hour_of=lambda ts: 3)
        manager.note_activity()
        clock.advance(manager.sleep_after + 1)
        action = manager.idle()
        self.assertEqual(action.kind, actions.SLEEP)
        self.assertTrue(manager.sleeping)

    def test_sleep_happens_once(self):
        manager, clock = build(hour_of=lambda ts: 3)
        manager.note_activity()
        clock.advance(manager.sleep_after + 1)
        manager.idle()
        self.assertIsNone(manager.idle())

    def test_activity_wakes_up(self):
        manager, clock = build(hour_of=lambda ts: 3)
        manager.note_activity()
        clock.advance(manager.sleep_after + 1)
        manager.idle()
        manager.note_activity()
        self.assertFalse(manager.sleeping)

    def test_lively_persona_moves_more(self):
        manager, _ = build(persona=DefaultPersonaProvider(traits={"活泼": 80}))
        self.assertLess(manager._persona_factor(), 1.0)

    def test_quiet_persona_moves_less(self):
        manager, _ = build(persona=DefaultPersonaProvider(traits={"安静": 80}))
        self.assertGreater(manager._persona_factor(), 1.0)


class InteractionTests(unittest.TestCase):
    def test_click_maps_to_happy_nod(self):
        manager, _ = build()
        action = manager.on_interaction("click")
        self.assertEqual(action.emotion, "happy")
        self.assertEqual(action.animation, "nod")

    def test_every_gesture_has_an_action(self):
        manager, _ = build()
        for name in actions.INTERACTIONS:
            self.assertEqual(manager.on_interaction(name).kind, actions.INTERACT)

    def test_interaction_updates_emotion_and_avatar(self):
        avatar = RecordingAvatar()
        manager, _ = build(avatar=avatar)
        manager.on_interaction("double_click")
        self.assertEqual(manager.emotion.emotion, "excited")
        self.assertEqual(avatar.state.get("emotion"), "excited")
        self.assertIn(("animation", "jump"), avatar.events)

    def test_interaction_notes_activity(self):
        manager, clock = build()
        manager.on_interaction("drag")
        self.assertEqual(manager.last_activity, clock.t)
        self.assertEqual(manager.interaction_count, 1)

    def test_unknown_gesture_is_safe(self):
        manager, _ = build()
        self.assertEqual(manager.on_interaction("poke-nonsense").kind, actions.INTERACT)

    def test_broken_avatar_does_not_raise(self):
        manager, _ = build(avatar=RecordingAvatar(explode=True))
        self.assertEqual(manager.on_interaction("click").kind, actions.INTERACT)


class ApplyTests(unittest.TestCase):
    def test_sleep_action_pushes_sleepy_expression(self):
        avatar = RecordingAvatar()
        manager, _ = build(avatar=avatar)
        manager.apply(actions.SLEEP_ACTION)
        self.assertEqual(manager.emotion.emotion, "sleepy")
        self.assertEqual(avatar.expression, "sleepy")

    def test_apply_publishes_bus_event(self):
        bus = EventBus()
        seen = []
        bus.subscribe(bus_module.BEHAVIOR, lambda event: seen.append(event))
        manager, _ = build(bus=bus)
        manager.on_interaction("click")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].payload["kind"], actions.INTERACT)

    def test_describe_exposes_state(self):
        manager, _ = build()
        manager.on_interaction("click")
        info = manager.describe()
        self.assertEqual(info["interaction_count"], 1)
        self.assertEqual(info["last_action"]["kind"], actions.INTERACT)
        self.assertIn("quiet_hours", info)
        self.assertTrue(info["proactive_allowed"])


class WindowProviderTests(unittest.TestCase):
    def test_null_window_is_never_busy(self):
        self.assertFalse(NullWindowProvider().is_busy())

    def test_window_provider_is_replaceable(self):
        manager, _ = build(window=BusyWindow(False))
        self.assertEqual(manager.should_interact(), INTERACT)


class AgentGateTests(unittest.TestCase):
    def _agent(self, provider=None):
        tmp = tempfile.mkdtemp()
        database = Database(Path(tmp) / "t.sqlite3")
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        agent = AgentCore(provider, memory, ContextManager(token_budget=400))
        return agent, memory

    def test_silent_during_quiet_hours_skips_model(self):
        provider = ScriptProvider()
        agent, _ = self._agent(provider)
        agent.attach_behavior(
            BehaviorManager(
                emotion=agent.emotion, avatar=agent.avatar, hour_of=lambda ts: 3
            )
        )
        event = Event(name=bus_module.COMPANION, payload={"situation": "提醒休息"})
        reply = agent.respond_to_event(event)
        self.assertTrue(reply.ok)
        self.assertEqual(reply.content, "")
        self.assertEqual(provider.calls, [])

    def test_allowed_event_reaches_model(self):
        provider = ScriptProvider(reply="起来走两步吧")
        agent, _ = self._agent(provider)
        agent.attach_behavior(
            BehaviorManager(
                emotion=agent.emotion, avatar=agent.avatar, hour_of=lambda ts: 12
            )
        )
        event = Event(name=bus_module.COMPANION, payload={"situation": "提醒休息"})
        reply = agent.respond_to_event(event)
        self.assertEqual(reply.content, "起来走两步吧")
        self.assertEqual(len(provider.calls), 1)

    def test_user_timer_is_never_suppressed(self):
        agent, _ = self._agent(None)
        behavior = BehaviorManager(
            emotion=agent.emotion, avatar=agent.avatar, hour_of=lambda ts: 12
        )
        behavior.cooldown = 10 ** 9
        behavior.last_proactive_at = behavior.clock()
        agent.attach_behavior(behavior)
        event = Event(
            name=bus_module.COMPANION,
            payload={"local_text": "时间到了", "capability": "timer"},
        )
        reply = agent.respond_to_event(event)
        self.assertEqual(reply.content, "时间到了")

    def test_user_message_records_activity(self):
        agent, _ = self._agent(None)
        behavior = BehaviorManager(emotion=agent.emotion, avatar=agent.avatar)
        agent.attach_behavior(behavior)
        agent.respond("s1", "你好")
        self.assertIsNotNone(behavior.last_activity)


class FakeEvent:
    def __init__(self, x_root=0, y_root=0):
        self.x_root = x_root
        self.y_root = y_root


class DesktopWiringTests(unittest.TestCase):
    """需要显示环境；没有显示时自动跳过。"""

    def setUp(self):
        try:
            self.root = tk.Tk()
        except Exception as exc:
            self.skipTest(f"tkinter 不可用：{exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _destroy_root(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _pet(self):
        from security.config import Config
        from ui.desktop import DesktopPet

        database = Database(Path(self.tmp.name) / "t.sqlite3")
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        agent = AgentCore(None, memory, ContextManager(token_budget=400))
        behavior = BehaviorManager(
            emotion=agent.emotion, avatar=agent.avatar, hour_of=lambda ts: 12
        )
        pet = DesktopPet(
            agent,
            Config(load_env=False),
            memory=memory,
            behavior=behavior,
            root=self.root,
        )
        return pet, behavior

    def test_ui_points_behavior_at_the_displayed_avatar(self):
        pet, behavior = self._pet()
        self.assertIs(behavior.avatar, pet.avatar)

    def test_pet_click_runs_behavior_interaction(self):
        pet, behavior = self._pet()
        pet.host._press(FakeEvent(500, 400))
        pet.host._release(FakeEvent(500, 400))
        self.assertEqual(behavior.emotion.emotion, "happy")
        self.assertEqual(behavior.interaction_count, 1)

    def test_right_click_is_bound_to_the_menu(self):
        pet, _ = self._pet()
        self.assertEqual(pet.host.on_right_click, pet.open_menu)

    def test_idle_tick_runs_without_error(self):
        pet, behavior = self._pet()
        pet._idle_tick()
        self.assertGreaterEqual(behavior.idle_count, 1)


if __name__ == "__main__":
    unittest.main()
