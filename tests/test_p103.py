"""阶段 E 验收：人格、记忆与桌宠状态。

两件事：
  1. 抽象生命状态（待机 / 活跃 / 在想 / 在说话 / 工作 / 休息 / 睡觉 / 出错）由真实事件推进，
     不是靠模型轮询装出来的；非法转换会被拒绝；每次切换都发一条事件。
  2. 记忆分层站得住：用户明说的偏好压得住推断、单次要求不会变成长期习惯、
     "仅限这一次"的字段永远不会被自动采纳、说法冲突时新的顶掉旧的。

界面与状态机都不接模型；用临时目录和临时数据库。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.context import ContextManager  # noqa: E402
from agent.core import AgentCore  # noqa: E402
from behavior import life as life_module  # noqa: E402
from behavior.life import (  # noqa: E402
    ACTIVE,
    ERROR,
    IDLE,
    RESTING,
    SLEEPING,
    STATES,
    TALKING,
    THINKING,
    WORKING,
    LifeStateMachine,
)
from database.db import Database  # noqa: E402
from events.bus import EventBus  # noqa: E402
from memory.manager import MemoryManager  # noqa: E402
from memory.repository import MemoryRepository  # noqa: E402
from model.base import ChatDelta, ChatResult, ModelProvider, ModelSlot, Usage  # noqa: E402
from persona.interface import DefaultPersonaProvider  # noqa: E402
from persona.learner import ASK_FIELDS, AUTO_FIELDS, HabitLearner  # noqa: E402
from security.config import Config  # noqa: E402


class LifeStateTests(unittest.TestCase):
    """状态机本身：八种状态、合法转换、事件驱动。"""

    def test_the_eight_states_are_declared_with_labels(self):
        self.assertEqual(
            set(STATES),
            {IDLE, ACTIVE, THINKING, TALKING, WORKING, RESTING, SLEEPING, ERROR},
        )
        for state in STATES:
            self.assertTrue(life_module.LABELS.get(state), state)

    def test_thinking_and_talking_are_reachable_in_order(self):
        machine = LifeStateMachine(initial=ACTIVE)
        self.assertTrue(machine.note_thinking())
        self.assertEqual(machine.state, THINKING)
        self.assertTrue(machine.note_talking())
        self.assertEqual(machine.state, TALKING)
        self.assertTrue(machine.note_user_message())
        self.assertEqual(machine.state, ACTIVE, "用户开口就说明它说完了")

    def test_an_error_can_come_from_anywhere_and_recover(self):
        for start in (IDLE, ACTIVE, WORKING, RESTING, SLEEPING, THINKING, TALKING):
            machine = LifeStateMachine(initial=start)
            self.assertTrue(machine.note_error("出错了"), start)
            self.assertEqual(machine.state, ERROR)
            self.assertTrue(machine.note_settled(), start)
            self.assertEqual(machine.state, ACTIVE)

    def test_an_illegal_transition_is_refused_not_forced(self):
        machine = LifeStateMachine(initial=SLEEPING)
        self.assertFalse(machine.can(THINKING))
        self.assertFalse(machine.note_thinking(), "睡着的时候不该直接开始想")
        self.assertEqual(machine.state, SLEEPING)

    def test_every_switch_is_announced_on_the_bus(self):
        seen = []
        bus = EventBus()
        bus.subscribe("life.state", lambda event: seen.append(event.payload))
        machine = LifeStateMachine(bus=bus, initial=ACTIVE).attach(bus)
        machine.note_thinking()
        self.assertTrue(seen)
        self.assertEqual(seen[-1]["state"], THINKING)
        self.assertEqual(seen[-1]["label"], "在想")
        self.assertEqual(seen[-1]["previous"], ACTIVE)

    def test_user_events_drive_the_usual_states(self):
        bus = EventBus()
        machine = LifeStateMachine(bus=bus, initial=IDLE).attach(bus)
        bus.emit("user.message")
        self.assertEqual(machine.state, ACTIVE)
        bus.emit("user.idle")
        self.assertEqual(machine.state, IDLE)
        bus.emit("tool.completed")
        self.assertEqual(machine.state, WORKING)

    def test_a_short_state_can_be_settled_without_a_timer(self):
        """瞬态不靠定时器回退：下一个真事件把它带走。"""
        machine = LifeStateMachine(initial=ACTIVE)
        machine.note_talking()
        self.assertEqual(machine.state, TALKING)
        self.assertTrue(machine.note_settled("没人接话"))
        self.assertEqual(machine.state, ACTIVE)
        self.assertFalse(machine.note_settled(), "常态下再收一次就是空操作")


class _Stub(ModelProvider):
    name = "stub"

    def __init__(self, reply="在的。", fail=False):
        self.reply = reply
        self.fail = fail
        self.calls = 0

    def is_available(self):
        return True

    def chat(self, messages, tools=None, temperature=0.7):
        self.calls += 1
        if self.fail:
            return ChatResult(ok=False, error="模型没答上来", model="stub")
        return ChatResult(ok=True, content=self.reply, usage=Usage(), model="stub")

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


class AgentDrivenLifeTests(unittest.TestCase):
    """对话本身把状态推着走，没有谁在旁边轮询。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"DESKPET_ROOT": str(self.base)})
        env.start()
        self.addCleanup(env.stop)
        self.database = Database(self.base / "t.sqlite3")
        self.database.migrate()
        self.memory = MemoryManager(MemoryRepository(self.database))

    def agent(self, provider):
        agent = AgentCore(
            provider,
            self.memory,
            ContextManager(token_budget=800),
            persona=DefaultPersonaProvider(),
            config=Config(load_env=False),
            companion_slot=ModelSlot(role="companion", provider=provider, configured=True),
        )
        agent.life = LifeStateMachine(initial=ACTIVE)
        return agent

    def test_a_normal_turn_goes_through_thinking_and_talking(self):
        agent = self.agent(_Stub())
        reply = agent.respond("s", "陪我聊两句今天的事")
        self.assertTrue(reply.ok, reply.error)
        made = [(row["from"], row["to"]) for row in agent.life.history]
        self.assertIn((ACTIVE, THINKING), made)
        self.assertIn((THINKING, TALKING), made)
        self.assertEqual(agent.life.state, TALKING)

    def test_a_failing_model_lands_in_error(self):
        agent = self.agent(_Stub(fail=True))
        reply = agent.respond("s", "陪我聊两句今天的事")
        self.assertFalse(reply.ok)
        self.assertEqual(agent.life.state, ERROR)
        self.assertIn("模型", agent.life.reason)

    def test_a_local_answer_does_not_pretend_to_think(self):
        """本机能答的（报时间）不该走"在想"这一步。"""
        agent = self.agent(_Stub())
        reply = agent.respond("s", "现在几点")
        self.assertTrue(reply.ok, reply.error)
        self.assertNotIn(
            THINKING, [row["to"] for row in agent.life.history], "没叫模型就别装在想"
        )

    def test_no_state_machine_means_no_crash(self):
        agent = self.agent(_Stub())
        agent.life = None
        self.assertTrue(agent.respond("s", "陪我聊两句今天的事").ok)


class MemoryLayerTests(unittest.TestCase):
    """四层要说得出区别：明确偏好 / 长期习惯 / 推断 / 临时要求。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"DESKPET_ROOT": str(self.base)})
        env.start()
        self.addCleanup(env.stop)
        self.database = Database(self.base / "t.sqlite3")
        self.database.migrate()
        self.memory = MemoryManager(MemoryRepository(self.database))

    def test_an_inference_does_not_overwrite_what_the_user_said(self):
        self.memory.remember("用户喜欢喝美式咖啡", source="USER_EXPLICIT")
        self.memory.remember("用户喜欢喝拿铁咖啡", source="MODEL_INFERRED")
        rows = {row["content"]: row for row in self.memory.browse(kind="long_term")}
        explicit = rows["用户喜欢喝美式咖啡"]
        guessed = rows["用户喜欢喝拿铁咖啡"]
        self.assertEqual(explicit["confirmed"], 1)
        self.assertEqual(guessed["confirmed"], 0)
        self.assertGreater(float(explicit["confidence"]), float(guessed["confidence"]))
        hits = self.memory.search("咖啡")
        self.assertEqual(hits[0]["content"], explicit["content"], "明说的排前面")

    def test_a_told_correction_replaces_the_old_statement(self):
        self.memory.remember("用户喜欢喝咖啡", source="USER_EXPLICIT")
        self.memory.remember("用户不喜欢喝咖啡了", source="USER_EXPLICIT")
        live = [row["content"] for row in self.memory.browse(kind="long_term")]
        self.assertIn("用户不喜欢喝咖啡了", live)
        self.assertNotIn("用户喜欢喝咖啡", live, "被取代的不再参与检索")

    def test_a_weak_overlap_is_kept_instead_of_guessed_away(self):
        """两句重合不多时不许猜：宁可都留着，也不删掉用户说过的话。"""
        self.memory.remember("用户喜欢喝美式咖啡", source="USER_EXPLICIT")
        self.memory.remember("用户最近改喝拿铁了", source="USER_EXPLICIT")
        live = [row["content"] for row in self.memory.browse(kind="long_term")]
        self.assertEqual(len(live), 2, "认不准是不是同一件事就都留着")

    def test_a_single_mention_stays_a_candidate(self):
        learner = HabitLearner(preferences=self.memory)
        learner.observe("以后回答短一点")
        rows = learner.rows()
        self.assertTrue(rows)
        self.assertEqual(rows[0]["status"], "candidate", "说一次只是候选")
        self.assertFalse(rows[0]["auto"])
        self.assertEqual(learner.observed(), [], "还没采纳就不能算数")

    def test_repeating_it_three_times_gets_it_adopted(self):
        learner = HabitLearner(preferences=self.memory)
        for _ in range(3):
            learner.observe("以后回答短一点")
        rows = learner.rows()
        self.assertEqual(rows[0]["seen"], 3)
        self.assertEqual(rows[0]["status"], "accepted")
        self.assertEqual(learner.observed()[0]["value"], "简短")

    def test_this_time_only_never_becomes_a_habit(self):
        learner = HabitLearner(preferences=self.memory)
        for _ in range(5):
            learner.observe("这次回答短一点")
        rows = learner.rows()
        self.assertTrue(rows)
        for row in rows:
            self.assertTrue(row.get("temporary"), "带「这次」的要说成仅限这一次")
            self.assertEqual(row["status"], "candidate", "说多少遍都还是候选")
            self.assertEqual(learner.scope(row["id"]), "仅限当前这一次")
        self.assertEqual(learner.observed(), [], "临时要求不许自动生效")

    def test_saying_it_later_as_a_rule_turns_it_permanent(self):
        learner = HabitLearner(preferences=self.memory)
        learner.observe("这次回答短一点")
        self.assertTrue(learner.rows()[0].get("temporary"))
        for _ in range(3):
            learner.observe("以后回答短一点")
        row = learner.rows()[0]
        self.assertFalse(row.get("temporary"), "不带「这次」就是立规矩了")
        self.assertEqual(row["status"], "accepted")

    def test_address_and_no_go_areas_wait_for_a_nod(self):
        learner = HabitLearner(preferences=self.memory)
        for _ in range(5):
            learner.observe("以后别叫我老板")
        waiting = [row for row in learner.rows() if row["field"] in ASK_FIELDS]
        self.assertTrue(waiting, "称呼和雷区应该被记下来等确认")
        for row in waiting:
            self.assertNotEqual(row["status"], "accepted", "没点头之前不许生效")

    def test_every_candidate_can_be_rejected_and_stays_out(self):
        learner = HabitLearner(preferences=self.memory)
        learner.observe("以后回答短一点")
        identifier = learner.rows()[0]["id"]
        learner.reject(identifier) if hasattr(learner, "reject") else learner.set_status(identifier, "rejected")
        before = len(learner.rows())
        for _ in range(3):
            learner.observe("以后回答短一点")
        self.assertEqual(len(learner.rows()), before, "拒绝过的建议不该再长出来")
        self.assertEqual(
            [row for row in learner.rows() if row["status"] == "accepted"], []
        )


if __name__ == "__main__":
    unittest.main()
