"""阶段 E 验收：可靠性、安全与打包。

覆盖：程序目录与用户数据目录分离（从快捷方式启动也不迷路）、打包边界
（.env / 数据库 / 日志 / 密钥不进发布包）、退出流程正确释放资源且可重复调用、
后台线程不直接碰 Tkinter 控件、启动失败只给人话而 traceback 只进日志、
数据库迁移可重复执行、没有任何模型时仍能启动并进入本地能力。
"""

import inspect
import io
import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.context import ContextManager
from agent.core import AgentCore
from app import main as app_main
from database.db import Database
from logkit.logger import configure, register_secret
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from model.base import ChatDelta, ChatResult, ModelProvider, Usage
from security import config as config_module
from security.config import Config, data_root, resource_path
from security.privacy import PrivacyCenter
from ui.desktop import DesktopPet
from voice.manager import SpeechPipeline, VoicePipeline
from voice.stt import NullSttProvider
from voice.tts import NullSpeechProvider as NullSpeech
from voice.wake import MicPrivacy, WakeWordDetector

MODEL_ENV_NAMES = (
    "DESKPET_LLM_API_KEY",
    "AGENT_LLM_API_KEY",
    "OPENAI_API_KEY",
    "PY_LLM_API_KEY",
    "DESKPET_LLM_BASE_URL",
    "AGENT_LLM_BASE_URL",
    "OPENAI_BASE_URL",
    "AGENT_LLM_MODEL",
    "DESKPET_LLM_MODEL",
    "DESKPET_COMPANION_API_KEY",
    "DESKPET_COMPANION_BASE_URL",
    "DESKPET_COMPANION_MODEL",
    "DESKPET_TASK_API_KEY",
    "DESKPET_TASK_BASE_URL",
    "DESKPET_TASK_MODEL",
)


def no_model_env(root):
    """清空所有模型相关的环境变量，只留下用户数据目录。"""
    env = {name: "" for name in MODEL_ENV_NAMES}
    env["DESKPET_ROOT"] = str(root)
    return env


def close_logger_handlers():
    """关掉全局日志 handler，避免 Windows 上临时目录删不掉。"""
    logger = logging.getLogger("deskpet")
    for handler in list(logger.handlers):
        try:
            handler.close()
        except Exception:
            pass
        logger.removeHandler(handler)


class StubProvider(ModelProvider):
    name = "stub"

    def __init__(self, available=True, reply="在的。"):
        self.available = available
        self.reply = reply
        self.calls = 0

    def is_available(self):
        return self.available

    def chat(self, messages, tools=None, temperature=0.7):
        self.calls += 1
        return ChatResult(
            ok=True,
            content=self.reply,
            usage=Usage(prompt_tokens=1, completion_tokens=1),
            model="stub",
        )

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


class ListLogger:
    """只记在内存里的假日志，方便断言。"""

    def __init__(self):
        self.lines = []

    def warning(self, *args):
        self.lines.append(args)

    def error(self, *args):
        self.lines.append(args)

    def info(self, *args):
        pass

    def debug(self, *args):
        pass

    def text(self):
        return " ".join(str(item) for line in self.lines for item in line)


class CountingStop:
    """一起数 stop / off 的替身，用来验证退出时资源真的被关掉。"""

    def __init__(self, broken=False):
        self.stops = 0
        self.offs = 0
        self.broken = broken

    def stop(self):
        if self.broken:
            raise RuntimeError("关不掉了")
        self.stops += 1
        return True

    def off(self):
        self.offs += 1
        return True

    def say(self, text, emotion=None):
        if self.broken:
            raise RuntimeError("发声坏了")
        return True

    def available(self):
        return True

    def poll(self):
        return None


# ------------------------------------------------------------ 路径分离
class PathSeparationTests(unittest.TestCase):
    """程序目录和用户数据目录必须分得清清楚楚。"""

    def test_data_root_follows_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"DESKPET_ROOT": tmp}):
                config = Config(load_env=False)
                self.assertEqual(config.data_root, Path(tmp).resolve())
                self.assertEqual(config.log_dir, Path(tmp).resolve() / "logs")

    def test_default_paths_live_under_the_data_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"DESKPET_ROOT": tmp}):
                config = Config(load_env=False)
                root = Path(tmp).resolve()
                for path in (
                    config.data_dir,
                    config.log_dir,
                    config.db_path,
                    config.voices_dir,
                    config.devices_dir,
                    config.characters_dir,
                    config.live2d_dir,
                ):
                    self.assertTrue(str(path).startswith(str(root)), path)
                self.assertEqual(config.db_path.parent, config.data_dir)

    def test_relative_override_is_anchored_to_the_data_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"DESKPET_ROOT": tmp, "DESKPET_DB_PATH": "mine/x.sqlite3"}
            with mock.patch.dict(os.environ, env):
                config = Config(load_env=False)
                self.assertEqual(
                    config.db_path, Path(tmp).resolve() / "mine" / "x.sqlite3"
                )

    def test_absolute_override_wins(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
            env = {
                "DESKPET_ROOT": tmp,
                "DESKPET_DB_PATH": str(Path(other) / "a.sqlite3"),
            }
            with mock.patch.dict(os.environ, env):
                config = Config(load_env=False)
                self.assertEqual(config.db_path, Path(other).resolve() / "a.sqlite3")

    def test_cwd_does_not_change_the_paths(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as elsewhere:
            with mock.patch.dict(os.environ, {"DESKPET_ROOT": tmp}):
                before = Config(load_env=False)
                old = os.getcwd()
                os.chdir(elsewhere)
                try:
                    after = Config(load_env=False)
                finally:
                    os.chdir(old)
            self.assertEqual(before.data_dir, after.data_dir)
            self.assertEqual(before.db_path, after.db_path)

    def test_development_data_root_is_the_project_dir(self):
        with mock.patch.dict(os.environ, {"DESKPET_ROOT": ""}):
            config = Config(load_env=False)
            self.assertEqual(config.data_root, ROOT)
            self.assertEqual(config.project_root, ROOT)

    def test_frozen_data_root_uses_local_appdata(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"LOCALAPPDATA": tmp, "DESKPET_ROOT": ""}
            with mock.patch.dict(os.environ, env), mock.patch.object(
                sys, "frozen", True, create=True
            ):
                self.assertEqual(data_root(ROOT), Path(tmp).resolve() / "DeskPet")

    def test_frozen_root_ignores_cwd_and_program_dir(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as elsewhere:
            env = {"LOCALAPPDATA": tmp, "DESKPET_ROOT": ""}
            with mock.patch.dict(os.environ, env), mock.patch.object(
                sys, "frozen", True, create=True
            ):
                old = os.getcwd()
                os.chdir(elsewhere)
                try:
                    root = data_root(Path(elsewhere))
                finally:
                    os.chdir(old)
            self.assertEqual(root, Path(tmp).resolve() / "DeskPet")
            self.assertNotEqual(root, Path(elsewhere).resolve())

    def test_user_override_beats_the_frozen_default(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as mine:
            env = {"LOCALAPPDATA": tmp, "DESKPET_ROOT": mine}
            with mock.patch.dict(os.environ, env), mock.patch.object(
                sys, "frozen", True, create=True
            ):
                self.assertEqual(data_root(ROOT), Path(mine).resolve())

    def test_frozen_data_root_is_readable_and_writable(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"DESKPET_ROOT": tmp}
            with mock.patch.dict(os.environ, env):
                config = Config(load_env=False)
                config.ensure_dirs()
                probe = config.data_dir / "probe.txt"
                probe.write_text("ok", encoding="utf-8")
                self.assertEqual(probe.read_text(encoding="utf-8"), "ok")

    def test_resource_path_uses_meipass_when_frozen(self):
        with mock.patch.object(sys, "_MEIPASS", str(Path("C:/tmp/meipass")), create=True):
            self.assertEqual(
                resource_path("deskpet.ico"),
                Path("C:/tmp/meipass") / "resources" / "deskpet.ico",
            )

    def test_resource_path_uses_project_dir_in_development(self):
        self.assertEqual(resource_path("deskpet.ico"), ROOT / "resources" / "deskpet.ico")

    def test_ensure_dirs_creates_the_folders_the_user_needs(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"DESKPET_ROOT": tmp}):
                config = Config(load_env=False)
                config.ensure_dirs()
                for path in (
                    config.data_dir,
                    config.log_dir,
                    config.voices_dir,
                    config.devices_dir,
                    config.live2d_dir,
                ):
                    self.assertTrue(path.is_dir(), path)

    def test_dotenv_never_overwrites_an_existing_variable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("""DESKPET_TEST_MARK=from-file
# 注释行
这一行不是键值对
""", encoding="utf-8")
            with mock.patch.dict(os.environ, {"DESKPET_TEST_MARK": "from-env"}):
                config_module.load_dotenv(path)
                self.assertEqual(os.environ["DESKPET_TEST_MARK"], "from-env")
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("DESKPET_TEST_MARK", None)
                config_module.load_dotenv(path)
                self.assertEqual(os.environ.get("DESKPET_TEST_MARK"), "from-file")

    def test_user_data_env_is_loaded_when_it_lives_elsewhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"DESKPET_ROOT": tmp}
            with mock.patch.dict(os.environ, env), mock.patch.object(
                sys, "frozen", True, create=True
            ), mock.patch.object(
                config_module, "load_dotenv", return_value=False
            ) as loader:
                Config()
            called = [Path(call.args[0]) for call in loader.call_args_list if call.args]
            self.assertIn(Path(tmp).resolve() / ".env", called)


# ------------------------------------------------------------ 打包边界
class PackagingBoundaryTests(unittest.TestCase):
    def spec_text(self):
        return (ROOT / "Blossom.spec").read_text(encoding="utf-8")

    def build_text(self):
        return (ROOT / "build.ps1").read_text(encoding="utf-8")

    def test_spec_entry_is_the_single_application_entry(self):
        spec = self.spec_text()
        self.assertIn('"app" / "main.py"', spec)
        self.assertEqual(spec.count("Analysis("), 1)

    def datas_lines(self):
        """只读资源清单那几段（图标 + Live2D 运行资源 + 随包角色 + 自带技能库），不含用户数据。"""
        spec = self.spec_text()
        start = spec.index("DATAS = [")
        return spec[start: spec.index("EXCLUDES = [")]

    def test_spec_bundles_only_program_resources(self):
        lines = self.datas_lines()
        self.assertIn('DATAS = [(str(ICON), "resources")]', lines)
        self.assertIn('(str(LIVE2D_ASSETS), "resources/live2d")', lines)
        self.assertIn('(str(LIVE2D_MODELS), "resources/live2d_models")', lines)
        self.assertIn("for _path in sorted(SKILL_LIBRARY.rglob", lines)
        self.assertIn('__pycache__', lines, "技能库不能带开发缓存")
        self.assertIn('_path.suffix == ".pyc"', lines, "技能库不能带字节码")
        self.assertIn("for _path in sorted(FIRMWARE.rglob", lines)
        self.assertIn('"build" in _path.parts', lines, "编译产物不该进发行包")
        # 只读资源就这四样：Live2D 运行文件 + 随包角色 + 自带技能库 + ESP32 固件工程，
        # 都是程序自带的，一样都不来自用户目录。
        self.assertEqual(lines.count("DATAS.append"), 4)

    def test_spec_never_bundles_user_data(self):
        lines = self.datas_lines()
        for needle in (".env", "sqlite", "logs", "characters", "backups", "deskpet.sqlite3"):
            self.assertNotIn(needle, lines)

    def test_spec_is_a_windowed_app(self):
        spec = self.spec_text()
        self.assertIn("console=False", spec)
        self.assertIn("debug=False", spec)

    def test_spec_and_shortcut_share_one_icon(self):
        self.assertIn('ICON = ROOT / "resources" / "deskpet.ico"', self.spec_text())
        self.assertIn("icon=str(ICON)", self.spec_text())
        self.assertIn('$shortcut.IconLocation = "$exePath,0"', self.build_text())

    def test_gitignore_keeps_data_and_artifacts_out_of_the_repo(self):
        lines = {
            line.strip()
            for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        }
        for entry in (
            ".env",
            "data/",
            "logs/",
            "dist/",
            "build/",
            "backups/",
            "characters/",
            "live2d/",
            "__pycache__/",
        ):
            self.assertIn(entry, lines)

    def test_build_script_never_overwrites_the_user_env(self):
        build = self.build_text()
        guards = [
            line
            for line in build.splitlines()
            if "Test-Path" in line and '(Join-Path $target ".env")' in line
        ]
        self.assertTrue(guards, "缺少「已存在就不覆盖」的判断")
        copies = [
            line
            for line in build.splitlines()
            if '-Destination (Join-Path $target ".env")' in line
        ]
        self.assertTrue(copies)
        for line in copies:
            self.assertNotIn("-Force", line)

    def test_build_script_points_the_shortcut_at_the_exe_directory(self):
        build = self.build_text()
        self.assertIn('$shortcut.TargetPath = $exePath', build)
        self.assertIn("$shortcut.WorkingDirectory = Split-Path -Parent $exePath", build)
        self.assertIn("Blossom.lnk", build)
        # 改名之后旧快捷方式要收掉，免得桌面上两个图标指向两个包。
        self.assertIn("DeskPet.lnk", build)

    def test_build_script_can_rebuild_only_the_shortcut(self):
        build = self.build_text()
        self.assertIn("$ShortcutOnly", build)
        self.assertIn("if (-not $ShortcutOnly)", build)

    def test_env_example_leaves_every_secret_blank(self):
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        secret_keys = (
            "DESKPET_LLM_API_KEY",
            "AGENT_LLM_API_KEY",
            "OPENAI_API_KEY",
            "PY_LLM_API_KEY",
            "DESKPET_NEWS_API_KEY",
            "DESKPET_COMPANION_API_KEY",
            "DESKPET_TASK_API_KEY",
        )
        seen = set()
        for line in text.splitlines():
            if "=" not in line:
                continue
            name = line.split("=", 1)[0].strip()
            if name in secret_keys:
                seen.add(name)
                self.assertEqual(line.split("=", 1)[1].strip(), "", line)
        self.assertTrue(seen, "配置模板里至少要出现密钥项")

    def test_release_folder_carries_no_real_key(self):
        folder = ROOT / "dist" / "Blossom"
        if not folder.is_dir():
            self.skipTest("还没有打包过")
        env_files = list(folder.glob(".env*"))
        self.assertTrue(env_files, "发布包里应该带一份空的配置模板")
        pattern = re.compile(r"sk-[A-Za-z0-9_-]{16,}")
        for path in env_files:
            text = path.read_text(encoding="utf-8", errors="ignore")
            self.assertIsNone(pattern.search(text), path.name)
            for line in text.splitlines():
                name, _, value = line.partition("=")
                if name.strip().endswith("API_KEY"):
                    self.assertEqual(value.strip(), "", f"{path.name}: {name}")

    def test_release_folder_keeps_user_data_outside(self):
        folder = ROOT / "dist" / "Blossom"
        if not folder.is_dir():
            self.skipTest("还没有打包过")
        self.assertFalse((folder / "data").exists())
        self.assertFalse((folder / "logs").exists())
        self.assertFalse((folder / "characters").exists())

    def test_nothing_derives_a_data_path_from_the_working_directory(self):
        for path in (ROOT / "app").rglob("*.py"):
            self.assertNotIn("os.getcwd()", path.read_text(encoding="utf-8"), path.name)
        for path in (ROOT / "security").rglob("*.py"):
            self.assertNotIn("os.getcwd()", path.read_text(encoding="utf-8"), path.name)

    def test_main_uses_its_own_path_only_for_the_development_import(self):
        text = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        own_path_lines = [line for line in text.splitlines() if "__file__" in line]
        self.assertEqual(len(own_path_lines), 1)
        self.assertIn("frozen", text)


# ------------------------------------------------------------ 启动失败
class StartupFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(close_logger_handlers)
        self.env = mock.patch.dict(
            os.environ, {"DESKPET_ROOT": str(Path(self.tmp.name))}
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def log_file(self):
        return Path(self.tmp.name) / "logs" / "startup-error.log"

    def test_database_problems_are_explained_in_plain_words(self):
        for exc in (
            sqlite3.OperationalError("unable to open database file"),
            OSError("拒绝访问"),
            PermissionError("denied"),
        ):
            message = app_main.friendly_error(exc)
            self.assertIn("数据", message)
            self.assertNotIn("Traceback", message)

    def test_unknown_problems_still_get_one_plain_sentence(self):
        message = app_main.friendly_error(ValueError("内部错误"))
        self.assertEqual(message, "桌宠启动失败，请查看日志文件了解详情。")
        self.assertNotIn("Traceback", message)

    def test_startup_error_writes_the_traceback_to_the_log_only(self):
        shown = []
        try:
            raise ValueError("找不到模型配置")
        except ValueError as exc:
            captured = exc
        with mock.patch.object(
            app_main.ctypes, "windll", mock.MagicMock(), create=True
        ) as windll, mock.patch.object(app_main.os, "name", "nt"):
            windll.user32.MessageBoxW.side_effect = lambda h, m, t, f: shown.append(m)
            app_main.report_fatal(captured)
        logged = self.log_file().read_text(encoding="utf-8")
        self.assertIn("Traceback", logged)
        self.assertIn("找不到模型配置", logged)
        self.assertTrue(shown)
        self.assertNotIn("Traceback", shown[0])

    def test_startup_error_never_writes_a_secret_to_the_log(self):
        register_secret("sk-startup-secret-123456")
        with mock.patch.object(
            app_main.ctypes, "windll", mock.MagicMock(), create=True
        ) as windll, mock.patch.object(app_main.os, "name", "nt"):
            windll.user32.MessageBoxW.side_effect = lambda h, m, t, f: None
            app_main.report_fatal(
                RuntimeError("连接失败 key=sk-startup-secret-123456")
            )
        logged = self.log_file().read_text(encoding="utf-8")
        self.assertNotIn("sk-startup-secret-123456", logged)
        self.assertIn("***", logged)

    def test_startup_error_survives_when_the_log_folder_is_unusable(self):
        # 配置都读不出来时也必须活着：不能抛异常，也不能弹出真窗口卡住测试。
        with mock.patch.object(app_main, "Config", side_effect=RuntimeError("没配置")), \
                mock.patch.object(app_main, "_message_box", return_value=True) as box:
            app_main.report_fatal(ValueError("随便什么错"))
        self.assertTrue(box.called)
        self.assertIn("启动失败", box.call_args[0][0])

    def test_run_turns_a_crash_into_a_friendly_exit_code(self):
        with mock.patch.object(app_main, "main", side_effect=ValueError("炸了")), mock.patch.object(
            app_main, "report_fatal"
        ) as reporter:
            code = app_main.run([])
        self.assertEqual(code, 1)
        reporter.assert_called_once()

    def test_run_never_swallows_system_exit(self):
        with mock.patch.object(app_main, "main", side_effect=SystemExit(3)):
            with self.assertRaises(SystemExit):
                app_main.run([])


# ------------------------------------------------------------ 线程安全
class TkTestCase(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except Exception as exc:
            self.skipTest(f"tkinter 不可用：{exc}")
        self.root.withdraw()
        self.addCleanup(self._safe_destroy)
        self.addCleanup(close_logger_handlers)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Config(load_env=False)
        base = Path(self.tmp.name)
        self.config.data_dir = base
        self.config.log_dir = base / "logs"
        self.config.db_path = base / "t.sqlite3"
        self.config.voices_dir = base / "voices"
        self.config.characters_dir = base / "characters"
        self.config.live2d_dir = base / "live2d"
        self.config.devices_dir = base / "devices"
        self.config.tray = False

    def _safe_destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def build_pet(self, provider=None, **kwargs):
        database = Database(Path(self.tmp.name) / "t.sqlite3")
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        agent = AgentCore(
            provider or StubProvider(), memory, ContextManager(token_budget=800)
        )
        pet = DesktopPet(
            agent,
            self.config,
            memory=memory,
            voice=VoicePipeline(provider=NullVoice()),
            speech=SpeechPipeline(provider=NullSpeech()),
            stt=NullSttProvider(),
            wake=WakeWordDetector(),
            mic=MicPrivacy(),
            privacy=PrivacyCenter(preferences=memory, config=self.config),
            root=self.root,
            **kwargs,
        )
        return pet, memory


class NullVoice:
    """没有麦克风时的最小语音替身。"""

    available_flag = False

    def is_available(self):
        return False

    def start(self):
        return False

    def stop(self):
        return True

    def listen(self, timeout=None):
        return None


class ThreadSafetyTests(TkTestCase):
    def test_background_workers_only_post_to_the_ui(self):
        banned = (
            "self.root.",
            "self.chat.",
            "self.card.",
            "self.dev.",
            "self.host.",
            "self.transcript.",
        )
        for name in ("_worker_reply", "_worker_proactive"):
            source = inspect.getsource(getattr(DesktopPet, name))
            for needle in banned:
                self.assertNotIn(needle, source, f"{name} 直接碰了控件：{needle}")

    def test_post_runs_the_action_on_the_main_thread(self):
        pet, _ = self.build_pet()
        seen = {}
        pet.post(lambda: seen.setdefault("thread", threading.get_ident()))
        pet._drain()
        self.assertEqual(seen["thread"], threading.get_ident())

    def test_worker_reply_does_not_touch_widgets_off_thread(self):
        pet, _ = self.build_pet()
        before = pet.chat.history_text()
        worker = threading.Thread(target=pet._worker_reply, args=("你好",))
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(pet.chat.history_text(), before)
        pet._drain()
        self.assertIn("在的。", pet.chat.history_text())

    def test_the_ui_stays_untouched_until_the_main_thread_drains(self):
        pet, _ = self.build_pet()
        pet._set_busy(True)
        worker = threading.Thread(target=pet._worker_reply, args=("在吗",))
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertTrue(pet._busy)
        self.assertEqual(pet.chat.state.cget("text"), "思考中……")
        pet._drain()
        self.assertFalse(pet._busy)
        self.assertIn("在的。", pet.chat.history_text())

    def test_drain_keeps_going_after_a_failing_action(self):
        pet, _ = self.build_pet()
        pet.logger = ListLogger()
        pet.post(lambda: 1 / 0)
        pet.post(lambda: pet._append_message("桌宠", "第二件事还做", "pet"))
        pet._drain()
        self.assertIn("第二件事还做", pet.chat.history_text())
        self.assertIn("ui action failed", pet.logger.text())

    def test_permission_dialog_runs_on_the_ui_thread(self):
        pet, _ = self.build_pet()
        idents = {}
        result = {}

        def fake_ask(*args, **kwargs):
            idents["thread"] = threading.get_ident()
            return True

        with mock.patch("ui.desktop.messagebox.askyesno", side_effect=fake_ask):
            worker = threading.Thread(
                daemon=True,
                target=lambda: result.setdefault("ok", pet.confirm("x", "HIGH", "原因")),
            )
            pet.root.after(1, pet._drain)
            worker.start()
            deadline = time.time() + 5
            while worker.is_alive() and time.time() < deadline:
                self.root.update()
                time.sleep(0.01)
            worker.join(1)
        self.assertTrue(result.get("ok"))
        self.assertEqual(idents.get("thread"), threading.get_ident())


# ------------------------------------------------------------ 退出流程
class ExitFlowTests(TkTestCase):
    def pet_with_stops(self, **kwargs):
        pet, _ = self.build_pet(**kwargs)
        pet.tray = CountingStop()
        pet.speech = CountingStop()
        pet.voice = CountingStop()
        pet.stt = CountingStop()
        pet.wake = CountingStop()
        pet.mic = CountingStop()
        pet.scheduler = CountingStop()
        return pet

    def test_close_releases_every_background_piece(self):
        pet = self.pet_with_stops()
        self.assertTrue(pet.close())
        for name in ("tray", "speech", "voice", "stt", "wake", "scheduler"):
            self.assertEqual(getattr(pet, name).stops, 1, name)
        self.assertEqual(pet.mic.offs, 1)

    def test_close_is_safe_to_call_twice(self):
        pet = self.pet_with_stops()
        pet.close()
        pet.close()
        self.assertEqual(pet.tray.stops, 1)

    def test_one_broken_piece_does_not_block_the_others(self):
        pet = self.pet_with_stops()
        pet.speech = CountingStop(broken=True)
        pet.close()
        self.assertEqual(pet.tray.stops, 1)
        self.assertEqual(pet.voice.stops, 1)

    def test_close_saves_the_position_before_leaving(self):
        pet = self.pet_with_stops()
        saved = []
        pet._save_position = lambda: saved.append(True)
        pet.close()
        self.assertTrue(saved)

    def test_close_writes_a_shutdown_line(self):
        pet = self.pet_with_stops()
        logger = configure(Path(self.tmp.name) / "logs", "INFO", dev_mode=False)
        pet.logger = logger
        pet.close()
        logged = (Path(self.tmp.name) / "logs" / "deskpet.log").read_text(
            encoding="utf-8"
        )
        self.assertIn("deskpet ui closing", logged)

    def test_close_releases_the_database_file(self):
        pet, _ = self.build_pet()
        pet.close()
        database = Path(self.tmp.name) / "t.sqlite3"
        moved = database.with_name("t.moved.sqlite3")
        database.rename(moved)
        self.assertTrue(moved.exists())


# ------------------------------------------------------------ 数据库
class DatabaseResilienceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_migrate_can_run_twice_without_losing_rows(self):
        database = Database(Path(self.tmp.name) / "a.sqlite3")
        first = database.migrate()
        self.assertTrue(first)
        repository = MemoryRepository(database)
        repository.add_memory("记住这句话", kind="long_term")
        second = database.migrate()
        self.assertEqual(second, [])
        self.assertEqual(repository.count_memories(), 1)
        self.assertEqual(
            repository.browse()[0]["content"], "记住这句话"
        )

    def test_health_reports_path_version_and_tables(self):
        path = Path(self.tmp.name) / "h.sqlite3"
        database = Database(path)
        database.migrate()
        health = database.health()
        self.assertEqual(Path(health["path"]), path)
        self.assertTrue(health["sqlite_version"])
        self.assertIn("memories", health["tables"])
        self.assertIn("schema_migrations", health["tables"])

    def test_a_new_connection_still_sees_the_old_rows(self):
        path = Path(self.tmp.name) / "r.sqlite3"
        database = Database(path)
        database.migrate()
        MemoryRepository(database).add_memory("跨连接也要看得到")
        reopened = MemoryRepository(Database(path))
        self.assertEqual(reopened.count_memories(), 1)

    def test_unusable_database_becomes_a_plain_data_message(self):
        blocked = Database(Path(self.tmp.name))
        with self.assertRaises(sqlite3.Error):
            blocked.migrate()
        self.assertIn("数据", app_main.friendly_error(sqlite3.OperationalError("x")))


# ------------------------------------------------------------ 降级
class DegradeTests(TkTestCase):
    def test_pet_builds_with_every_optional_module_missing(self):
        database = Database(Path(self.tmp.name) / "n.sqlite3")
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        agent = AgentCore(
            StubProvider(available=False), memory, ContextManager()
        )
        pet = DesktopPet(agent, self.config, memory=memory, root=self.root)
        self.assertIsNone(pet.voice)
        self.assertIsNone(pet.speech)
        self.assertIsNone(pet.scheduler)
        self.assertTrue(pet.close())

    def test_missing_icon_does_not_break_startup(self):
        pet, _ = self.build_pet()
        missing = Path(self.tmp.name) / "没有这个.ico"
        with mock.patch("ui.desktop.resource_path", return_value=missing):
            self.assertIsNone(pet._set_window_icon())

    def test_voice_center_works_without_a_voice_pack_folder(self):
        pet, _ = self.build_pet()
        rows = dict(pet.voice_center_rows())
        self.assertIn("语音包", rows)
        self.assertIsNone(pet.current_voice_pack())

    def test_ui_errors_are_logged_and_shown_in_plain_words(self):
        pet, _ = self.build_pet()
        pet.logger = ListLogger()
        detail = pet._on_ui_error(ValueError, ValueError("回调炸了"), None)
        self.assertIn("回调炸了", detail)
        self.assertIn("回调炸了", pet.logger.text())
        self.assertIn("日志", pet.chat.state.cget("text"))
        self.assertNotIn("Traceback", pet.chat.state.cget("text"))

    def test_the_ui_error_handler_is_installed(self):
        pet, _ = self.build_pet()
        self.assertEqual(pet.root.report_callback_exception, pet._on_ui_error)

    def test_a_failing_speech_engine_does_not_break_the_reply(self):
        pet, _ = self.build_pet()
        pet.speech = CountingStop(broken=True)
        self.assertFalse(pet._say("喂"))


class NoModelStartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(close_logger_handlers)

    def run_check(self, extra=None):
        env = no_model_env(Path(self.tmp.name))
        if extra:
            env.update(extra)
        buffer = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(
            config_module, "load_dotenv", return_value=False
        ), redirect_stdout(buffer):
            code = app_main.main(["--check"])
        return code, buffer.getvalue()

    def test_check_runs_without_any_model(self):
        code, output = self.run_check()
        self.assertEqual(code, 0)
        self.assertIn("companion_model: unconfigured", output)
        self.assertIn("task_model: unconfigured", output)

    def test_check_creates_the_database_and_logs_under_the_user_data_root(self):
        code, output = self.run_check()
        self.assertEqual(code, 0)
        root = Path(self.tmp.name)
        self.assertTrue((root / "data" / "deskpet.sqlite3").is_file())
        self.assertTrue((root / "logs" / "deskpet.log").is_file())
        self.assertIn(str(root), output)

    def test_check_never_prints_a_secret(self):
        secret = "sk-news-secret-1234567890"
        code, output = self.run_check({"DESKPET_NEWS_API_KEY": secret})
        self.assertEqual(code, 0)
        self.assertNotIn(secret, output)
        self.assertIn("news_api_key: set", output)

    def test_check_reports_the_character_screen_stack(self):
        """画面起不来时用户只看到占位形象，自检得给他一句准话。"""
        code, output = self.run_check()
        self.assertEqual(code, 0)
        # 键名不能和配置开关 live2d_render 撞车，否则输出里两行同名、看着像自相矛盾。
        self.assertIn("live2d_env:", output)
        detail = [line for line in output.splitlines() if line.startswith("live2d_env:")][0]
        self.assertTrue(detail.split(":", 1)[1].strip())


if __name__ == "__main__":
    unittest.main()
