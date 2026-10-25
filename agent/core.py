"""Agent Core：串联 Context、Memory、ModelProvider。

不执行工具、不判断权限、不碰数据库，那些由程序层在后续阶段接入。
"""

import json
import re
import time
from dataclasses import dataclass, field

from agent import safe_tools
from behavior.manager import INTERACT as BEHAVIOR_INTERACT
from agent.capabilities import CapabilityRouter
from agent.capabilities import LocalCapabilityRegistry
from agent.diagnostics import Diagnostics
from agent.router import Intent, IntentKind, IntentRouter, Mode
from agent.task import (
    ERROR_OTHER,
    TERMINAL_STATES,
    TaskLimits,
    TaskRecord,
    TaskResult,
    TaskState,
    TaskStep,
    classify_error,
    error_label,
    now_stamp,
    retryable,
    state_for_error,
)
from agent.task_store import TaskStore
from events import bus as bus_module
from model.base import ChatResult, ModelRole, ModelSlot, Usage
from model.router import ModelRouter
from model.logged import LoggedProvider
from persona import feedback as persona_feedback
from persona.interface import build_persona_note
from persona.policy import PersonaPolicy
from persona.state import EmotionState
from tools.permission import SAFE_LEVELS
from tools.registry import ASK_USER_TOOL, ToolOutcome

DEFAULT_SYSTEM_PROMPT = (
    "你是运行在用户电脑上的桌面陪伴式桌宠。"
    "用简短自然的中文回答，不要输出思考过程。"
    "不确定就直说不确定，不要编造工具结果或系统状态。"
    "关于用户的身份、偏好、过往，只依据「已知记忆」里的内容；"
    "记忆里没有就直说没记住，不要猜，也不要编。"
    "需要改文件、跑命令这类动手的事，提醒用户说「帮我改…」进入任务模式。"
)

TASK_SYSTEM_PROMPT = (
    "你正在执行用户明确要求的电脑任务。"
    "你只能通过工具完成操作，不能假装做过任何事。"
    "只使用本次提供的工具；没有合适工具时直接说明你做不到，并说明缺什么。"
    "不确定就说不确定；失败就如实说明失败原因。"
    "最后用简短中文说明做了什么、是否需要用户继续操作。"
)

# ponytail: 任务最多跑几轮工具。需要更长的自主循环时再调这个数字。
MAX_TASK_ROUNDS = 3
# V3：单轮任务的上限。到顶就停下来问用户，绝不无限循环。
TASK_LIMITS = TaskLimits(max_rounds=MAX_TASK_ROUNDS)
TASK_SUMMARY_CHARS = 400

LOCAL_GREETING = "我在。现在没有连接聊天模型，我只能做基础本地互动，比如报时间和记事情。"
NO_COMPANION_REPLY = "当前没有连接聊天模型，我暂时只能进行基础本地互动。"

# 会带出屏幕内容的工具。交给云端模型之前要用户单独授权，规则由装配那一层注入。
SCREEN_TOOLS = ("screen_context",)
NO_TASK_REPLY = "任务执行模型尚未配置，因此我目前不能执行这个任务。"
TASK_CONFIRM_PROMPT = (
    "这件事需要进入任务模式，用任务模型配合工具来做。要现在开始吗？"
    "（回复「开始」继续，回复「算了」取消）"
)

# 用户明确说「记住」时，按内容归到关系层，方便以后人格和主动行为使用。
_RELATION_WORDS = ("称呼", "叫我", "喊我", "关系", "陪伴", "陪我")

# 人格反馈的回应：只说明记下了，不假装改了性格。
FEEDBACK_REPLIES = {
    "like": "好，那我以后就这么说。",
    "keep": "好，以后就这样。",
    "dislike": "好，我记下了，以后注意。",
    "stop": "好，我以后不这样了。",
}



def _merge_usage(first, second):
    return Usage(
        prompt_tokens=first.prompt_tokens + second.prompt_tokens,
        completion_tokens=first.completion_tokens + second.completion_tokens,
    )


@dataclass
class AgentReply:
    ok: bool
    content: str = ""
    error: str = ""
    model: str = ""
    usage: Usage = field(default_factory=Usage)
    debug: dict = field(default_factory=dict)


class AgentCore:
    def __init__(
        self,
        provider,
        memory,
        context,
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        logger=None,
        persona=None,
        avatar=None,
        emotion=None,
        executor=None,
        bus=None,
        devices=None,
        task_provider=None,
        companion_slot=None,
        task_slot=None,
        router=None,
        task_confirm=True,
        config=None,
        capabilities=None,
        training=None,
        task_store=None,
        diagnostics=None,
        limits=None,
        skills=None,
        screen_policy=None,
        policy=None,
        model_call_log=None,
    ):
        self.provider = provider
        self.memory = memory
        self.context = context
        self.system_prompt = system_prompt
        self.logger = logger
        self.persona = persona
        # Local persona policy: context mode and explicit preferences affect
        # behavior before any model is called.
        self.policy = policy or PersonaPolicy(memory)
        self.model_call_log = model_call_log
        # V6：习惯学习。只从用户明说的话里挑候选偏好，没接入就是 None。
        self.learning = None
        self.training = training  # 人格训练端口：只收集样本，不训练
        self.avatar = avatar
        self.emotion = emotion or EmotionState()
        self.executor = executor
        self.bus = bus
        self.devices = devices
        # 陪伴与任务的模型选择集中在 ModelRouter 里，Agent Core 只按角色取用。
        self.models = ModelRouter(
            companion=companion_slot
            or ModelSlot(
                ModelRole.COMPANION.value, provider, configured=provider is not None
            ),
            task=task_slot
            or ModelSlot(
                ModelRole.TASK.value, task_provider, configured=task_provider is not None
            ),
        )
        # 按配置 id 建好的 Provider 表 + 会话级覆盖：切模型只影响当前会话。
        self.model_catalog = {}
        self._session_models = {}
        self.router = router or IntentRouter()
        self.task_confirm = bool(task_confirm)
        self.config = config
        # 屏幕类工具能不能交给模型：装配那一层注入的规则，没注入就照旧。
        self.screen_policy = screen_policy
        self.scheduler = None
        self.behavior = None  # 行为层：待机与互动的唯一决策者
        # 本地能力层：能不用模型就不用模型。顺序见 agent/capabilities.py。
        self.capabilities = capabilities or LocalCapabilityRegistry(
            memory=memory,
            config=config,
            status=self.local_status,
            logger=logger,
            persona=self.persona,
            # V11：本地也能回答设备状态，不需要模型。
            devices=devices,
            policy=self.policy,
        )
        if getattr(self.capabilities, "policy", None) is None:
            self.capabilities.policy = self.policy
        self.mode = Mode.COMPANION if self.companion_slot.available() else Mode.LOCAL
        self.last_intent = Intent.CHAT.value
        self.task_state = TaskState.NEW.value
        self.last_task_result = None
        self._pending_request = ""
        self._cancel_requested = False
        self._pending_question = ""
        self._task_messages = []
        self._task_request = ""
        self._task_session = ""
        self._task_debug = {}
        self._task_usage = Usage()
        self.last_debug = {}
        # V3：任务落库、本轮诊断、任务上限。都可以不接，不接时功能降级但不报错。
        self.task_store = task_store
        # V4：Skill 系统。没接上时行为与以前完全一致。
        self.skills = skills
        self.last_skill_candidates = []
        self.last_skill = None
        self.diagnostics = diagnostics or Diagnostics()
        self.limits = limits or TASK_LIMITS
        self.last_route = None
        self.task_record = None
        self._task_started_at = 0.0
        self._task_tool_calls = 0
        self._task_steps = []

    # ---------- 模式与模型状态（只读视图，供程序与界面使用） ----------
    @property
    def mode_name(self):
        return self.mode.value if isinstance(self.mode, Mode) else str(self.mode)

    @property
    def companion_slot(self):
        return self.models.companion

    @property
    def task_slot(self):
        return self.models.task

    # ---------- 多配置与会话级模型 ----------
    def set_model_catalog(self, catalog):
        """登记可以切换的 Provider。只收已经装配好的对象，不接受名字。"""
        self.model_catalog = {
            key: LoggedProvider.wrap(provider, self.model_call_log, "companion")
            for key, provider in dict(catalog or {}).items()
        }
        return len(self.model_catalog)

    def set_session_model(self, session_id, config_id):
        """给某个会话指定模型配置；传空串表示跟随默认。"""
        sid = str(session_id or "").strip()
        if not sid:
            return False
        wanted = str(config_id or "").strip()
        if wanted and wanted not in self.model_catalog:
            return False
        if wanted:
            self._session_models[sid] = wanted
        else:
            self._session_models.pop(sid, None)
        return True

    def session_model(self, session_id):
        return self._session_models.get(str(session_id or "").strip(), "")

    def provider_for(self, session_id=""):
        """当前会话实际要用的陪伴 Provider：会话覆盖优先，否则用默认槽位。"""
        config_id = self.session_model(session_id)
        if config_id:
            provider = self.model_catalog.get(config_id)
            if provider is not None:
                return provider
        return self.companion_slot.provider

    def available_for(self, session_id=""):
        provider = self.provider_for(session_id)
        if provider is None:
            return False
        try:
            return bool(provider.is_available())
        except Exception:
            return False

    @property
    def companion_provider(self):
        return self.companion_slot.provider

    @property
    def task_provider(self):
        return self.task_slot.provider

    def model_status(self):
        return {
            "companion": self.companion_slot.to_dict(),
            "task": self.task_slot.to_dict(),
            "mode": self.mode_name,
            "intent": self.last_intent,
            "task_state": self.task_state,
        }

    def respond(self, session_id, user_input, persona_note="", on_delta=None):
        """对外唯一入口。所有分支都过一遍诊断记录，界面只读结论。

        给了 on_delta 就尽量流式：模型支持就边生成边交给界面，
        不支持或中途出错就退回原来的整段调用，功能不受影响。
        """
        reply = self._respond(session_id, user_input, persona_note, on_delta)
        self._record_turn(reply)
        return reply

    def _respond(self, session_id, user_input, persona_note="", on_delta=None):
        user_input = (user_input or "").strip()
        if not user_input:
            return AgentReply(ok=False, error="empty input")

        self._note_activity()
        self._note_relationship()

        # 任务进行中：用户说停就停。程序负责取消，模型不能继续。
        if self.mode == Mode.TASK and self.router.is_stop(user_input):
            self.cancel_task()
            return AgentReply(ok=True, content="好，我停下了。", debug=self._mode_debug())

        # 任务在等用户补信息：把这一轮回答接回任务继续跑。
        if self.task_state == TaskState.WAITING_USER.value:
            return self._resume_task(user_input)

        # 等待确认：这一轮只处理「开始 / 取消」。
        if self.mode == Mode.TASK_PENDING:
            if self.router.is_stop(user_input):
                self._pending_request = ""
                self._finish_task(TaskState.CANCELLED.value)
                return AgentReply(ok=True, content="好，那先不做。", debug=self._mode_debug())
            if self.router.is_confirm(user_input):
                request = self._pending_request
                self._pending_request = ""
                return self._run_task(session_id, request)
            # 既不是确认也不是取消：当成新输入，退出待确认状态
            self._pending_request = ""
            self._set_mode(Mode.COMPANION if self.companion_slot.available() else Mode.LOCAL)

        route = self.router.decide(user_input)
        self.last_route = route
        intent = self.router.classify(user_input)
        self.last_intent = intent.value

        if intent == Intent.TASK:
            if route.missing_information:
                # 目标说不清就先问，别猜着动手。
                self._pending_request = ""
                self._set_mode(
                    Mode.COMPANION if self.companion_slot.available() else Mode.LOCAL
                )
                return AgentReply(
                    ok=True,
                    content="你是想让我对哪个文件或项目动手？说清楚我再开始。",
                    debug={
                        **self._mode_debug(intent.value),
                        "needs_clarification": True,
                    },
                )
            return self._handle_task_intent(session_id, user_input)
        if intent == Intent.MEMORY:
            self._remember_long_term(user_input)
        else:
            # 用户当面评价桌宠的说话方式：记下来，并留一条人格训练样本。
            feedback_kind = persona_feedback.classify(user_input)
            if feedback_kind:
                self._store_feedback(user_input)
                if not self.companion_slot.available():
                    return AgentReply(
                        ok=True,
                        content=FEEDBACK_REPLIES.get(feedback_kind, "好，我记下了。"),
                        debug={
                            **self._mode_debug(intent.value),
                            "persona_feedback": feedback_kind,
                            "local": True,
                        },
                    )

        # V6：从用户明说的话里挑候选习惯。只写偏好记录，不碰权限，不记整段原文。
        if self.learning is not None:
            try:
                self.learning.observe(user_input)
            except Exception as exc:
                if self.logger:
                    self.logger.warning("preference learning failed: %s", exc)

        # V4：用户点名要用某个 Skill 时，直接走 Skill 执行路径。
        if self.skills is not None:
            called = self.skills.explicit(user_input)
            if called:
                return self._run_skill(called, user_input)

        # Local First：本机能答的就不调用模型，也不花 token。
        local = self.capabilities.resolve(user_input)
        if local is not None:
            return self._local_answer(local, intent)

        # 安全工具直连：只读、无参数、能自己说人话的工具，程序直接跑。
        if intent == Intent.SAFE_TOOL:
            direct = self._run_safe_tool(session_id, user_input, intent)
            if direct is not None:
                return direct

        return self._companion_reply(
            session_id, user_input, intent, persona_note, on_delta=on_delta
        )

    # ---------- V4：Skill ----------
    def skill_rows(self):
        if self.skills is None:
            return []
        try:
            return self.skills.rows()
        except Exception as exc:
            self._warn("skill rows failed: %s", exc)
            return []

    def skill_stats(self):
        if self.skills is None:
            return {}
        try:
            return self.skills.stats()
        except Exception as exc:
            self._warn("skill stats failed: %s", exc)
            return {}

    def recommend_skills(self, text, intent=""):
        """只给推荐，不动手。低分不返回，界面据此决定要不要提示。"""
        if self.skills is None:
            return []
        try:
            candidates = self.skills.recommend(text, intent=intent)
        except Exception as exc:
            self._warn("skill recommend failed: %s", exc)
            return []
        self.last_skill_candidates = list(candidates)
        return list(candidates)

    def _run_skill(self, name, request, inputs=None):
        """跑一个被点名的 Skill。每个工具调用仍然要单独过权限。"""
        manager = self.skills
        if manager is None:
            return AgentReply(ok=False, error="当前没有接入 Skill 系统")
        self.last_skill = name
        plan = manager.plan(name, request=request, inputs=inputs)
        debug = {
            **self._mode_debug(Intent.TASK.value),
            "skill": name,
            "skill_plan": plan.to_dict(),
        }
        self.last_debug = debug
        result = manager.run(name, request=request, inputs=inputs)
        debug["skill_ok"] = bool(result.ok)
        debug["skill_verified"] = bool(result.verified)
        debug["skill_seconds"] = result.seconds
        self.last_debug = debug
        if result.cancelled:
            self._set_mode(Mode.COMPANION if self.companion_slot.available() else Mode.LOCAL)
            return AgentReply(ok=True, content="好，我停下了。", debug=debug)
        if not result.ok:
            return AgentReply(
                ok=False, error=result.error or "这个 Skill 没跑成", debug=debug
            )
        content = result.summary or "Skill 跑完了。"
        if result.detail:
            content = content + "\n" + str(result.detail)[:900]
        if not result.verified:
            content += "\n（这一步没有通过程序的复核，请自己确认。）"
        self._set_mode(Mode.COMPANION if self.companion_slot.available() else Mode.LOCAL)
        return AgentReply(ok=True, content=content, debug=debug)

    def _begin_task_record(self, session_id, request):
        """开一张任务单。没接持久化时只留在内存里，不影响执行。"""
        record = TaskRecord(
            goal=str(request or ""),
            session=str(session_id or ""),
            project=self._project_name(request),
            state=TaskState.NEW.value,
            created_at=now_stamp(),
        )
        self.task_record = record
        self._task_steps = []
        self.save_task_record()
        return record

    @staticmethod
    def _project_name(request):
        """从话里挑一个像路径的片段当项目名。挑不出就留空。"""
        match = re.search(r"[\w\-./\\]+\.(py|js|ts|tsx|json|md|txt|html|css|sql)", str(request or ""))
        return match.group(0) if match else ""

    def _out_of_budget(self):
        """到顶就停下问用户。返回空串表示还能继续。"""
        limits = self.limits
        if self._task_started_at and getattr(limits, "max_seconds", 0):
            if time.monotonic() - self._task_started_at > float(limits.max_seconds):
                return "这件事比预计的长"
        if getattr(limits, "max_tool_calls", 0):
            if self._task_tool_calls >= int(limits.max_tool_calls):
                return "工具调用次数到上限了"
        return ""

    def _record_step(self, outcome, index=None):
        """把一次工具执行记成任务步骤。真实结果是什么就记什么。"""
        if not isinstance(outcome, dict):
            outcome = outcome.to_dict() if hasattr(outcome, "to_dict") else {}
        step = TaskStep(
            index=len(self._task_steps) if index is None else int(index),
            title=str(outcome.get("name") or "工具"),
            tool=str(outcome.get("name") or ""),
            status="done" if outcome.get("ok") else "failed",
            result=str(outcome.get("checks") or "")[:200],
            error=str(outcome.get("error") or "")[:200],
            verify="已校验" if outcome.get("verified") else "未校验",
        )
        step.attempts = 1
        self._task_steps.append(step)
        return step

    # ---------- 任务模式：路由 → 确认 → 权限 → 执行 ----------
    def _handle_task_intent(self, session_id, request):
        if not self.task_slot.available():
            self._warn("task model unavailable, refusing to fake task execution")
            self._set_mode(Mode.COMPANION if self.companion_slot.available() else Mode.LOCAL)
            return AgentReply(
                ok=False,
                error=NO_TASK_REPLY,
                debug={**self._mode_debug(Intent.TASK.value), "error_type": "TASK_MODEL_UNAVAILABLE"},
            )
        self._begin_task_record(session_id, request)
        if not self.task_confirm:
            return self._run_task(session_id, request)
        self._pending_request = request
        self.task_state = TaskState.PENDING_CONFIRMATION.value
        if self.task_record is not None:
            self.task_record.pending_confirmation = TASK_CONFIRM_PROMPT
        self._set_mode(Mode.TASK_PENDING)
        self.save_task_record()
        return AgentReply(ok=True, content=TASK_CONFIRM_PROMPT, debug=self._mode_debug(Intent.TASK.value))

    def _run_task(self, session_id, request):
        if not request:
            self._finish_task(TaskState.CANCELLED.value)
            return AgentReply(ok=False, error="没有待执行的任务", debug=self._mode_debug())
        provider = self.task_slot.provider
        self._cancel_requested = False
        self.task_state = TaskState.PLANNING.value
        self._set_mode(Mode.TASK)
        # V3：单轮预算。任务跑再久也不会超出这几个数。
        self._task_started_at = time.monotonic()
        self._task_tool_calls = 0
        if self.task_record is None or self.task_record.goal != request:
            self._begin_task_record(session_id, request)
        self.task_state = TaskState.RUNNING.value
        task_debug = {
            **self._mode_debug(Intent.TASK.value),
            "tools_offered": [],
            "tool_calls": [],
            "tool_results": [],
            "task_rounds": 0,
            "memory_count": 0,
        }
        self.last_debug = task_debug
        self._task_request = request
        self._task_session = session_id
        self._task_debug = task_debug
        self._pending_question = ""

        # 只带与任务相关的记忆：不带闲聊历史，也不把整库塞给任务模型。
        _, memory_block, _ = self._recall(session_id, request)
        task_debug["memory_count"] = self._recall_stats().get("memory_count", 0)
        bundle = self.context.build(
            system_prompt=TASK_SYSTEM_PROMPT,
            recent_messages=[],
            memory_block=memory_block,
            summary="",
            persona_note=self._persona_note(),
            current_input=request,
        )
        tool_names = self._task_tool_names(request)
        task_debug["tools_offered"] = tool_names
        self._task_messages = list(bundle.messages)
        result, kind, attempts = self._call_task_model(bundle.messages, tool_names)
        task_debug["model_attempts"] = attempts
        if result is None or not result.ok:
            detail = getattr(result, "error", "") or "任务模型没有给出结果"
            self._warn("task model call failed (%s, %d 次): %s", kind or ERROR_OTHER, attempts, detail)
            self._finish_task(state_for_error(kind))
            task_debug["model_error_kind"] = kind or ERROR_OTHER
            return AgentReply(
                ok=False, error="%s：%s" % (error_label(kind), detail), debug=task_debug
            )

        self._record_usage(session_id, result)
        self._task_usage = result.usage
        return self._task_loop(result, rounds=0)

    def _call_task_model(self, messages, tool_names):
        """叫任务模型。可恢复的错（超时、限流、断网）按预算重试，别的直接报。

        返回 (结果, 错误类别, 试了几次)。连异常都抛的时候结果是 None。
        不无限重试：次数由 TaskLimits.max_retries 定。
        """
        limit = 1 + max(0, int(getattr(self.limits, "max_retries", 0) or 0))
        provider = self.task_slot.provider
        schemas = self._tool_schemas(tool_names)
        kind = ""
        last = None
        for attempt in range(1, limit + 1):
            self._life("note_thinking", "在想这个任务怎么做")
            try:
                last = provider.chat(messages, tools=schemas)
            except Exception as exc:
                kind = classify_error(str(exc))
                last = None
            else:
                if last.ok:
                    return last, "", attempt
                kind = classify_error(getattr(last, "error", ""))
            if not retryable(kind) or attempt >= limit:
                self._life("note_error", "任务模型没答上来")
                return last, kind, attempt
            self._warn("task model %s, retrying (%d/%d)", kind, attempt, limit)
        return last, kind, limit

    def _task_tool_names(self, request):
        """任务模式始终带上 ask_user，其余只加载相关工具。"""
        names = list(self._select_tools(request))
        if self.executor and self.executor.registry.get(ASK_USER_TOOL):
            names.append(ASK_USER_TOOL)
        return names

    def _task_loop(self, result, rounds):
        """任务工具循环。中途需要用户补信息就暂停，等下一轮回答再继续。"""
        provider = self.task_slot.provider
        task_debug = self._task_debug
        max_rounds = max(1, int(getattr(self.limits, "max_rounds", MAX_TASK_ROUNDS) or 1))
        while result.tool_calls and self.executor and rounds < max_rounds:
            if self._cancel_requested:
                break
            budget = self._out_of_budget()
            if budget:
                # 到顶了：把真实原因说出来，剩下的交给用户决定。
                self._task_debug["limit_hit"] = budget
                self._pending_question = "%s，我先停在这里。要我接着做吗？" % budget
                self.save_task_record()
                return self._pause_for_user(rounds)
            rounds += 1
            self.task_state = TaskState.WAITING_TOOL.value
            self.save_task_record()
            result, usage = self._run_tools(
                self._task_session,
                self._task_messages,
                result,
                self._task_usage,
                task_debug,
                provider=provider,
            )
            self._task_usage = usage
            if self._pending_question:
                self.task_state = TaskState.WAITING_USER.value
                return self._pause_for_user(rounds)
            if not result.ok:
                break
        return self._finish_task_round(result, rounds)

    def _pause_for_user(self, rounds):
        task_debug = self._task_debug
        task_debug["task_rounds"] = rounds
        task_debug["task_state"] = self.task_state
        task_debug["prompt_tokens"] = self._task_usage.prompt_tokens
        task_debug["completion_tokens"] = self._task_usage.completion_tokens
        task_debug["total_tokens"] = self._task_usage.total_tokens
        self._set_mode(Mode.TASK)
        return AgentReply(ok=True, content=self._pending_question, debug=task_debug)

    def _resume_task(self, answer):
        """用户回答了任务的问题，把回答接回任务继续执行。"""
        provider = self.task_slot.provider
        self._pending_question = ""
        self.task_state = TaskState.RUNNING.value
        self._set_mode(Mode.TASK)
        if self.task_record is not None:
            self.task_record.pending_question = ""
        self.save_task_record()
        messages = list(self._task_messages or [])
        messages.append({"role": "user", "content": answer})
        self._task_messages = messages
        self.last_debug = self._task_debug
        try:
            result = provider.chat(
                messages, tools=self._tool_schemas(self._task_tool_names(self._task_request))
            )
        except Exception as exc:
            self._warn("task resume raised: %s", exc)
            self._finish_task(TaskState.FAILED.value)
            return AgentReply(ok=False, error=f"任务模型调用异常：{exc}", debug=self._task_debug)
        if not result.ok:
            self._warn("task resume failed: %s", result.error)
            self._finish_task(TaskState.FAILED.value)
            return AgentReply(
                ok=False, error=result.error or "任务模型不可用", debug=self._task_debug
            )
        self._record_usage(self._task_session, result)
        self._task_usage = _merge_usage(self._task_usage, result.usage)
        return self._task_loop(result, rounds=self._task_debug.get("task_rounds", 0))

    def _finish_task_round(self, result, rounds):
        task_debug = self._task_debug
        usage = self._task_usage
        task_debug["task_rounds"] = rounds
        task_debug["prompt_tokens"] = usage.prompt_tokens
        task_debug["completion_tokens"] = usage.completion_tokens
        task_debug["total_tokens"] = usage.total_tokens
        task_debug["model"] = getattr(result, "model", "")

        if self._cancel_requested or self.task_state == TaskState.CANCELLED.value:
            self._finish_task(TaskState.CANCELLED.value)
            task_debug["task_state"] = self.task_state
            return AgentReply(ok=True, content="任务已取消。", debug=task_debug)

        state = TaskState.COMPLETED.value if result.ok else TaskState.FAILED.value
        task_result = TaskResult(
            ok=bool(result.ok),
            request=self._task_request,
            summary=(result.content or "").strip(),
            steps=[
                {"tool": row.get("name"), "ok": row.get("ok"), "allowed": row.get("allowed")}
                for row in task_debug["tool_results"]
            ],
            changes=[],
            needs_user=False,
            error=result.error or "",
            state=state,
        )
        self.last_task_result = task_result
        self._finish_task(state)
        task_debug["task_state"] = self.task_state
        task_debug["task_result"] = task_result.to_dict()

        if not result.ok:
            self._update_emotion("sad")
            return AgentReply(
                ok=False,
                error=f"任务执行失败：{result.error}" if result.error else "任务执行失败",
                debug=task_debug,
            )
        content = self._compose_task_reply(self._task_session, task_result)
        self._update_emotion("happy")
        self._remember(self._task_session, "assistant", content)
        return AgentReply(
            ok=True, content=content, model=result.model, usage=usage, debug=task_debug
        )

    def _compose_task_reply(self, session_id, task_result):
        """任务模型给结构化结果，陪伴模型用自己的语气转述。"""
        summary = (task_result.summary or "").strip() or "任务已经执行完，但任务模型没有给出说明。"
        if not self.companion_slot.available():
            return summary[:TASK_SUMMARY_CHARS]
        prompt = (
            "任务已经执行完了，下面是结果，请用一句话自然地转述给用户，"
            "不要添加没有依据的内容：\n" + summary[:TASK_SUMMARY_CHARS]
        )
        try:
            bundle = self.context.build(
                system_prompt=self.system_prompt,
                recent_messages=[],
                memory_block="",
                summary="",
                persona_note=self._persona_note(),
                current_input=prompt,
            )
            result = self.companion_slot.provider.chat(bundle.messages)
        except Exception as exc:
            self._warn("companion summary of task failed: %s", exc)
            return summary[:TASK_SUMMARY_CHARS]
        if not result.ok or not (result.content or "").strip():
            return summary[:TASK_SUMMARY_CHARS]
        self._record_usage(session_id, result)
        return result.content.strip()

    # ---------- 安全工具直连 ----------
    def _run_safe_tool(self, session_id, user_input, intent):
        """程序直接跑只读工具；跑不了就返回 None，交给后面的路。"""
        if not self.executor:
            return None
        tool = safe_tools.candidate(self.executor.registry, user_input)
        if tool is None:
            return None
        try:
            outcome = self.executor.execute(tool.name, {})
        except Exception as exc:
            self._warn("safe tool failed: %s", exc)
            return None
        text = safe_tools.note(tool, outcome)
        if text is None:
            return None
        self._publish(
            bus_module.TOOL_COMPLETED,
            name=tool.name,
            ok=outcome.ok,
            allowed=outcome.allowed,
            level=outcome.level,
        )
        tool_debug = {
            "safe_tool": tool.name,
            "safe_tool_outcome": outcome.to_dict(),
        }

        if not self.companion_slot.available():
            # 没有模型：程序自己把真实结果说出来，不花 token。
            self._set_mode(Mode.LOCAL)
            debug = {
                **self._mode_debug(intent.value),
                **tool_debug,
                "tools_offered": [],
                "local": True,
                "emotion": self.emotion.emotion,
            }
            self.last_debug = debug
            return AgentReply(ok=True, content=text, debug=debug)

        reply = self._companion_reply(
            session_id,
            user_input,
            intent,
            tool_note=f"已经用 {tool.name} 查到：{text}",
        )
        if reply.debug is not None:
            # 只补记直连这一步，不覆盖模型自己那轮的 tool_results。
            reply.debug.update(tool_debug)
            self.last_debug = reply.debug
        return reply

    # ---------- 本地能力 ----------
    def _local_answer(self, answer, intent):
        """本地能力直接回答：不调用模型，也不记录 token。"""
        self._set_mode(Mode.LOCAL)
        debug = {
            **self._mode_debug(intent.value),
            "capability": answer.capability,
            "local": True,
            "emotion": self.emotion.emotion,
        }
        self.last_debug = debug
        return AgentReply(ok=True, content=answer.text, debug=debug)

    def attach_behavior(self, behavior):
        """接入行为层。没接上也能聊，只是没有待机动作和打扰门禁。"""
        self.behavior = behavior
        return behavior

    def attach_scheduler(self, scheduler):
        """把调度器接给倒计时/提醒能力。没接上也能启动，只是不能计时。"""
        self.scheduler = scheduler
        self.capabilities.scheduler = scheduler
        return scheduler

    def local_status(self):
        """本地能力的只读状态视图，只给本地能力和开发者面板用，绝不发给模型。"""
        return {
            "mode": self.mode_name,
            "companion_ready": self.companion_slot.available(),
            "task_ready": self.task_slot.available(),
            "companion": self.companion_slot.status(),
            "task": self.task_slot.status(),
            "emotion": self.emotion.emotion,
            "capability": self.capabilities.last,
        }

    def cancel_task(self):
        """用户或程序取消当前任务。取消后不再执行任何工具。"""
        self._cancel_requested = True
        if self.skills is not None:
            try:
                self.skills.cancel()
            except Exception as exc:
                self._warn("skill cancel failed: %s", exc)
        if self.task_state not in TERMINAL_STATES:
            self.task_state = TaskState.CANCELLED.value
        self._pending_request = ""
        self._pending_question = ""
        self._set_mode(Mode.COMPANION if self.companion_slot.available() else Mode.LOCAL)
        return True

    def _finish_task(self, state):
        self.task_state = state
        self._set_mode(Mode.COMPANION if self.companion_slot.available() else Mode.LOCAL)
        record = self.task_record
        if record is not None:
            record.state = state
            record.pending_question = ""
            record.pending_confirmation = ""
            record.attempts = int(record.attempts or 0) + 1
            result = self.last_task_result
            if result is not None and result.error and not record.error:
                record.error = str(result.error)[:200]
            self.save_task_record()
            self._remember_project(record)

    def _remember_project(self, record):
        """把任务结果记成项目记忆。只记真实发生的事，来源标记为程序观察。"""
        if self.memory is None or record is None:
            return None
        label = record.project or record.goal[:40]
        if not label:
            return None
        status = "已完成" if record.state == TaskState.COMPLETED.value else record.state_label
        line = "任务「%s」%s" % (label, status)
        if record.error:
            line += "，原因：%s" % str(record.error)[:60]
        saved = None
        try:
            saved = self.memory.remember(
                line, kind="project", importance=1, source="PROGRAM_OBSERVED"
            )
        except Exception as exc:
            self._warn("project memory write failed: %s", exc)
        # 结构化项目状态：目标、当前进度、已完成、待办、阻塞与最后更新时间。
        # 长期记忆说明发生过什么，这一条说明现在做到哪一步。
        self._update_project_state(record, label, status)
        return saved

    def _update_project_state(self, record, label, status):
        """把这次任务结果并进项目状态。没有项目记忆层就安静跳过。"""
        projects = getattr(self.memory, "projects", None)
        if projects is None:
            return None
        plan = getattr(record, "plan", None) or []
        done = []
        todo = []
        for step in plan:
            if not isinstance(step, dict):
                continue
            title = str(step.get("title") or "").strip()
            if not title:
                continue
            mark = str(step.get("status") or "pending")
            if mark == "done":
                done.append(title)
            elif mark in ("pending", "running"):
                todo.append(title)
        pending = str(getattr(record, "pending_question", "") or "").strip()
        if pending and not todo:
            todo.append(pending)
        finished = record.state == TaskState.COMPLETED.value
        if finished:
            todo = []
        goal = str(getattr(record, "goal", "") or label)
        progress = "第 %d 步" % int(getattr(record, "current_step", 0) or 0) if not finished else "已交付"
        try:
            return projects.update(
                label,
                goal=goal,
                state=status,
                progress=progress,
                done=done,
                todo=todo,
                blockers=[record.error] if record.error else [],
            )
        except Exception as exc:
            self._warn("project state write failed: %s", exc)
            return None

    def _set_mode(self, mode):
        self.mode = mode if isinstance(mode, Mode) else Mode(str(mode))

    def _mode_debug(self, intent=None):
        """只给界面和开发调试看，不发送给模型。"""
        data = {
            "mode": self.mode_name,
            "intent": intent or self.last_intent,
            "companion_status": self.companion_slot.status(),
            "task_status": self.task_slot.status(),
            "task_state": self.task_state,
        }
        if self.last_route is not None:
            data["route"] = self.last_route.to_dict()
        return data

    def _remember_long_term(self, text):
        """用户明确要求记住时写入长期记忆，没有模型也能做。"""
        if persona_feedback.classify(text):
            return self._store_feedback(text)
        kind = "relationship" if any(word in text for word in _RELATION_WORDS) else "long_term"
        try:
            # 记不下去就是没记住，别跟用户说记住了。
            return bool(self.memory.remember(text, kind=kind, importance=2, source="USER_EXPLICIT"))
        except Exception as exc:
            self._warn("long-term memory write failed: %s", exc)
            return False

    def _store_feedback(self, text):
        """人格反馈：写进记忆，同时留一条训练样本。没有模型也能做。"""
        try:
            self.memory.remember(text, kind="feedback", importance=2, source="USER_EXPLICIT")
        except Exception as exc:
            self._warn("persona feedback write failed: %s", exc)
            return False
        self._collect_sample("feedback", text)
        return True

    def _collect_sample(self, kind, payload):
        """只收集样本，不做训练。没接训练端口时直接跳过。"""
        if self.training is None:
            return None
        try:
            return self.training.collect(kind, payload)
        except Exception as exc:
            self._warn("persona sample collect failed: %s", exc)
            return None

    def _local_reply(self, text, intent):
        """没有模型时的本地能力：只报真实状态，绝不假装是 AI。"""
        if intent == Intent.MEMORY:
            return "好，我记住了。"
        if intent == Intent.SAFE_TOOL:
            # 天气/新闻这类要联网的信息，没有模型就不猜，交给调用方如实说明。
            return None
        answer = self.capabilities.resolve(text)
        if answer is not None:
            return answer.text
        lowered = text.lower()
        if any(word in lowered for word in ("你好", "您好", "在吗", "在不在", "hi", "hello")):
            return LOCAL_GREETING
        return None

    def _companion_reply(
        self, session_id, user_input, intent, persona_note="", tool_note="", on_delta=None
    ):
        self._remember(session_id, "user", user_input)
        self._publish(bus_module.USER_MESSAGE, session_id=session_id, text=user_input)

        if not self.available_for(session_id):
            self._set_mode(Mode.LOCAL)
            local = self._local_reply(user_input, intent)
            error_type = {"error_type": "COMPANION_MODEL_UNAVAILABLE"}
            if local is not None:
                return AgentReply(
                    ok=True, content=local, debug={**self._mode_debug(intent.value), **error_type}
                )
            self._update_emotion("sad")
            return AgentReply(
                ok=False, error=NO_COMPANION_REPLY, debug={**self._mode_debug(intent.value), **error_type}
            )

        self._set_mode(Mode.COMPANION)
        persona_note = persona_note or self._persona_note()
        self._update_emotion("thinking")
        recent, memory_block, summary = self._recall(session_id, user_input)
        bundle = self.context.build(
            system_prompt=self.system_prompt,
            recent_messages=recent,
            memory_block=memory_block,
            summary=summary,
            persona_note=persona_note,
            current_input=user_input,
            extra_block=tool_note,
        )

        # 工具已经跑过了，不再把 schema 发出去，免得重复执行。
        tool_names = [] if tool_note else self._select_tools(user_input, safe_only=True)
        tool_debug = {
            **self._mode_debug(intent.value),
            "tools_offered": tool_names,
            "tool_calls": [],
            "tool_results": [],
        }
        # 这一轮不牵扯工具时才能流式：工具流程要保持原样，不能漏掉工具请求。
        if on_delta is not None and not tool_names:
            streamed = self._stream_reply(session_id, bundle, tool_debug, on_delta, intent)
            if streamed is not None:
                return streamed

        self._life("note_thinking", "在想怎么回")
        try:
            result = self.provider_for(session_id).chat(
                bundle.messages, tools=self._tool_schemas(tool_names)
            )
        except Exception as exc:
            self._update_emotion("sad")
            self._warn("model call raised: %s", exc)
            self._life("note_error", "模型调用异常")
            return AgentReply(ok=False, error=f"模型调用异常：{exc}", debug=bundle.stats)

        if not result.ok:
            self._update_emotion("sad")
            self._warn("model call failed: %s", result.error)
            self._life("note_error", "模型没有回答")
            return AgentReply(
                ok=False,
                error=result.error or "model call failed",
                model=result.model,
                debug=bundle.stats,
            )

        self._record_usage(session_id, result)
        usage = result.usage
        if result.tool_calls and self.executor:
            result, usage = self._run_tools(
                session_id,
                bundle.messages,
                result,
                usage,
                tool_debug,
                allowed=set(tool_names),
            )
            if not result.ok:
                self._update_emotion("sad")
                return AgentReply(ok=False, error=result.error, model=result.model, debug=tool_debug)

        self._update_emotion("happy")
        self._remember(session_id, "assistant", result.content)

        debug = {
            **bundle.stats,
            **self._mode_debug(intent.value),
            "model": result.model,
            "emotion": self.emotion.emotion,
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
            **self._recall_stats(),
            **tool_debug,
            "devices": self._device_summary(),
        }
        self.last_debug = debug
        self._life("note_talking", "把话说出来")
        return AgentReply(
            ok=True,
            content=result.content,
            model=result.model,
            usage=usage,
            debug=debug,
        )

    # ---------- 流式与停止 ----------
    def _stream_reply(self, session_id, bundle, tool_debug, on_delta, intent):
        """边生成边交给界面。模型不支持、要调工具或中途出错都返回 None 走整段调用。"""
        provider = self.provider_for(session_id)
        stream = getattr(provider, "stream", None)
        if not callable(stream):
            return None
        self._cancel_requested = False
        parts = []
        usage = None
        try:
            for chunk in stream(bundle.messages, tools=None):
                if self._cancel_requested:
                    return self._cancelled_reply(parts, bundle, tool_debug)
                if getattr(chunk, "tool_calls", None):
                    # 流里夹带工具请求：这一轮不适合流式，交回整段调用处理。
                    return None
                text = getattr(chunk, "content", "") or ""
                if text:
                    parts.append(text)
                    self._emit_delta(on_delta, text)
                measured = getattr(chunk, "usage", None)
                if measured is not None and (
                    getattr(measured, "prompt_tokens", 0) or getattr(measured, "completion_tokens", 0)
                ):
                    usage = measured
        except Exception as exc:
            self._warn("model stream failed: %s", exc)
            if parts:
                # 已经说出去了半句：把它当这一轮的回复收尾，别让界面空着，也不重发。
                return self._finish_stream(session_id, "".join(parts), usage, bundle, tool_debug, intent, partial=True)
            return None
        content = "".join(parts)
        if not content.strip():
            return None
        return self._finish_stream(session_id, content, usage, bundle, tool_debug, intent)

    def _finish_stream(self, session_id, content, usage, bundle, tool_debug, intent, partial=False):
        """流式收尾：写记忆、更新情绪、组装和整段调用同样的诊断信息。"""
        self._update_emotion("happy")
        self._remember(session_id, "assistant", content)
        if usage is None:
            usage = Usage()
        try:
            self.memory.record_usage(
                session_id, self.companion_slot.model or "stream", usage
            )
        except Exception as exc:
            self._warn("usage record failed: %s", exc)
        debug = {
            **bundle.stats,
            **self._mode_debug(intent.value),
            "emotion": self.emotion.emotion,
            "streamed": True,
            "partial": bool(partial),
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
            **self._recall_stats(),
            **tool_debug,
            "devices": self._device_summary(),
        }
        self.last_debug = debug
        self._life("note_talking", "边说边显示")
        return AgentReply(ok=True, content=content, usage=usage, debug=debug)

    def _cancelled_reply(self, parts, bundle, tool_debug):
        """流到一半被叫停：如实记下已经说出去的部分，标成已取消。"""
        said = "".join(parts)
        debug = {
            **bundle.stats,
            "cancelled": True,
            "streamed": True,
            "partial": bool(said),
            **tool_debug,
        }
        self.last_debug = debug
        return AgentReply(ok=False, error="已取消", content=said, debug=debug)

    def _emit_delta(self, on_delta, text):
        try:
            on_delta(text)
        except Exception as exc:
            # 界面回调坏了不该拖垮这一轮回答，后面的照常收尾。
            self._warn("delta callback failed: %s", exc)

    def cancel_generation(self):
        """用户点了停止：这一轮不再往下吐字。已经说出去的字不改。"""
        self._cancel_requested = True
        return True

    def generation_cancelled(self):
        return bool(self._cancel_requested)

    # ---------- 工具：模型只能请求，执行由程序完成 ----------
    def respond_to_event(self, event, session_id="proactive"):
        """主动行为入口：只在事件真正触发时被调用。

        调度器不做 LLM 轮询，这里也不加载工具 schema。
        模型返回空内容表示不值得打扰，调用方应保持安静。
        """
        payload = dict(getattr(event, "payload", None) or {})
        if self._behavior_silent(payload):
            # 程序判定现在不该打扰：直接保持安静，不调用模型。
            return AgentReply(
                ok=True,
                content="",
                debug={
                    "event": getattr(event, "name", ""),
                    "behavior": "silent",
                    "spoke": False,
                },
            )
        situation = payload.get("situation") or payload.get("prompt") or str(
            getattr(event, "name", "")
        )
        recent, memory_block, summary = self._recall(session_id, situation)
        system_prompt = (
            self.system_prompt
            + "\n这是程序触发的主动提醒，不是用户提问。"
            "只说一句简短自然的话。如果现在不值得打扰，就回复空内容。"
        )
        bundle = self.context.build(
            system_prompt=system_prompt,
            recent_messages=recent,
            memory_block=memory_block,
            summary=summary,
            persona_note=self._persona_note(),
            current_input=situation,
        )
        if not self.companion_slot.available():
            local_text = payload.get("local_text")
            if local_text:
                # 程序自己写的提醒（例如倒计时到点），不是模型编的。
                self._update_emotion(payload.get("emotion", "happy"))
                self._remember(session_id, "assistant", local_text)
                return AgentReply(
                    ok=True,
                    content=local_text,
                    debug={
                        "mode": Mode.LOCAL.value,
                        "event": getattr(event, "name", ""),
                        "capability": payload.get("capability"),
                        "emotion": self.emotion.emotion,
                        "spoke": True,
                    },
                )
            return AgentReply(ok=False, error=NO_COMPANION_REPLY)
        try:
            result = self.companion_slot.provider.chat(bundle.messages)
        except Exception as exc:
            self._warn("proactive model call raised: %s", exc)
            self._life("note_error", "主动搭话时模型出错了")
            return AgentReply(ok=False, error=f"模型调用异常：{exc}")
        if not result.ok:
            self._warn("proactive model call failed: %s", result.error)
            self._life("note_error", "主动搭话时模型没答上来")
            return AgentReply(ok=False, error=result.error, model=result.model)

        content = (result.content or "").strip()
        self._record_usage(session_id, result)
        if content:
            self._update_emotion(payload.get("emotion", "happy"))
            self._remember(session_id, "assistant", content)
            self._life("note_talking", "主动搭一句话")

        debug = {
            **bundle.stats,
            "mode": self.mode_name,
            "companion_status": self.companion_slot.status(),
            "task_status": self.task_slot.status(),
            "event": getattr(event, "name", ""),
            "rule": payload.get("rule"),
            "priority": payload.get("priority"),
            "emotion": self.emotion.emotion,
            "spoke": bool(content),
            "prompt_tokens": result.usage.prompt_tokens,
            "completion_tokens": result.usage.completion_tokens,
            "devices": self._device_summary(),
        }
        self.last_debug = debug
        return AgentReply(
            ok=True, content=content, model=result.model, usage=result.usage, debug=debug
        )

    def _note_activity(self):
        if self.behavior is None:
            return
        try:
            self.behavior.note_activity()
        except Exception as exc:
            self._warn("behavior activity note failed: %s", exc)

    def _note_relationship(self):
        """关系状态：用户每说一句都记一次，本地就能答的那些也算。"""
        state = getattr(self.memory, "relationship", None)
        if state is None:
            return
        try:
            state.touch()
        except Exception as exc:
            self._warn("relationship note failed: %s", exc)

    def _behavior_silent(self, payload):
        """程序评分：只有行为层说 INTERACT 才允许开口。

        用户自己设的倒计时 / 提醒（local_text）是程序写好的话术，必须照发，
        不参与「值不值得打扰」的评分。
        """
        if self.behavior is None:
            return False
        if payload.get("local_text") or payload.get("force"):
            return False
        try:
            verdict = self.behavior.should_interact(
                priority=payload.get("priority", "normal"),
                urgent=bool(payload.get("urgent")),
            )
        except Exception as exc:
            self._warn("behavior gate failed: %s", exc)
            return False
        return verdict != BEHAVIOR_INTERACT

    def _publish(self, event_name, **payload):
        if not self.bus:
            return
        try:
            self.bus.emit(event_name, **payload)
        except Exception as exc:
            self._warn("event publish failed for %s: %s", event_name, exc)

    def _device_summary(self):
        """只用于开发者面板，绝不发送给模型。"""
        if not self.devices:
            return {}
        try:
            return {
                "names": self.devices.names(),
                "allow_real": self.devices.allow_real,
            }
        except Exception:
            return {}

    def _select_tools(self, query, safe_only=False):
        """只挑相关工具的 schema，不把全部 schema 发给模型。

        陪伴模型只拿安全工具（READ / LOW）；要动手的工具只在任务模式出现。
        """
        if not self.executor:
            return []
        try:
            names = list(
                self.executor.registry.relevant(
                query, levels=SAFE_LEVELS if safe_only else None
                )
            )
        except Exception as exc:
            self._warn("tool selection failed: %s", exc)
            return []
        if not self.screen_tools_allowed("companion" if safe_only else "task"):
            # 没授权把屏幕内容交给模型，那就连工具都不给它，省得它问出来。
            names = [name for name in names if name not in SCREEN_TOOLS]
        return names

    def screen_tools_allowed(self, role="task"):
        """屏幕类工具能不能交给模型。

        判断规则由装配那一层注入（谁负责屏幕、谁就负责定规矩）；没有注入就按老行为放行，
        屏幕感知的主开关仍然是第一道闸。
        """
        policy = self.screen_policy
        if not callable(policy):
            return True
        try:
            return bool(policy(str(role or "task")))
        except Exception as exc:
            self._warn("screen policy check failed: %s", exc)
            return False

    def _tool_schemas(self, names):
        if not self.executor or not names:
            return None
        try:
            return self.executor.registry.schemas_for(names) or None
        except Exception as exc:
            self._warn("tool schema load failed: %s", exc)
            return None

    def _life(self, method, *args, **kwargs):
        """给生命状态机递个信号。没接状态机就什么都不做，绝不影响对话本身。"""
        machine = getattr(self, "life", None)
        handler = getattr(machine, method, None)
        if not callable(handler):
            return None
        try:
            return handler(*args, **kwargs)
        except Exception as exc:
            self._warn("life %s failed: %s", method, exc)
            return None

    def _run_tools(
        self, session_id, messages, result, usage, tool_debug, provider=None, allowed=None
    ):
        """执行模型请求的工具，再把结果交给模型组织自然语言。

        allowed 是本次真正发出去的 schema 名单：陪伴模型没拿到的高风险工具，
        即使被"猜"出名字也不执行。任务模式传 None，其余仍由权限层把关。

        ponytail: 只跑一轮工具调用。模型看到结果后还要再调工具的场景等真正需要时再加循环。
        """
        actor = provider or self.companion_slot.provider
        calls = [(call.id or f"call_{index}", call) for index, call in enumerate(result.tool_calls)]
        # 模型一旦要问用户，就把问题之后的调用丢掉：既不再执行，也不留在上下文里。
        for index, (_, call) in enumerate(calls):
            if call.name == ASK_USER_TOOL:
                calls = calls[: index + 1]
                break
        conversation = list(messages)
        conversation.append(
            {
                "role": "assistant",
                "content": result.content or "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments or {}, ensure_ascii=False),
                        },
                    }
                    for call_id, call in calls
                ],
            }
        )

        for call_id, call in calls:
            tool_debug["tool_calls"].append({"name": call.name, "arguments": call.arguments})
            if self._cancel_requested:
                outcome = ToolOutcome(
                    name=call.name, ok=False, allowed=False, error="任务已取消，未执行"
                )
                tool_debug["tool_results"].append(outcome.to_dict())
                break
            if allowed is not None and call.name not in allowed:
                outcome = ToolOutcome(
                    name=call.name,
                    ok=False,
                    allowed=False,
                    error="这个工具没有提供给当前模型，已拒绝执行",
                )
                tool_debug["tool_results"].append(outcome.to_dict())
                conversation.append(
                    {"role": "tool", "tool_call_id": call_id, "content": outcome.for_model()}
                )
                continue
            if call.name == ASK_USER_TOOL:
                question = ""
                if isinstance(call.arguments, dict):
                    question = str(call.arguments.get("question") or "")
                self._pending_question = question.strip() or "需要你先补充一些信息。"
                outcome = ToolOutcome(
                    name=ASK_USER_TOOL,
                    ok=True,
                    result={"question": self._pending_question, "note": "已向用户提问，等待回答"},
                )
                tool_debug["tool_results"].append(outcome.to_dict())
                conversation.append(
                    {"role": "tool", "tool_call_id": call_id, "content": outcome.for_model()}
                )
                break
            try:
                outcome = self.executor.execute(call.name, call.arguments)
            except Exception as exc:
                self._warn("tool execution failed: %s", exc)
                outcome = ToolOutcome(name=call.name, ok=False, error=f"执行层异常：{exc}")
            tool_debug["tool_results"].append(outcome.to_dict())
            if self.mode == Mode.TASK:
                self._task_tool_calls += 1
                self._record_step(outcome)
                self.save_task_record()
            self._publish(
                bus_module.TOOL_COMPLETED,
                name=outcome.name,
                ok=outcome.ok,
                allowed=outcome.allowed,
                level=outcome.level,
            )
            conversation.append(
                {"role": "tool", "tool_call_id": call_id, "content": outcome.for_model()}
            )

        if self._cancel_requested:
            # 取消后不再让模型继续，也不执行任何后续工具。
            return ChatResult(ok=False, error="任务已取消", model=getattr(result, "model", "")), usage

        if provider is not None:
            # 任务路径：已经跑过的工具结果留在上下文里，下一轮从完整历史继续，不重复执行。
            self._task_messages = conversation

        if self._pending_question:
            # 任务在等用户补信息：先不生成回复，把问题交给用户。
            return ChatResult(ok=True, content="", model=getattr(result, "model", "")), usage

        try:
            follow_up = actor.chat(conversation)
        except Exception as exc:
            return ChatResult(ok=False, error=f"工具已执行，但生成回复失败：{exc}"), usage
        if not follow_up.ok:
            return (
                ChatResult(ok=False, error=f"工具已执行，但生成回复失败：{follow_up.error}"),
                usage,
            )

        self._record_usage(session_id, follow_up)
        total = Usage(
            prompt_tokens=usage.prompt_tokens + follow_up.usage.prompt_tokens,
            completion_tokens=usage.completion_tokens + follow_up.usage.completion_tokens,
        )
        return follow_up, total

    # ---------- 记忆降级：任何记忆故障都不应中断对话 ----------
    def _update_emotion(self, name):
        """情绪由程序决定，模型不能直接控制形象。"""
        self.emotion.set(name)
        if not self.avatar:
            return
        try:
            self.avatar.set_state(self.emotion.to_dict())
        except Exception as exc:
            self._warn("avatar update failed: %s", exc)

    def _remember(self, session_id, role, content):
        try:
            return self.memory.record(session_id, role, content)
        except Exception as exc:
            self._warn("memory write failed, continuing without memory: %s", exc)
            return None

    def _recall(self, session_id, query):
        try:
            return self.memory.recall(session_id, query)
        except Exception as exc:
            self._warn("memory read failed, continuing without memory: %s", exc)
            return [], "", ""

    def _record_usage(self, session_id, result):
        try:
            self.memory.record_usage(session_id, result.model, result.usage)
        except Exception as exc:
            self._warn("usage record failed: %s", exc)

    def _recall_stats(self):
        stats = getattr(self.memory, "last_recall", None)
        return dict(stats) if isinstance(stats, dict) else {}

    # ---------- V3：可观测性与任务落库 ----------
    def _record_turn(self, reply):
        """把本轮的判断写进诊断缓冲。只记结论，不记模型私密推理。"""
        if self.diagnostics is None:
            return None
        debug = reply.debug if isinstance(reply.debug, dict) else {}
        try:
            return self.diagnostics.record(
                "turn",
                route=self.last_route.to_dict() if self.last_route else {},
                mode=debug.get("mode", self.mode_name),
                capability=debug.get("capability"),
                tools=list(debug.get("tools_offered") or []),
                tool_results=list(debug.get("tool_results") or []),
                memory_count=debug.get(
                    "memory_count", self._recall_stats().get("memory_count", 0)
                ),
                estimated_tokens=debug.get("estimated_tokens", 0),
                task_state=self.task_state,
                ok=bool(reply.ok),
                local=bool(debug.get("local")),
            )
        except Exception as exc:
            self._warn("diagnostics record failed: %s", exc)
            return None

    def recent_turns(self, limit=10):
        if self.diagnostics is None:
            return []
        return self.diagnostics.recent(limit)

    def save_task_record(self):
        """把当前任务记录写进库。没接持久化就什么也不做。"""
        if self.task_store is None or self.task_record is None:
            return None
        record = self.task_record
        record.state = self.task_state
        record.plan = [step.to_dict() for step in self._task_steps]
        record.pending_question = self._pending_question
        record.error = record.error or ""
        try:
            task_id = self.task_store.save(record)
            self.task_store.replace_steps(record.id, self._task_steps)
        except Exception as exc:
            self._warn("task persist failed: %s", exc)
            return None
        return task_id

    def unfinished_tasks(self, limit=5):
        """启动时展示的待续任务。只看一眼，绝不自动重跑。"""
        if self.task_store is None:
            return []
        try:
            return self.task_store.unfinished(limit=limit)
        except Exception as exc:
            self._warn("unfinished task read failed: %s", exc)
            return []

    def task_history(self, limit=20):
        if self.task_store is None:
            return []
        try:
            return self.task_store.recent(limit=limit)
        except Exception as exc:
            self._warn("task history read failed: %s", exc)
            return []

    def _persona_note(self):
        try:
            extra = self.policy.prompt_lines()
        except Exception as exc:
            self._warn("persona policy note failed: %s", exc)
            extra = []
        return build_persona_note(self.persona, extra=extra)

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)
