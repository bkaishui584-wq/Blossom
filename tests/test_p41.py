"""阶段 D 验收：桌面交互、语音和配置中心。

覆盖：语音包导入校验（只读元数据、不执行代码）、麦克风开关真的停采集、
托盘与全局快捷键的降级与命令分发、开机启动注册表开关、语音配置中心的读数、
本地能力不依赖模型，以及任务确认流程保持不变。
"""

import json
import sys
import tempfile
import tkinter as tk
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
from security import startup as startup_control
from security.config import Config
from security.privacy import PrivacyCenter
from tools.permission import PermissionLevel, PermissionManager
from ui import settings_view
from ui import tray as tray_module
from ui.desktop import DesktopPet
from ui.tray import TrayIcon, menu_map, parse_hotkey, tooltip_text
from voice import pack as voice_pack
from voice.interface import VoiceProvider
from voice.manager import SpeechPipeline, VoicePipeline
from voice.stt import NullSttProvider
from voice.tts import NullSpeechProvider as NullSpeech
from voice.wake import MicPrivacy, WakeWordDetector


def make_memory(tmp, **kwargs):
    database = Database(Path(tmp) / "t.sqlite3")
    database.migrate()
    return MemoryManager(MemoryRepository(database), **kwargs), database


def write_pack(folder, manifest):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / voice_pack.MANIFEST).write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return folder


GOOD = {"id": "xiaozhuo", "name": "小桌声音", "version": "1.0", "engine": "sapi"}


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
            ok=True, content=self.reply, usage=Usage(prompt_tokens=5, completion_tokens=3),
            model="stub",
        )

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


class FakeVoice(VoiceProvider):
    name = "fake"

    def __init__(self, available=True):
        self.available_flag = available
        self.listening = False
        self.stopped = 0

    def is_available(self):
        return self.available_flag

    def start(self):
        self.listening = True
        return True

    def stop(self):
        self.listening = False
        self.stopped += 1
        return True

    def listen(self, timeout=None):
        return None


# ---------------------------------------------------------------- 语音包
class VoicePackManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_valid_manifest_loads(self):
        folder = write_pack(Path(self.tmp.name) / "a", GOOD)
        pack = voice_pack.load(folder)
        self.assertIsNotNone(pack)
        self.assertEqual(pack.id, "xiaozhuo")
        self.assertEqual(pack.label(), "小桌声音（1.0）")

    def test_rate_and_volume_are_clamped(self):
        folder = write_pack(
            Path(self.tmp.name) / "a", dict(GOOD, rate=99, volume=-5)
        )
        pack = voice_pack.load(folder)
        self.assertEqual(pack.rate, 10)
        self.assertEqual(pack.volume, 0)

    def test_unknown_engine_is_refused(self):
        folder = write_pack(Path(self.tmp.name) / "a", dict(GOOD, engine="python"))
        self.assertIsNone(voice_pack.load(folder))

    def test_missing_or_bad_id_is_refused(self):
        for manifest in ({}, {"id": ""}, {"id": "有 空格"}, {"id": "../逃"}):
            folder = write_pack(Path(self.tmp.name) / "x", manifest)
            self.assertIsNone(voice_pack.load(folder), manifest)

    def test_broken_json_and_non_dict_are_refused(self):
        folder = Path(self.tmp.name) / "a"
        folder.mkdir()
        (folder / voice_pack.MANIFEST).write_text("{oops", encoding="utf-8")
        self.assertIsNone(voice_pack.load(folder))
        (folder / voice_pack.MANIFEST).write_text("[1, 2]", encoding="utf-8")
        self.assertIsNone(voice_pack.load(folder))

    def test_oversized_manifest_is_refused(self):
        folder = Path(self.tmp.name) / "a"
        folder.mkdir()
        big = dict(GOOD, note="x" * (voice_pack.MAX_MANIFEST_BYTES + 10))
        (folder / voice_pack.MANIFEST).write_text(json.dumps(big), encoding="utf-8")
        self.assertIsNone(voice_pack.load(folder))

    def test_missing_folder_is_refused(self):
        self.assertIsNone(voice_pack.load(Path(self.tmp.name) / "nope"))
        self.assertEqual(voice_pack.scan(Path(self.tmp.name) / "nope"), [])

    def test_scan_and_find(self):
        root = Path(self.tmp.name) / "voices"
        write_pack(root / "a", GOOD)
        write_pack(root / "b", dict(GOOD, id="other", name="另一个"))
        write_pack(root / "broken", {"id": "坏"})
        packs = voice_pack.scan(root)
        self.assertEqual([pack.id for pack in packs], ["xiaozhuo", "other"])
        self.assertEqual(voice_pack.find(root, "xiaozhuo").name, "小桌声音")
        self.assertIsNone(voice_pack.find(root, "没有"))

    def test_describe_is_a_plain_view(self):
        folder = write_pack(Path(self.tmp.name) / "a", dict(GOOD, voice="Microsoft Huihui"))
        data = voice_pack.load(folder).describe()
        self.assertEqual(data["engine"], "sapi")
        self.assertEqual(data["voice"], "Microsoft Huihui")
        self.assertIn("version", data)


class VoicePackImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "voices"
        self.source = Path(self.tmp.name) / "来源"
        self.memory, _ = make_memory(self.tmp.name)

    def test_import_only_records_metadata(self):
        write_pack(self.source, GOOD)
        # 包里夹带脚本也不该被复制
        (self.source / "evil.bat").write_text("del /q *", encoding="utf-8")
        (self.source / "run.py").write_text("print('boom')", encoding="utf-8")
        result = voice_pack.import_pack(self.root, self.source)
        self.assertTrue(result["ok"], result.get("error"))
        installed = self.root / "xiaozhuo"
        self.assertEqual(sorted(item.name for item in installed.iterdir()), [voice_pack.MANIFEST])
        self.assertFalse((installed / "evil.bat").exists())

    def test_import_needs_a_valid_manifest(self):
        self.assertFalse(voice_pack.import_pack(self.root, self.source)["ok"])
        write_pack(self.source, {"id": "坏 id"})
        self.assertFalse(voice_pack.import_pack(self.root, self.source)["ok"])

    def test_import_refuses_a_missing_folder(self):
        self.assertFalse(voice_pack.import_pack(self.root, Path(self.tmp.name) / "没有")["ok"])

    def test_import_refuses_duplicates_without_overwrite(self):
        write_pack(self.source, GOOD)
        self.assertTrue(voice_pack.import_pack(self.root, self.source)["ok"])
        second = voice_pack.import_pack(self.root, self.source)
        self.assertFalse(second["ok"])
        self.assertIn("同名", second["error"])
        self.assertTrue(voice_pack.import_pack(self.root, self.source, overwrite=True)["ok"])

    def test_select_and_resolve(self):
        write_pack(self.source, GOOD)
        voice_pack.import_pack(self.root, self.source)
        self.assertIsNone(voice_pack.resolve(self.root, self.memory))
        self.assertTrue(voice_pack.select(self.memory, "xiaozhuo"))
        pack = voice_pack.resolve(self.root, self.memory)
        self.assertIsNotNone(pack)
        self.assertEqual(pack.id, "xiaozhuo")
        self.assertTrue(voice_pack.select(self.memory, ""))
        self.assertIsNone(voice_pack.resolve(self.root, self.memory))

    def test_select_without_memory_reports_failure(self):
        self.assertFalse(voice_pack.select(None, "xiaozhuo"))
        self.assertEqual(voice_pack.selected(None), "")


# ---------------------------------------------------------------- 开机启动
class FakeWinreg:
    """够用的注册表替身：只实现 Run 键用到的几个调用。"""

    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values = {}

    class _Key:
        def __init__(self, store):
            self.store = store

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def OpenKey(self, root, path, reserved=0, access=0):
        if not self.values:
            raise FileNotFoundError(path)
        return self._Key(self.values)

    def CreateKeyEx(self, root, path, reserved=0, access=0):
        return self._Key(self.values)

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return (self.values[name], self.REG_SZ)

    def SetValueEx(self, key, name, reserved, kind, value):
        key.store[name] = value

    def DeleteValue(self, key, name):
        if name not in key.store:
            raise FileNotFoundError(name)
        del key.store[name]


class StartupTests(unittest.TestCase):
    def test_unsupported_platform_reports_cleanly(self):
        with mock.patch.object(startup_control, "_registry", return_value=None):
            self.assertFalse(startup_control.is_enabled())
            self.assertFalse(startup_control.enable())
            self.assertFalse(startup_control.disable())
            self.assertFalse(startup_control.describe()["supported"])

    def test_enable_disable_roundtrip(self):
        fake = FakeWinreg()
        with mock.patch.object(startup_control, "_registry", return_value=fake):
            self.assertFalse(startup_control.is_enabled())
            self.assertTrue(startup_control.enable("x.exe"))
            self.assertTrue(startup_control.is_enabled())
            self.assertTrue(startup_control.disable())
            self.assertFalse(startup_control.is_enabled())

    def test_set_enabled_reports_real_state(self):
        fake = FakeWinreg()
        with mock.patch.object(startup_control, "_registry", return_value=fake):
            self.assertTrue(startup_control.set_enabled(True))
            self.assertTrue(startup_control.describe()["enabled"])
            self.assertFalse(startup_control.set_enabled(False))
            self.assertFalse(startup_control.describe()["enabled"])

    def test_write_failure_is_not_fatal(self):
        class Broken(FakeWinreg):
            def SetValueEx(self, *args, **kwargs):
                raise PermissionError("拒绝访问")

        with mock.patch.object(startup_control, "_registry", return_value=Broken()):
            self.assertFalse(startup_control.set_enabled(True))

    def test_disable_on_empty_registry_is_success(self):
        fake = FakeWinreg()
        with mock.patch.object(startup_control, "_registry", return_value=fake):
            self.assertTrue(startup_control.disable())

    def test_command_line_points_at_the_program(self):
        line = startup_control.command_line()
        self.assertIn("main.py", line)
        self.assertTrue(line.startswith('"'))


# ---------------------------------------------------------------- 托盘与快捷键
class HotkeyParseTests(unittest.TestCase):
    def test_modifiers_and_letters(self):
        self.assertEqual(parse_hotkey("ctrl+alt+d"), (0x0003, ord("D")))
        self.assertEqual(parse_hotkey("Ctrl+Shift+F5"), (0x0006, 0x74))
        self.assertEqual(parse_hotkey("win+space"), (0x0008, 0x20))

    def test_single_key_without_modifier_is_refused(self):
        self.assertEqual(parse_hotkey("d"), (0, 0))

    def test_garbage_is_refused(self):
        for spec in ("", "ctrl+", "ctrl+?", "ctrl+alt", "shift+ctrl", "ctrl+unknownkey+5"):
            self.assertEqual(parse_hotkey(spec), (0, 0), spec)

    def test_tooltip_is_trimmed(self):
        self.assertEqual(tooltip_text(""), tray_module.DEFAULT_TOOLTIP)
        self.assertEqual(len(tooltip_text("x" * 500)), tray_module.TOOLTIP_MAX)


class FakeOwner:
    def __init__(self):
        self.calls = []
        self.raise_on = ""

    def _record(self, name):
        if name == self.raise_on:
            raise RuntimeError("宿主报错")
        self.calls.append(name)

    def toggle_visible(self):
        self._record("toggle_visible")

    def toggle_chat(self):
        self._record("toggle_chat")

    def open_settings(self):
        self._record("open_settings")

    def close(self):
        self._record("close")


class TrayCommandTests(unittest.TestCase):
    def test_menu_map_matches_menu(self):
        mapping = menu_map()
        self.assertEqual(mapping[1], "toggle_visible")
        self.assertEqual(set(mapping), {command for command, _l, _m in tray_module.MENU})

    def test_commands_reach_the_host(self):
        owner = FakeOwner()
        icon = TrayIcon(owner)
        for command in menu_map():
            self.assertTrue(icon.handle_command(command))
        self.assertEqual(sorted(owner.calls), sorted(menu_map().values()))

    def test_unknown_command_is_refused(self):
        icon = TrayIcon(FakeOwner())
        self.assertFalse(icon.handle_command(99))

    def test_host_error_does_not_leak(self):
        owner = FakeOwner()
        owner.raise_on = "close"
        icon = TrayIcon(owner)
        self.assertFalse(icon.handle_command(4))

    def test_hotkey_action_toggles_chat(self):
        owner = FakeOwner()
        self.assertTrue(TrayIcon(owner, hotkey="ctrl+alt+d")._on_hotkey())
        self.assertEqual(owner.calls, ["toggle_chat"])

    def test_describe_shows_state(self):
        data = TrayIcon(FakeOwner(), hotkey="ctrl+alt+d").describe()
        self.assertTrue(data["hotkey_ok"])
        self.assertFalse(data["started"])
        self.assertIn("available", data)


class TrayLifecycleTests(unittest.TestCase):
    def test_start_refuses_without_shell(self):
        with mock.patch.object(tray_module, "_load_shell", return_value=None):
            icon = TrayIcon(FakeOwner())
            self.assertFalse(icon.available())
            self.assertFalse(icon.start())
            self.assertIsNone(icon._thread)

    def test_loop_runs_and_stops(self):
        seen = {}

        def fake_loop(tray):
            seen["started"] = True
            tray._stop.wait(2)

        icon = TrayIcon(FakeOwner(), loop=fake_loop)
        icon._shell = {"user32": None}
        icon._probed = True
        self.assertTrue(icon.start())
        icon._thread.join(1)
        self.assertTrue(seen.get("started"))
        icon.stop()
        self.assertFalse(icon.started)

    def test_loop_error_is_recorded_not_raised(self):
        def broken_loop(tray):
            raise RuntimeError("托盘坏了")

        icon = TrayIcon(FakeOwner(), loop=broken_loop)
        icon._shell = {"user32": None}
        icon._probed = True
        self.assertTrue(icon.start())
        icon._thread.join(1)
        self.assertIn("托盘坏了", icon.failed)
        self.assertFalse(icon.started)

    def test_stop_is_safe_without_start(self):
        self.assertTrue(TrayIcon(FakeOwner()).stop())


# ---------------------------------------------------------------- 界面与宿主
class TkTestCase(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except Exception as exc:
            self.skipTest(f"tkinter 不可用：{exc}")
        self.root.withdraw()
        self.addCleanup(self._safe_destroy)
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

    def build_pet(self, voice_provider=None, speech=None, **kwargs):
        database = Database(Path(self.tmp.name) / "t.sqlite3")
        database.migrate()
        memory = MemoryManager(MemoryRepository(database), **kwargs)
        agent = AgentCore(
            StubProvider(), memory, ContextManager(token_budget=800)
        )
        pet = DesktopPet(
            agent,
            self.config,
            memory=memory,
            voice=VoicePipeline(provider=voice_provider or FakeVoice()),
            speech=speech or SpeechPipeline(provider=NullSpeech()),
            stt=PowerlessStt(),
            wake=WakeWordDetector(),
            mic=MicPrivacy(),
            privacy=PrivacyCenter(preferences=memory, config=self.config),
            root=self.root,
        )
        return pet, memory


def allow_listening(memory):
    """打开「能不能听」。默认是关着的，测试要先明确打开。"""
    memory.set_preference("privacy.listen", "1")


class PowerlessStt:
    """识别引擎的最小替身：永远不可用，但状态能读。"""

    provider = NullSttProvider()

    def available(self):
        return False


class VoiceCenterTests(TkTestCase):
    def test_rows_cover_the_voice_center(self):
        pet, _ = self.build_pet()
        rows = dict(pet.voice_center_rows())
        for key in ("麦克风", "能不能听", "监听", "唤醒词", "识别引擎", "语音输出", "语音包"):
            self.assertIn(key, rows)
        self.assertEqual(rows["监听"], "没有采集")
        self.assertIn("系统默认", rows["语音包"])

    def test_listening_off_by_default(self):
        pet, memory = self.build_pet()
        allow_listening(memory)
        voice = FakeVoice()
        pet.voice = VoicePipeline(provider=voice)
        self.assertTrue(pet.set_listening(True))
        self.assertTrue(voice.listening)
        self.assertEqual(pet.mic.label(), "正在监听")

    def test_stop_listening_stops_collecting(self):
        pet, memory = self.build_pet()
        allow_listening(memory)
        voice = FakeVoice()
        pet.voice = VoicePipeline(provider=voice)
        pet.set_listening(True)
        self.assertTrue(pet.set_listening(False))
        self.assertFalse(voice.listening)
        self.assertEqual(voice.stopped, 1)
        self.assertEqual(pet.mic.label(), "关闭")
        # 停掉之后管线不再采集
        self.assertIsNone(pet.voice.poll())

    def test_unavailable_microphone_is_reported(self):
        pet, memory = self.build_pet()
        allow_listening(memory)
        window = pet.open_settings()
        self.addCleanup(window.win.destroy)
        pet.voice = VoicePipeline(provider=FakeVoice(available=False))
        self.assertFalse(pet.set_listening(True))
        self.assertEqual(pet.mic.label(), "关闭")
        self.assertIn("麦克风", str(pet.settings_hint.cget("text")))

    def test_privacy_switch_blocks_listening(self):
        pet, memory = self.build_pet()
        memory.set_preference("privacy.listen", "0")
        pet.privacy = PrivacyCenter(preferences=memory, config=self.config)
        pet.voice = VoicePipeline(provider=FakeVoice())
        self.assertFalse(pet.set_listening(True))

    def test_wake_toggle(self):
        pet, _ = self.build_pet()
        self.assertTrue(pet.set_wake(True))
        self.assertTrue(pet.wake.armed)
        self.assertTrue(pet.set_wake(False))
        self.assertFalse(pet.wake.armed)

    def test_voice_missing_does_not_break_chat(self):
        pet, _ = self.build_pet()
        pet.voice = None
        self.assertFalse(pet.set_listening(True))
        self.assertTrue(pet.set_wake(False))
        rows = dict(pet.voice_center_rows())
        self.assertEqual(rows["麦克风"], "关闭")
        self.assertEqual(rows["监听"], "没有采集")

    def test_toggle_visible_roundtrip(self):
        pet, _ = self.build_pet()
        pet.toggle_visible()
        self.assertTrue(pet.host.hidden)
        pet.toggle_visible()
        self.assertFalse(pet.host.hidden)

    def test_import_and_select_a_pack(self):
        pet, memory = self.build_pet()
        source = write_pack(Path(self.tmp.name) / "来源", dict(GOOD, rate=4, volume=60))
        result = pet.import_voice_pack(source)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(voice_pack.selected(memory), "xiaozhuo")
        self.assertEqual(pet.current_voice_pack().rate, 4)
        self.assertEqual(pet.speech.provider.rate, 4)
        self.assertEqual(pet.speech.provider.volume, 60)
        rows = dict(pet.voice_center_rows())
        self.assertIn("小桌声音", rows["语音包"])

    def test_import_failure_is_reported(self):
        pet, _ = self.build_pet()
        result = pet.import_voice_pack(Path(self.tmp.name) / "没有这个目录")
        self.assertFalse(result["ok"])
        self.assertIsNone(pet.current_voice_pack())

    def test_use_system_default_clears_selection(self):
        pet, memory = self.build_pet()
        source = write_pack(Path(self.tmp.name) / "来源", GOOD)
        pet.import_voice_pack(source)
        self.assertTrue(pet.select_voice_pack(""))
        self.assertIsNone(pet.current_voice_pack())

    def test_saving_hotkey(self):
        pet, memory = self.build_pet()
        pet.hotkey_entry = tk.Entry(self.root)
        pet.hotkey_entry.insert(0, "ctrl+alt+d")
        self.assertTrue(pet._save_hotkey())
        self.assertEqual(memory.get_preference("shell.hotkey"), "ctrl+alt+d")

    def test_saving_garbage_hotkey_is_refused(self):
        pet, memory = self.build_pet()
        pet.hotkey_entry = tk.Entry(self.root)
        pet.hotkey_entry.insert(0, "ctrl+d+?")
        self.assertFalse(pet._save_hotkey())
        self.assertIsNone(memory.get_preference("shell.hotkey"))

    def test_empty_hotkey_means_no_hotkey(self):
        pet, memory = self.build_pet()
        pet.hotkey_entry = tk.Entry(self.root)
        self.assertTrue(pet._save_hotkey())
        self.assertEqual(memory.get_preference("shell.hotkey"), "")


class ShellWiringTests(TkTestCase):
    def test_tray_is_skipped_when_disabled(self):
        pet, _ = self.build_pet()
        self.config.tray = False
        self.assertIsNone(pet.start_tray())

    def test_tray_failure_is_not_fatal(self):
        pet, _ = self.build_pet()
        self.config.tray = True
        with mock.patch.object(tray_module.TrayIcon, "start", side_effect=RuntimeError("坏了")):
            self.assertIsNone(pet.start_tray())

    def test_tray_starts_when_available(self):
        pet, _ = self.build_pet()
        self.config.tray = True
        with mock.patch.object(tray_module.TrayIcon, "start", return_value=True):
            icon = pet.start_tray()
        self.assertIsNotNone(icon)
        self.assertIs(icon, pet.tray)

    def test_startup_checkbox_reflects_the_registry(self):
        pet, _ = self.build_pet()
        with mock.patch.object(startup_control, "is_enabled", return_value=True):
            self.assertTrue(pet.startup_enabled())
        with mock.patch.object(startup_control, "is_enabled", return_value=False):
            self.assertFalse(pet.startup_enabled())

    def test_startup_switch_uses_the_confirmed_path(self):
        pet, _ = self.build_pet()
        with mock.patch.object(
            startup_control, "describe", return_value={"supported": True}
        ), mock.patch.object(
            startup_control, "set_enabled", return_value=True
        ) as setter, mock.patch.object(
            startup_control, "is_enabled", return_value=True
        ):
            self.assertTrue(pet.set_startup(True))
        setter.assert_called_once_with(True)

    def test_unsupported_platform_says_so(self):
        pet, _ = self.build_pet()
        with mock.patch.object(
            startup_control, "describe", return_value={"supported": False}
        ):
            self.assertFalse(pet.set_startup(True))


class SettingsCenterTests(TkTestCase):
    def test_settings_has_a_voice_center(self):
        pet, _ = self.build_pet()
        window = pet.open_settings()
        self.addCleanup(window.win.destroy)
        titles = [window.book.tab(index, "text") for index in range(window.book.index("end"))]
        self.assertIn("语音", titles)
        # 设置中心按类别分区，基础与主动行为分开，不再挤在一个「通用」页里。
        self.assertIn("基础", titles)
        self.assertIn("主动行为", titles)
        self.assertIn("开发者", titles)
        self.assertNotIn("通用", titles)

    def test_voice_tab_builds_without_voice_hardware(self):
        pet, _ = self.build_pet()
        window = pet.open_settings()
        self.addCleanup(window.win.destroy)
        self.assertTrue(hasattr(window, "pack_var"))
        self.assertEqual(window.pack_var.get(), "")

    def test_general_tab_has_shell_controls(self):
        pet, _ = self.build_pet()
        window = pet.open_settings()
        self.addCleanup(window.win.destroy)
        self.assertTrue(hasattr(pet, "startup_var"))
        self.assertTrue(hasattr(pet, "hotkey_entry"))

    def test_use_pack_without_selection_is_refused(self):
        pet, _ = self.build_pet()
        window = pet.open_settings()
        self.addCleanup(window.win.destroy)
        self.assertFalse(window._use_voice_pack())

    def test_import_through_the_dialog(self):
        pet, memory = self.build_pet()
        source = write_pack(Path(self.tmp.name) / "来源", GOOD)
        window = pet.open_settings()
        self.addCleanup(window.win.destroy)
        with mock.patch.object(
            settings_view.filedialog, "askdirectory", return_value=str(source)
        ):
            self.assertTrue(window._import_voice_pack())
        self.assertEqual(voice_pack.selected(memory), "xiaozhuo")

    def test_cancelled_import_changes_nothing(self):
        pet, _ = self.build_pet()
        window = pet.open_settings()
        self.addCleanup(window.win.destroy)
        with mock.patch.object(settings_view.filedialog, "askdirectory", return_value=""):
            self.assertFalse(window._import_voice_pack())
        self.assertIsNone(pet.current_voice_pack())


# ---------------------------------------------------------------- 本地能力与任务流程
class LocalAbilityTests(TkTestCase):
    def build_agent(self, provider):
        memory = MemoryManager(
            MemoryRepository(Database(Path(self.tmp.name) / "l.sqlite3"))
        )
        memory.repository.db.migrate()
        return AgentCore(provider, memory, ContextManager(token_budget=800)), memory

    def test_local_time_needs_no_model(self):
        provider = StubProvider(available=False)
        agent, _ = self.build_agent(provider)
        answer = agent.capabilities.resolve("现在几点")
        self.assertIsNotNone(answer)
        self.assertIn("现在", answer.text)
        self.assertEqual(provider.calls, 0)

    def test_local_calculator_needs_no_model(self):
        provider = StubProvider(available=False)
        agent, _ = self.build_agent(provider)
        answer = agent.capabilities.resolve("算一下 12*8 等于多少")
        self.assertIsNotNone(answer)
        self.assertIn("96", answer.text)
        self.assertEqual(provider.calls, 0)

    def test_local_greeting_needs_no_model(self):
        provider = StubProvider(available=False)
        agent, _ = self.build_agent(provider)
        reply = agent.respond("t", "你好呀")
        self.assertTrue(reply.ok)
        self.assertEqual(provider.calls, 0)
        self.assertTrue(reply.content)


class TaskPermissionTests(unittest.TestCase):
    """任务模式的确认流程和阶段 D 之前完全一致。"""

    def test_medium_risk_asks_for_confirmation(self):
        asked = []

        def confirm(name, level, reason):
            asked.append((name, level))
            return True

        manager = PermissionManager(confirm_callback=confirm)
        decision = manager.check("write_file", PermissionLevel.HIGH, "改文件")
        self.assertTrue(decision.allowed)
        self.assertFalse(decision.auto)
        self.assertEqual(asked, [("write_file", "HIGH")])

    def test_refusal_keeps_the_operation_unrun(self):
        manager = PermissionManager(confirm_callback=lambda *a: False)
        decision = manager.check("write_file", PermissionLevel.HIGH, "改文件")
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.needs_confirmation)

    def test_no_confirm_channel_fails_closed(self):
        decision = PermissionManager().check("write_file", PermissionLevel.HIGH)
        self.assertFalse(decision.allowed)

    def test_low_risk_still_auto_allowed(self):
        decision = PermissionManager().check("weather", PermissionLevel.READ)
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.auto)

    def test_critical_is_denied_outright(self):
        decision = PermissionManager(
            confirm_callback=lambda *a: True
        ).check("run_command", PermissionLevel.CRITICAL)
        self.assertFalse(decision.allowed)


if __name__ == "__main__":
    unittest.main()
