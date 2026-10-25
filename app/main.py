"""桌宠启动入口。开发用 python 跑，用户用打包后的 Blossom.exe 跑，同一个入口。"""

import argparse
import ctypes
import os
import sqlite3
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.context import ContextManager  # noqa: E402
from agent.diagnostics import Diagnostics  # noqa: E402
from agent.task_store import TaskStore  # noqa: E402
from memory.review import ReviewService  # noqa: E402
from agent.core import AgentCore  # noqa: E402
from avatar.live2d import build_provider as build_avatar  # noqa: E402
from avatar import live2d  # noqa: E402
from behavior.activity import ActivityWatcher, WindowsUserActivityProvider  # noqa: E402
from behavior.body import PhysicalBody  # noqa: E402
from behavior.detections import DetectionWatcher, NullDetectionProvider  # noqa: E402
from behavior.environment import EnvironmentWatcher, WindowsEnvironmentProvider  # noqa: E402
from behavior.focus import FocusMode, WindowsWindowProvider  # noqa: E402
from behavior.life import LifeStateMachine  # noqa: E402
from behavior.quiet import QuietMode  # noqa: E402
from behavior.screen import ScreenWatcher, WindowsScreenProvider  # noqa: E402
from behavior.awareness import build_service as build_awareness_service  # noqa: E402
from behavior.vision import NullVisionProvider  # noqa: E402
from behavior.vision import build_service as build_vision_service  # noqa: E402
from behavior.manager import BehaviorManager  # noqa: E402
from characters.manager import CharacterManager  # noqa: E402
from database.db import Database  # noqa: E402
from devices.esp32 import Esp32Adapter  # noqa: E402
from devices.interface import MockDevice  # noqa: E402
from devices.manager import DeviceManager  # noqa: E402
from devices.stack import build_stack  # noqa: E402
from events import bus as bus_module  # noqa: E402
from events.bus import EventBus  # noqa: E402
from extensions import packs  # noqa: E402
from logkit.logger import configure, get_logger, redact, register_secret  # noqa: E402
from memory.manager import MemoryManager  # noqa: E402
from memory.repository import MemoryRepository  # noqa: E402
from memory.todo import TodoStore  # noqa: E402
from model.adapter import build_companion_provider, build_task_provider, sdk_status  # noqa: E402
from model.logged import LoggedProvider  # noqa: E402
from model.base import ModelRole, ModelSlot  # noqa: E402
from model import config as model_config_module  # noqa: E402
from persona.interface import DefaultPersonaProvider  # noqa: E402
from persona.package import PackagePersonaProvider, list_packages  # noqa: E402
from persona.training import LocalTrainingCollector  # noqa: E402
from persona.policy import PersonaPolicy  # noqa: E402
from persona.traits import load as load_traits  # noqa: E402
from persona.traits import parse as parse_traits  # noqa: E402
from persona import profile as persona_profile  # noqa: E402
from persona import cards as persona_cards  # noqa: E402
from persona.learner import HabitLearner  # noqa: E402
from pets.manager import from_config as build_pets  # noqa: E402
from scheduler.manager import Scheduler  # noqa: E402
from scheduler.reminders import CalendarReminder, TodoReminder  # noqa: E402
from scheduler.store import ScheduledJobStore  # noqa: E402
from skills import build_skills  # noqa: E402
from scheduler.rules import install as install_proactive_rules  # noqa: E402
from services.cache import TTLCache  # noqa: E402
from services.calendar import CalendarService  # noqa: E402
from services.file_organizer import FileOrganizer  # noqa: E402
from services.worklog import WorkLogService  # noqa: E402
from services.model_calls import ModelCallLog  # noqa: E402
from services.weather import WeatherService  # noqa: E402
from security.audit import AuditLog  # noqa: E402
from security.config import Config  # noqa: E402
from security.privacy import PrivacyCenter  # noqa: E402
from tools.permission import PermissionManager  # noqa: E402
from tools.permission import GrantStore  # noqa: E402
from tools.builtin import register_default_tools  # noqa: E402
from tools.device_actions import register_device_tools  # noqa: E402
from tools.local import register_local_tools  # noqa: E402
from tools.local_extra import register_extra_tools  # noqa: E402
from tools.runtimes import register_runtime_tools  # noqa: E402
from tools.file_organizer import register_file_organizer_tools  # noqa: E402
from tools.skills import skills_tool  # noqa: E402
from tools.registry import ToolExecutor, ToolRegistry  # noqa: E402
from tools.workspace import authorized_roots  # noqa: E402
from tools.workspace import workspace_root  # noqa: E402
from ui import theme  # noqa: E402
from ui.desktop import Confirmer, launch  # noqa: E402
from voice import pack as voice_pack  # noqa: E402
from voice.capture import build_capture  # noqa: E402
from voice.interface import NullVoiceProvider  # noqa: E402
from voice.manager import SpeechPipeline, TranscribePipeline, VoicePipeline  # noqa: E402
from voice.stt import NullSttProvider, build_stt  # noqa: E402
from voice.speaker import build_provider as build_speech  # noqa: E402
from voice.wake import MicPrivacy, NullWakeWordProvider, WakeWordDetector  # noqa: E402

APP_NAME = "Blossom"
MUTEX_NAME = "Blossom.SingleInstance"
WINDOW_TITLE = "桌宠"
ERROR_ALREADY_EXISTS = 183

# 界面起了但窗口迟迟不出现时的兜底等待时间（秒）。
STARTUP_WATCHDOG_SECONDS = 45

_instance_handle = None
# 已经报过一次启动错误就不再重复弹窗，免得用户被连弹两次。
_fatal_reported = False


def _declare_window_lookup(user32):
    """窗口查找同样返回 64 位句柄，先声明好类型再调用。"""
    from ctypes import wintypes

    user32.FindWindowW.restype = wintypes.HWND
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    return user32


def acquire_single_instance():
    """第二次双击不再开第二个桌宠。非 Windows 或失败时不阻塞启动。"""
    global _instance_handle
    if os.name != "nt":
        return True
    try:
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        # 64 位下句柄是 64 位宽，不声明类型会被截断，单实例判断会变得不可靠。
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        _instance_handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if not _instance_handle:
            return True
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(_instance_handle)
            _instance_handle = None
            return False
    except Exception:
        return True
    return True


def focus_existing_window():
    """已经在运行的桌宠窗口拉到最前面；找不到就算了。"""
    if os.name != "nt":
        return False
    try:
        user32 = ctypes.windll.user32
        _declare_window_lookup(user32)
        handle = user32.FindWindowW(None, WINDOW_TITLE)
        if not handle:
            return False
        user32.ShowWindow(handle, 9)  # SW_RESTORE
        user32.SetForegroundWindow(handle)
        return True
    except Exception:
        return False


def _message_box(message, flags=0x10, title=APP_NAME):
    """桌宠所有弹窗的唯一出口。

    集中在一处的好处：测试只替换这一个函数就不会弹出真窗口；
    出问题时也只用改这里就能把弹窗内容记进日志。
    """
    if os.name != "nt":
        print(message, file=sys.stderr)
        return False
    try:
        ctypes.windll.user32.MessageBoxW(None, message, title, flags)
        return True
    except Exception:
        return False


def notify_already_running():
    _message_box("Blossom 已经在运行了，看看桌面上的桌宠。", flags=0x40)


def friendly_error(exc):
    """给普通用户看的一句话，详细 traceback 只进日志。"""
    if isinstance(exc, (sqlite3.Error, OSError, PermissionError)):
        return "桌宠数据无法读取或写入，请查看日志文件了解详情。"
    return "桌宠启动失败，请查看日志文件了解详情。"


def report_fatal(exc):
    global _fatal_reported
    _fatal_reported = True
    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    message = friendly_error(exc)
    log_path = ""
    try:
        config = Config(load_env=False)
        # 先登记密钥再写盘：报错文本里万一夹着密钥，落盘前也会被抹掉。
        for secret in config.secret_values():
            register_secret(secret)
        detail = redact(detail)
        log_dir = config.log_dir
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "startup-error.log"
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} ===\n{message}\n{detail}")
        # 弹窗里带上日志位置，用户不用猜日志在哪。
        log_path = str(log_file)
    except Exception:
        # 连配置都读不出来时也不能什么都不留：退到临时目录，好歹留下证据。
        try:
            fallback = Path(os.environ.get("TEMP") or ".") / "deskpet-startup-error.log"
            with fallback.open("a", encoding="utf-8") as handle:
                handle.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} ===\n{message}\n{detail}")
            log_path = str(fallback)
        except Exception:
            pass
    if log_path:
        message = f"{message}\n\n详细日志：{log_path}"
    if _message_box(message, flags=0x10):
        return
    print(message, file=sys.stderr)


def deskpet_window_exists():
    """桌宠的透明载体窗口有没有出现。非 Windows 一律当作已出现。"""
    if os.name != "nt":
        return True
    try:
        user32 = ctypes.windll.user32
        _declare_window_lookup(user32)
        return bool(user32.FindWindowW(None, WINDOW_TITLE))
    except Exception:
        return True


def notify_startup_stalled():
    _message_box("桌宠窗口没能显示出来，详细情况已经写进日志文件。", flags=0x30)


def _log_is_fresh(log_file, seconds):
    """日志还在写，说明程序只是在慢慢启动，不是卡住了。"""
    try:
        return time.time() - Path(log_file).stat().st_mtime <= max(1.0, float(seconds))
    except (OSError, TypeError, ValueError):
        return False


def start_startup_watchdog(
    logger=None, seconds=STARTUP_WATCHDOG_SECONDS, log_file=None, rounds=4
):
    """界面启动卡住时给用户一句人话，而不是让桌面上什么都没有。

    只在窗口一直没出现、日志也不再更新、并且没有别的地方已经报过错的时候开口。
    慢启动（杀毒扫描、WebView2 首次初始化、数据库被占用）不该被当成启动失败。
    """

    def watch(rounds_left=rounds):
        deadline = time.monotonic() + max(1, int(seconds))
        while time.monotonic() < deadline:
            time.sleep(1.0)
            if deskpet_window_exists():
                return
        if deskpet_window_exists() or _fatal_reported:
            return
        # 还在写日志就是还在干活，换个新的观察窗口再等，最多等 rounds 轮。
        if rounds_left > 1 and _log_is_fresh(log_file, seconds):
            return watch(rounds_left - 1)
        if logger:
            logger.error(
                "startup watchdog: no deskpet window after %s seconds", seconds
            )
        notify_startup_stalled()

    thread = threading.Thread(target=watch, name="deskpet-startup-watchdog", daemon=True)
    thread.start()
    return thread


def bootstrap():
    """装配 Config / Logger / SQLite / ModelProvider。"""
    config = Config()
    logger = configure(config.log_dir, config.log_level, config.dev_mode)
    # 适配层要把服务商错误写进同一条日志，这里把 logger 挂到配置上。
    config.logger = logger
    # 旧 .env 里的模型配置一次性搬进配置仓库（只读 .env，不改它）。
    safe_call(
        logger,
        "model configs",
        lambda: model_config_module.import_env_configs(config, logger=logger),
        [],
    )
    for secret in config.secret_values():
        register_secret(secret)
    # 可选能力包：装了文档解析 / 离线语音的包，就在这里接进 sys.path。
    # 一个都没装也照常走，后面用到时各自会如实说"没装能力包"。
    safe_call(logger, "capability packs", lambda: packs.activate(config), [])
    # 随包带的角色模型：第一次跑的时候放进用户目录，之后用户换自己的模型就换。
    safe_call(
        logger,
        "bundled models",
        lambda: live2d.seed_bundled_models(config, logger=logger),
        [],
    )
    # 内置角色卡：第一次跑时放进用户数据目录的卡库，之后用户自己增删。
    safe_call(
        logger,
        "persona cards",
        lambda: persona_cards.seed_library(config, logger=logger),
        [],
    )
    database = Database(config.db_path)
    applied = database.migrate()
    provider = build_companion_provider(config)
    logger.info("bootstrap done, migrations applied: %s", applied)
    return config, logger, database, provider


def build_agent(
    config, database, provider, logger, confirm_callback=None, bus=None, task_provider=None,
    window=None,
):
    if task_provider is None:
        task_provider = build_task_provider(config)
    model_call_log = ModelCallLog(database, logger=logger)
    provider = LoggedProvider.wrap(provider, model_call_log, "companion")
    task_provider = LoggedProvider.wrap(task_provider, model_call_log, "task")
    # V3：任务落库、诊断缓冲、回顾服务。三者都只读本地数据，不联网。
    task_store = TaskStore(database, logger=logger)
    calendar_service = CalendarService(database)
    diagnostics = Diagnostics(logger=logger)
    review = ReviewService(database, memory=None, tasks=task_store, logger=logger)

    memory = MemoryManager(
        MemoryRepository(database),
        recent_window=config.recent_window,
        summary_trigger=config.summary_trigger,
        memory_limit=config.memory_limit,
        summary_max_chars=getattr(config, "summary_max_chars", 600),
        memory_capacity=getattr(config, "memory_capacity", 2000),
        logger=logger,
    )
    persona_policy = PersonaPolicy(memory)
    context = ContextManager(
        token_budget=config.context_budget, recent_window=config.recent_window
    )
    audit = AuditLog(database)
    permissions = PermissionManager(
        confirm_callback=confirm_callback,
        audit=audit,
        logger=logger,
        # 持久授权落在数据目录里，重启后还在；范围由用户逐条给。
        grants=GrantStore(Path(config.data_dir) / "permissions.json", logger=logger),
    )
    devices = DeviceManager(
        logger=logger, audit=audit, allow_real=config.allow_real_devices
    )
    devices.register(MockDevice(name="mock-1", capabilities=("servo", "sensor")))
    # ESP32 适配层：默认走模拟传输，真硬件要显式打开 allow_real 才可能注册。
    devices.register(Esp32Adapter(logger=logger))
    # V11：设备栈。带版本号的动作协议、动作注册表、设备权限、任务队列和事件桥。
    # 设备清单由设备栈读一次、注册进上面这一份注册表，别处不再重复装一遍；
    # 设备中心、设备工具和实体身体因此看到的是同一份清单。
    # 默认装一台模拟设备把链路跑通；真串口要清单里写了端口，而且 allow_real 打开。
    device_stack = safe_call(
        logger,
        "device stack",
        lambda: build_stack(
            manager=devices,
            devices_dir=getattr(config, "devices_dir", ""),
            logger=logger,
            audit=audit,
            permissions=permissions,
            bus=bus,
            allow_real=config.allow_real_devices,
            start_worker=True,
        ),
        None,
    )
    manifest = getattr(device_stack, "manifest_results", None) or []
    if manifest:
        logger.info("device manifest: %d device(s)", len(manifest))
        for item in manifest:
            if not item.get("ok"):
                logger.warning("device %s refused: %s", item["name"], item.get("error", ""))
    registry = ToolRegistry()
    # V4：多装一组工作区工具（读写文件、跑白名单测试、看 Git）。
    register_default_tools(
        registry,
        config,
        database,
        devices=devices,
        workspace=True,
        calendar=calendar_service,
        # V5：权限中心里持久授权过的目录，文件工具才认；否则只认工作区。
        extra_roots=lambda: authorized_roots(
            permissions.grants.all() if permissions.grants else []
        ),
    )
    # V5：本地工具（时间、系统状态、计时器、待办）。没有模型、断网也能用。
    # 计时器要等 Scheduler 挂上才有，所以这里传一个等用到时再取的回调。
    # 先注册再装 Skill：技能里声明了本地工具的，才能通过校验。
    todo_store = TodoStore(database, logger=logger)
    todo_reminder = TodoReminder(todo_store, logger=logger, policy=persona_policy)
    try:
        register_local_tools(
            registry,
            database=database,
            config=config,
            timer=lambda: agent.capabilities.scheduler,
            logger=logger,
            todos=todo_store,
            on_todo_added=todo_reminder.schedule_todo,
        )
    except Exception as exc:
        logger.warning("local tools registration failed: %s", exc)
    # V9.1：本地能力的补充工具（文本整理、任务待续、自检、备份）。
    # 和上面的本地工具一样，不需要模型也不联网；恢复那条是高风险，过权限中心。
    try:
        register_extra_tools(
            registry,
            database=database,
            config=config,
            task_store=task_store,
            grants=(lambda: permissions.grants.all() if permissions.grants else []),
            logger=logger,
        )
    except Exception as exc:
        logger.warning("extra local tools registration failed: %s", exc)
    # V9.1：本地知识库。加目录由界面触发，模型只能查已经索引好的片段。
    knowledge = None
    try:
        from knowledge.service import KnowledgeService
        from tools.knowledge import register_knowledge_tools

        knowledge = KnowledgeService(
            database=database,
            config=config,
            logger=logger,
            authorized=lambda: authorized_roots(
                permissions.grants.all() if permissions.grants else []
            )
            + [workspace_root(config)],
        )
        register_knowledge_tools(registry, service=knowledge)
    except Exception as exc:
        logger.warning("knowledge tools registration failed: %s", exc)
    # P1：文件整理。预览只生成计划；执行和撤销都是高风险工具，必须过权限中心。
    file_organizer = None
    try:
        file_organizer = FileOrganizer(
            database,
            authorized=lambda: authorized_roots(
                permissions.grants.all() if permissions.grants else []
            )
            + [workspace_root(config)],
            logger=logger,
        )
        register_file_organizer_tools(registry, service=file_organizer)
    except Exception as exc:
        logger.warning("file organizer registration failed: %s", exc)
    # V9.1：本地运行时探测（本地模型、文档解析、离线语音、界面理解、全文检索）。
    # 只看本机装了什么，不下载、不安装、不启动服务。
    try:
        register_runtime_tools(registry, config)
    except Exception as exc:
        logger.warning("runtime tools registration failed: %s", exc)
    # V9：屏幕与语音状态工具。都只读：屏幕感知默认关着，语音工具不会自己开麦。
    try:
        from tools.screen import register_screen_tools
        from tools.voice import register_voice_tools

        register_screen_tools(registry, config)
        register_voice_tools(registry, config)
    except Exception as exc:
        logger.warning("screen/voice tools registration failed: %s", exc)
    # V11：设备工具。模型只能请求登记过的动作，参数与授权都由程序校验。
    register_device_tools(registry, device_stack)
    executor = ToolExecutor(registry, permissions, audit=audit, logger=logger)
    # V4：Skill 系统。装不上就降级为空系统，启动照旧。
    try:
        skills = build_skills(
            config=config, tools=executor, memory=memory, logger=logger, database=database
        )
    except Exception as exc:
        if logger:
            logger.warning("skills build failed, continuing: %s", exc)
        skills = None
    # 角色资源：把角色包清单和真实模型文件对上。没装角色包时行为与以前完全一致。
    characters = CharacterManager(
        characters_root=getattr(config, "characters_dir", ""),
        live2d_root=getattr(config, "live2d_dir", ""),
        selected=getattr(config, "character", ""),
        default_model=getattr(config, "live2d_model", ""),
        logger=logger,
    )
    agent = AgentCore(
        provider,
        memory,
        context,
        config=config,
        logger=logger,
        persona=PackagePersonaProvider(
            default=DefaultPersonaProvider(
                name=config.persona_name,
                traits=parse_traits(config.persona_traits),
                feedback=lambda: memory.feedback_memories(),
            ),
            packages=list_packages(getattr(config, "characters_dir", "")),
            selected=getattr(config, "character", ""),
        ),
        training=LocalTrainingCollector(database, logger=logger),
        avatar=build_avatar(config, logger=logger, characters=characters),
        executor=executor,
        bus=bus,
        devices=devices,
        task_provider=task_provider,
        companion_slot=ModelSlot(
            ModelRole.COMPANION.value, provider, configured=config.has_companion()
        ),
        task_slot=ModelSlot(
            ModelRole.TASK.value, task_provider, configured=config.has_task()
        ),
        task_confirm=config.task_confirm,
        task_store=task_store,
        diagnostics=diagnostics,
        skills=skills,
        policy=persona_policy,
        model_call_log=model_call_log,
    )
    # 角色包管理器挂在 Agent 上，界面只读它。不经过模型，也不影响权限。
    agent.characters = characters
    # V11：设备栈也挂在 Agent 上。设备中心只读它，动作一律走栈里的任务管理。
    agent.device_stack = device_stack
    agent.file_organizer = file_organizer
    agent.model_call_log = model_call_log
    # P3：工作记录默认关闭；这里只装配服务，不启动采集。
    worklog = WorkLogService(database, preferences=memory, logger=logger)
    agent.worklog = worklog
    agent.capabilities.worklog = worklog
    # 技能清单也能当工具查：模型只读得到能力目录，跑技能仍然走 Agent。
    if skills is not None:
        try:
            registry.register(skills_tool(skills))
        except Exception as exc:
            logger.warning("skills tool registration failed: %s", exc)
    # V5 第三阶段：生命状态与待办提醒。都由程序推进，不经过模型。
    agent.life = LifeStateMachine(bus=bus, logger=logger, preferences=memory).attach(bus)
    agent.todo_store = todo_store
    agent.todo_reminder = todo_reminder
    # 界面要用到的执行层与审计，挂到 Agent 上只读展示，不改变任何流程。
    agent.permissions = permissions
    agent.audit = audit
    agent.registry = registry
    # 用户调过的性格强度存在偏好里，启动时套回去，重启后行为一致。
    saved_traits = load_traits(memory)
    if saved_traits:
        try:
            agent.persona.set_traits(saved_traits)
        except Exception as exc:
            logger.warning("persona traits apply failed: %s", exc)
    # V6：用户自己写的人格文本与习惯也存在偏好里，启动时套回去。
    try:
        applied = persona_profile.apply_to(
            agent.persona, memory, default_name=getattr(config, "persona_name", "")
        )
        if not applied.get("ok"):
            logger.warning("persona text not applied: %s", applied.get("error"))
    except Exception as exc:
        logger.warning("persona text apply failed: %s", exc)
    # V6 第三阶段：习惯学习。
    # 只从用户明说的偏好里挑候选，学习开关关掉就一条不记。
    try:
        agent.learning = HabitLearner(
            preferences=memory, persona=agent.persona, logger=logger
        )
        agent.learning.apply()
    except Exception as exc:
        logger.warning("habit learner setup failed: %s", exc)
    # 行为层：待机与互动的唯一决策者，程序规则，不经过模型。
    agent.attach_behavior(
        BehaviorManager(
            emotion=agent.emotion,
            avatar=agent.avatar,
            persona=agent.persona,
            preferences=memory,
            bus=bus,
            logger=logger,
            window=window,
            policy=agent.policy,
        )
    )
    # V3：本地能力要能读任务、日程和项目记忆，读不到时如实说读不到。
    agent.capabilities.tasks = task_store
    agent.capabilities.calendar = calendar_service
    agent.capabilities.database = database
    # V9.1：知识库服务。界面和工具都从这里取，没有就如实说没接入。
    if knowledge is not None:
        agent.knowledge = knowledge
    review.memory = memory
    agent.capabilities.review = review
    # 多宠物名册：默认一只加每个角色包一只，换宠物只换人格和会话。
    agent.capabilities.pets = build_pets(config, persona=agent.persona, logger=logger)
    # 记忆归属跟着当前这只宠物走：私有记忆不串门，公共记忆照旧共享。
    memory.use_pet(agent.capabilities.pets.active)
    agent.task_store = task_store
    agent.review = review
    # V9：扩展注册表与可选后端适配。
    # 只登记、只装配，不下载、不安装、不执行任何第三方代码；默认全都不启用。
    try:
        from adapters import CommandOutputReducer, build_memory_backend, build_runtime, build_vision_provider, build_voice_provider
        from extensions import ExtensionRegistry
        from tools import workspace as workspace_module

        extensions = ExtensionRegistry()
        agent.extensions = extensions
        reducer = CommandOutputReducer(registry=extensions, logger=logger)
        agent.output_reducer = reducer
        try:
            workspace_module.set_output_reducer(reducer)
        except Exception as exc:
            logger.warning("output reducer hook failed: %s", exc)
        agent.memory_backend = build_memory_backend(extensions, memory)
        agent.runtime_adapter = build_runtime(extensions, agent)
        # 语音与视觉只装配接口：不主动开麦，不开屏幕感知，等用户自己打开。
        agent.voice_provider = build_voice_provider(extensions)
        agent.vision_provider = build_vision_provider(
            extensions, privacy=lambda: bool(getattr(config, "screen_perception", False))
        )
        # 只读工具接到真实后台上；开关始终在用户手里，工具不会自己去动。
        try:
            from tools import screen as screen_module
            from tools import voice as voice_module

            screen_module.set_vision_provider(agent.vision_provider)
            voice_module.set_voice_provider(agent.voice_provider)
        except Exception as exc:
            logger.warning("screen/voice provider hook failed: %s", exc)
    except Exception as exc:
        # 扩展层出问题不该拦着桌宠启动。
        logger.warning("extensions wiring failed: %s", exc)
    return agent, memory


def build_topic_source(memory, tasks, calendar):
    """自主话题的素材来源。只从用户允许的真实数据里取，取不到就闭嘴。"""

    def topics(ctx):
        # 优先用项目进展，其次是马上要到的日程，最后是最近做完的任务。
        try:
            rows = memory.browse(kind="project", limit=3)
        except Exception:
            rows = []
        for row in rows or []:
            content = row.get("content") if isinstance(row, dict) else str(row)
            if content:
                return {
                    "situation": "项目进展是这样的：%s" % str(content)[:60],
                    "local_text": "顺手说一句：%s" % str(content)[:60],
                    "emotion": "happy",
                    "topic": "project",
                }
        if calendar is not None:
            try:
                result = calendar.list_events(limit=3)
            except Exception:
                result = None
            events = (result or {}).get("data", {}).get("events") if result else None
            if events:
                row = events[0]
                return {
                    "situation": "有个日程：%s（%s）" % (row["title"], row["when_at"]),
                    "local_text": "别忘了：%s。" % row["title"],
                    "emotion": "neutral",
                    "topic": "calendar",
                }
        for record in tasks.recent(limit=3) if tasks is not None else []:
            if record.state == "completed":
                return {
                    "situation": "上次做完的任务：%s" % record.goal[:40],
                    "local_text": "上次那个「%s」我做完了，还想接着弄吗？" % record.goal[:30],
                    "emotion": "happy",
                    "topic": "task",
                }
        return None

    return topics


def build_scheduler(
    config, memory, logger, bus, calendar=None, tasks=None, todos=None, store=None
):
    """主动陪伴：由程序判断是否值得打扰，命中的事件才交给 Agent 说话。

    调度器本身始终存在：本地倒计时/提醒要它来发事件。
    DESKPET_PROACTIVE=0 时只是不装主动规则，不影响用户自己设的计时。
    """
    scheduler = Scheduler(
        bus,
        preferences=memory,
        logger=logger,
        cooldown_floor=getattr(config, "proactive_cooldown", 0),
        daily_limit=getattr(config, "proactive_max_per_day", 0),
        max_per_tick=getattr(config, "proactive_max_per_tick", 0),
        store=store,
    )
    topics = build_topic_source(memory, tasks, calendar)
    # V5：早上说天气、晚上收个尾。没配城市就不装天气规则，读不到就不开口。
    weather = None
    if getattr(config, "weather_city", ""):
        weather = WeatherService(
            cache=TTLCache(ttl_seconds=config.service_cache_ttl),
            default_city=config.weather_city,
        )
    return install_proactive_rules(
        scheduler,
        config,
        calendar=calendar,
        topics=topics,
        todos=todos,
        tasks=tasks,
        weather=weather,
    )


def cli_confirm(tool_name, level, reason):
    detail = f"：{reason}" if reason else ""
    print(f"\n[权限] 桌宠请求执行 {tool_name}（等级 {level}）{detail}")
    answer = input("允许吗？[y/N] ").strip().lower()
    return answer in {"y", "yes"}


def chat_loop(agent, config, bus=None, scheduler=None, session_id="cli"):
    def on_companion(event):
        reply = agent.respond_to_event(event, session_id=session_id)
        if reply.ok and reply.content:
            print(f"\n桌宠（主动）: {reply.content}")
        if config.dev_mode and reply.debug:
            print(f"[debug] {reply.debug}")

    if bus:
        bus.subscribe(bus_module.COMPANION, on_companion)
    if scheduler and scheduler.start():
        print("主动陪伴已开启。")
    print("桌宠已就绪。输入 exit 退出。")
    try:
        return _chat_messages(agent, config, session_id)
    finally:
        if scheduler:
            scheduler.stop()


def _chat_messages(agent, config, session_id):
    while True:
        try:
            text = input("你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if text in {"exit", "quit", "退出"}:
            return 0
        if not text:
            continue
        reply = agent.respond(session_id, text)
        if reply.ok:
            print(f"桌宠: {reply.content}")
        else:
            print(f"[错误] {reply.error}")
        if config.dev_mode and reply.debug:
            print(f"[debug] {reply.debug}")


RENDERER_FLAG = "--live2d-renderer"


def main(argv=None):
    args_in = list(sys.argv[1:] if argv is None else argv)
    if RENDERER_FLAG in args_in:
        # 打包后角色窗口是这个 EXE 的另一个实例，只负责画画，不碰配置和数据库。
        from avatar.web2d_renderer import main as render_main  # noqa: E402

        args_in.remove(RENDERER_FLAG)
        return render_main(args_in)
    parser = argparse.ArgumentParser(prog="deskpet", description="AI 桌宠")
    parser.add_argument("--check", action="store_true", help="运行环境自检后退出")
    parser.add_argument("--chat", action="store_true", help="在终端与桌宠对话")
    parser.add_argument("--ui", action="store_true", help="启动桌面桌宠界面（默认）")
    args = parser.parse_args(argv)

    config, logger, database, provider = bootstrap()

    if args.check:
        lines = [f"{key}: {value}" for key, value in config.redacted().items()]
        health = database.health()
        lines.append(f"sqlite: {health['sqlite_version']} {health['path']}")
        lines.append(f"tables: {', '.join(health['tables']) or '(none)'}")
        companion = ModelSlot(ModelRole.COMPANION.value, provider, configured=config.has_companion())
        task = ModelSlot(
            ModelRole.TASK.value, build_task_provider(config), configured=config.has_task()
        )
        lines.append(f"companion_model: {companion.status()}")
        lines.append(f"task_model: {task.status()}")
        lines.append(f"model_sdk: {sdk_status()}")
        # V9.1：可选组件在这里做一次真导入，打包漏文件时自检就能看出来。
        try:
            from adapters import documents

            ok, note = documents.self_test()
            lines.append("documents: %s %s" % ("ready" if ok else "unavailable", note))
        except Exception as exc:
            lines.append(f"documents: unavailable {exc}")
        try:
            import sherpa_onnx  # noqa: F401

            lines.append("sherpa_onnx: import ok")
        except Exception as exc:
            lines.append(f"sherpa_onnx: import failed {exc}")
        try:
            from voice.stt import streaming_model_status

            subtitle = streaming_model_status(
                getattr(config, "stt_model", ""),
                kind=getattr(config, "stt_kind", "auto"),
            )
            lines.append(
                "subtitle_asr: %s %s"
                % (subtitle.get("state"), subtitle.get("note") or "")
            )
        except Exception as exc:
            lines.append(f"subtitle_asr: probe failed {exc}")
        # 记忆全文索引：在库上真建一次，报分词方式与条数。
        try:
            from memory.fts import describe as fts_describe

            state = fts_describe(database, logger=logger)
            if state["ready"]:
                lines.append(
                    "memory_fts: ready %s 分词 %d 条"
                    % (state["tokenizer"] or "默认", state["indexed"])
                )
            else:
                lines.append("memory_fts: unavailable %s" % state["reason"])
        except Exception as exc:
            lines.append(f"memory_fts: unavailable {exc}")
        # 知识库：真在库上建一次索引，报文档与片段数。
        try:
            from knowledge.service import KnowledgeService

            report = KnowledgeService(database=database, logger=logger).self_test()
            lines.append(
                "knowledge: %s %s" % ("ready" if report["ok"] else "unavailable", report["detail"])
            )
        except Exception as exc:
            lines.append(f"knowledge: unavailable {exc}")
        # 界面语义：真读一次前台窗口，确认打包后 COM 这套还是通的。
        try:
            from behavior.awareness import UiAutomationProvider

            provider = UiAutomationProvider()
            note = provider.describe().get("note", "")
            if provider.is_available():
                # 只报读没读到，不把窗口标题写进日志：自检日志不该装用户在看什么。
                data = provider.read(limit=3)
                if data.get("page"):
                    note = "%s；试读前台窗口正常（拿到标题，%d 个控件）" % (
                        note,
                        len(data.get("controls") or []),
                    )
                else:
                    note = "%s；试读前台窗口没拿到标题" % note
            lines.append(
                "ui_semantics: %s %s"
                % ("ready" if provider.is_available() else "unavailable", note)
            )
        except Exception as exc:
            lines.append(f"ui_semantics: unavailable {exc}")
        # 角色画面：pywebview 在不在、系统里的 WebView2 运行时装没装。
        # 画面起不来时用户只会看到透明载体，自检这一行就是给他的一句准话。
        # 键名用 live2d_env，别和上面的配置开关 live2d_render 撞成两行同名的。
        try:
            from avatar import web2d

            lines.append("live2d_env: %s" % web2d.webview2_note())
        except Exception as exc:
            lines.append(f"live2d_env: unavailable {exc}")
        for line in lines:
            print(line)
        # 打包成窗口程序后看不到终端，所以自检结果同时写日志。
        logger.info("self check\n%s", "\n".join(lines))
        return 0

    if args.chat:
        bus = EventBus(logger=logger)
        agent, memory = build_agent(
            config, database, provider, logger, confirm_callback=cli_confirm, bus=bus
        )
        calendar_service = getattr(agent.capabilities, "calendar", None)
        if calendar_service is None:
            calendar_service = CalendarService(database)
            agent.capabilities.calendar = calendar_service
        scheduler = build_scheduler(
            config, memory, logger, bus, calendar=calendar_service,
            tasks=agent.task_store,
            store=ScheduledJobStore(database, logger=logger),
        )
        agent.attach_scheduler(scheduler)
        return chat_loop(agent, config, bus=bus, scheduler=scheduler)

    if not acquire_single_instance():
        logger.info("another instance is already running, exiting")
        if not focus_existing_window():
            notify_already_running()
        return 0

    start_startup_watchdog(logger, log_file=config.log_dir / "deskpet.log")
    return start_ui(config, database, provider, logger)


def recover_pending_tasks(agent, logger, limit=5):
    """启动时处理上次留下的任务：把中断的标成等用户，再列出待续的。"""
    store = getattr(agent, "task_store", None)
    if store is None:
        return []
    try:
        store.mark_interrupted()
    except Exception as exc:
        logger.warning("task interrupt mark failed: %s", exc)
    try:
        rows = store.unfinished(limit=limit)
    except Exception as exc:
        logger.warning("unfinished task read failed: %s", exc)
        return []
    if rows:
        logger.info("unfinished tasks: %s", "、".join(r.goal[:40] for r in rows))
    return rows


def safe_call(logger, label, fn, default=None):
    """可选子系统出错只记一条日志，不让整个桌宠起不来。"""
    try:
        return fn()
    except Exception as exc:
        logger.warning("%s failed, continuing: %s", label, exc)
        return default


def start_ui(config, database, provider, logger):
    """默认入口：启动桌面桌宠。"""
    bus = EventBus(logger=logger)
    confirmer = Confirmer()
    # 窗口状态：只看前台窗口是不是全屏，不读窗口内容。
    window = WindowsWindowProvider()
    agent, memory = build_agent(
        config, database, provider, logger, confirm_callback=confirmer, bus=bus, window=window
    )
    calendar_service = getattr(agent.capabilities, "calendar", None)
    if calendar_service is None:
        calendar_service = CalendarService(database)
        agent.capabilities.calendar = calendar_service
    scheduler = build_scheduler(
        config, memory, logger, bus, calendar=calendar_service,
        tasks=agent.task_store, todos=getattr(agent, "todo_store", None),
        store=ScheduledJobStore(database, logger=logger),
    )
    agent.attach_scheduler(scheduler)
    # V5：重启后把没到期的待办重新挂上提醒，已经错过的补一句，做完的不管。
    reminder = getattr(agent, "todo_reminder", None)
    if reminder is not None:
        safe_call(
            logger,
            "todo reminders",
            lambda: reminder.attach(scheduler).arm(),
            default=0,
        )
    calendar_reminder = CalendarReminder(calendar_service, logger=logger)
    try:
        calendar_reminder.attach(scheduler).arm()
        agent.calendar_reminder = calendar_reminder

        def on_calendar_change(action, event_id):
            scheduler.cancel_key_prefix("calendar:%s:" % event_id)
            calendar_reminder.forget_event(event_id)
            if action not in ("complete", "delete"):
                calendar_reminder.arm()
            return True

        calendar_service.on_change = on_calendar_change
    except Exception as exc:
        logger.warning("calendar reminders failed: %s", exc)
    # V3：上次没跑完的任务先摆出来给用户看，不自动重跑。
    pending_tasks = recover_pending_tasks(agent, logger)
    # 语音输入：当前默认没有麦克风，管线保持安静；以后换掉 provider 即可。
    voice = VoicePipeline(provider=NullVoiceProvider(), logger=logger)
    # 语音包：用户自己准备好的发声设置，只读元数据；没选就用系统默认发音。
    voice_pack_active = safe_call(
        logger,
        "voice pack",
        lambda: voice_pack.resolve(getattr(config, "voices_dir", ""), memory),
    )
    speech = SpeechPipeline(
        provider=build_speech(config, logger, pack=voice_pack_active), logger=logger
    )
    # 识别引擎：用户自己装了引擎并配了模型才真的可用，否则保持不可用并说明原因。
    stt = TranscribePipeline(
        provider=safe_call(logger, "stt provider", lambda: build_stt(config, logger))
        or NullSttProvider(),
        logger=logger,
    )
    # 音频采集：只有用户明确按下录音时才采一段，设备缺失时保持不可用。
    capture = safe_call(logger, "audio capture", lambda: build_capture(config, logger))
    # 唤醒词：没有唤醒模型时用文字匹配；麦克风状态保持关闭。
    wake = WakeWordDetector(provider=NullWakeWordProvider(), logger=logger)
    mic = MicPrivacy()
    # 用户活动感知：只看键盘鼠标空闲时间，不读内容，也不联网。
    activity = ActivityWatcher(provider=WindowsUserActivityProvider(), bus=bus, logger=logger)
    # 专注模式：全屏时降低存在感，退出后恢复。
    focus = FocusMode(provider=window, bus=bus, logger=logger)
    # 静默模式：不发声、不主动说话，开关记在记忆库里。
    quiet = QuietMode(preferences=memory, logger=logger)
    # 日记：启动时补写已经过完的日子，不轮询也不调用模型。
    safe_call(logger, "diary catch-up", memory.diary.ensure_recent)
    # 时间线与习惯：从真实数据补记「第一次」，推断活跃时段，都不调用模型。
    safe_call(logger, "timeline sync", lambda: memory.timeline.sync(memory))
    safe_call(logger, "habit refresh", memory.habit.refresh)
    safe_call(logger, "timeline first_start", lambda: memory.timeline.mark("first_start"))
    if config.voice_output:
        memory.timeline.mark("first_voice", detail="系统语音输出")
    if voice_pack_active is not None:
        memory.timeline.mark("first_voice_pack", detail=voice_pack_active.label())
    if getattr(config, "live2d_model", ""):
        memory.timeline.mark("first_live2d", detail=config.live2d_model)
    connected = agent.devices.connected_names()
    if connected:
        memory.timeline.mark("first_device", detail="、".join(connected))

    def _note_first_task(event):
        # 真正跑完一个需要确认的任务（中高风险工具）才算第一次完成任务。
        # 事件总线发的是 Event 对象，这里兼容字典写法，免得读到不存在的方法。
        data = getattr(event, "payload", event) or {}
        if data.get("ok") and str(data.get("level", "")).upper() in {"MEDIUM", "HIGH"}:
            memory.timeline.mark("first_task", detail=str(data.get("name", "")))

    bus.subscribe(bus_module.TOOL_COMPLETED, _note_first_task)
    # 隐私中心与权限中心：集中管理能不能听、能不能看、能不能主动说话。
    privacy = safe_call(
        logger,
        "privacy center",
        lambda: PrivacyCenter(preferences=memory, config=config, mic=mic, logger=logger),
    )
    # 隐私中心建好之后，把「屏幕内容能不能给模型」这条规则交给 Agent。
    # Agent Core 只调一个回调，不认识隐私中心本身。
    if privacy is not None:
        from tools.screen import screen_model_policy

        agent.screen_policy = screen_model_policy(privacy, config)
    # 环境感知：只读电量与内存，成本低；电量低时由规则提醒，不轮询模型。
    environment = EnvironmentWatcher(
        provider=WindowsEnvironmentProvider(), bus=bus, logger=logger
    )
    # 屏幕感知：只读屏幕数量与分辨率，不做截图；布局变了就把桌宠挪回看得见的地方。
    screen = ScreenWatcher(provider=WindowsScreenProvider(), bus=bus, logger=logger)
    # 视觉：默认不截屏。用户开启「截图分析」后，也只在他手动点一下时截一张，
    # 存在数据目录里，不上传；没有 OCR 引擎就如实说读不了字。
    vision = safe_call(
        logger,
        "vision service",
        lambda: build_vision_service(config, privacy=privacy, bus=bus, logger=logger),
    ) or NullVisionProvider()
    # 屏幕感知：默认关闭。开了以后只读前台应用的进程名与窗口标题，不读画面。
    awareness = safe_call(
        logger,
        "awareness service",
        lambda: build_awareness_service(
            preferences=memory, privacy=privacy, bus=bus, logger=logger, vision=vision
        ),
    )
    # 视觉检测事件层：没接检测源时保持安静，看到东西才发事件。
    detection = DetectionWatcher(provider=NullDetectionProvider(), bus=bus, logger=logger)
    # 实体身体：把情绪和视觉事件转成设备动作，只经 DeviceManager。
    body = PhysicalBody(devices=agent.devices, logger=logger)
    body.attach(bus)
    if agent.behavior is not None:
        agent.behavior.quiet = quiet
    logger.info("starting desktop ui")
    return launch(
        agent,
        config,
        memory=memory,
        bus=bus,
        scheduler=scheduler,
        logger=logger,
        confirmer=confirmer,
        behavior=agent.behavior,
        voice=voice,
        speech=speech,
        stt=stt,
        wake=wake,
        mic=mic,
        activity=activity,
        focus=focus,
        quiet=quiet,
        environment=environment,
        screen=screen,
        vision=vision,
        awareness=awareness,
        detection=detection,
        body=body,
        privacy=privacy,
        pending_tasks=pending_tasks,
        capture=capture,
        knowledge=getattr(agent, "knowledge", None),
        worklog=getattr(agent, "worklog", None),
    )


def run(argv=None):
    """EXE 与终端都走这里：任何启动异常都要变成人话，而不是闪退。"""
    configure_console_encoding()
    try:
        return main(argv)
    except SystemExit:
        raise
    except BaseException as exc:
        report_fatal(exc)
        return 1


def configure_console_encoding():
    """中文 Windows 的终端默认不是 UTF-8，统一成 UTF-8，避免中文变乱码。"""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            continue


if __name__ == "__main__":
    raise SystemExit(run())
