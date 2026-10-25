"""P47：Agent 审计与意图理解（V3 阶段 A / B）。

这一层要保证的事：
    路由给出结构化结论，而不是只有四个大类；
    「告诉我怎么做」和「替我动手」永远不是同一类；
    取消、确认、补充信息优先于意图判断；
    本地能力能接的活先走本地，用不着模型；
    目标说不清就先问，不猜着动手；
    旧接口 classify / is_stop / is_confirm 一个字都没变。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.router import (
    Intent,
    IntentKind,
    IntentRouter,
    RouteDecision,
    classify,
    is_confirm,
    is_stop,
)


class StructuredRouteTests(unittest.TestCase):
    def setUp(self):
        self.router = IntentRouter()

    def test_decision_carries_the_whole_contract(self):
        data = self.router.decide("帮我改 main.py").to_dict()
        for key in (
            "intent",
            "confidence",
            "target",
            "requested_action",
            "requires_tools",
            "requires_confirmation",
            "missing_information",
            "session_relation",
        ):
            self.assertIn(key, data)

    def test_analysis_is_not_the_same_class_as_editing(self):
        analysis = self.router.decide("帮我分析代码")
        editing = self.router.decide("帮我修改项目")
        self.assertEqual(analysis.intent, IntentKind.CONSULT.value)
        self.assertEqual(editing.intent, IntentKind.TASK.value)
        self.assertNotEqual(analysis.intent, editing.intent)
        self.assertFalse(analysis.requires_tools)
        self.assertTrue(editing.requires_tools)
        self.assertTrue(editing.requires_confirmation)

    def test_how_to_questions_do_not_become_tasks(self):
        for text in ("这个文件应该怎么改", "解释一下这段代码", "你能帮我改代码吗？"):
            self.assertNotEqual(self.router.decide(text).intent, IntentKind.TASK.value, text)

    def test_path_is_picked_up_as_the_target(self):
        decision = self.router.decide("帮我改 main.py")
        self.assertEqual(decision.target, "main.py")

    def test_cancel_beats_everything_else(self):
        decision = self.router.decide("算了，帮我修改项目")
        self.assertEqual(decision.intent, IntentKind.CANCEL.value)
        self.assertGreaterEqual(decision.confidence, 0.9)

    def test_confirm_only_makes_sense_while_waiting(self):
        waiting = self.router.decide("开始", awaiting="confirm")
        idle = self.router.decide("开始")
        self.assertEqual(waiting.intent, IntentKind.CONFIRM.value)
        self.assertEqual(waiting.session_relation, "continue")
        self.assertNotEqual(idle.intent, IntentKind.CONFIRM.value)

    def test_waiting_for_user_answer_is_supplement(self):
        decision = self.router.decide("保存在 D 盘", awaiting="user")
        self.assertEqual(decision.intent, IntentKind.SUPPLEMENT.value)
        self.assertEqual(decision.session_relation, "continue")

    def test_local_capability_wins_before_any_model_call(self):
        decision = self.router.decide("现在几点", capability=True)
        self.assertEqual(decision.intent, IntentKind.LOCAL.value)
        self.assertTrue(decision.requires_tools)

    def test_local_probe_is_called_lazily(self):
        calls = []

        def probe():
            calls.append(1)
            return True

        self.router.decide("现在几点", capability=probe)
        self.assertEqual(calls, [1])

    def test_vague_task_asks_instead_of_guessing(self):
        decision = self.router.decide("帮我弄一下那个东西")
        self.assertEqual(decision.intent, IntentKind.TASK.value)
        self.assertTrue(decision.missing_information)
        self.assertLess(decision.confidence, 0.5)

    def test_clear_task_does_not_ask(self):
        decision = self.router.decide("帮我改 main.py")
        self.assertEqual(decision.missing_information, "")
        self.assertGreaterEqual(decision.confidence, 0.5)

    def test_info_requests_use_tools_without_confirmation(self):
        decision = self.router.decide("今天天气怎么样")
        self.assertEqual(decision.intent, IntentKind.INFO.value)
        self.assertTrue(decision.requires_tools)
        self.assertFalse(decision.requires_confirmation)

    def test_memory_request_is_recognised(self):
        self.assertEqual(
            self.router.decide("记住我叫小明").intent, IntentKind.MEMORY.value
        )

    def test_empty_input_is_unknown_not_a_task(self):
        decision = self.router.decide("   ")
        self.assertEqual(decision.intent, IntentKind.UNKNOWN.value)
        self.assertFalse(decision.requires_tools)

    def test_label_is_human_readable(self):
        self.assertEqual(self.router.decide("帮我修改项目").label, "任务")


class LegacyRouteStaysIntactTests(unittest.TestCase):
    """老接口是别处依赖的公共契约，一个都不能变。"""

    def setUp(self):
        self.router = IntentRouter()

    def test_classify_still_returns_the_old_enum(self):
        cases = {
            "今天天气怎么样": Intent.SAFE_TOOL,
            "记住我喜欢喝咖啡": Intent.MEMORY,
            "帮我改一下这个文件": Intent.TASK,
            "你好呀": Intent.CHAT,
        }
        for text, expected in cases.items():
            self.assertEqual(classify(text), expected, text)

    def test_stop_and_confirm_helpers_unchanged(self):
        self.assertTrue(is_stop("算了"))
        self.assertTrue(is_confirm("ok"))
        self.assertFalse(is_stop("继续做"))

    def test_decision_keeps_the_legacy_intent_for_traceability(self):
        decision = self.router.decide("帮我修改项目")
        self.assertEqual(decision.legacy, Intent.TASK.value)

    def test_route_decision_defaults_are_safe(self):
        decision = RouteDecision()
        self.assertFalse(decision.requires_tools)
        self.assertFalse(decision.requires_confirmation)
        self.assertEqual(decision.to_dict()["intent_label"], "闲聊")
