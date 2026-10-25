"""V9 收尾验收：扩展注册表与许可证闸门、可选后端适配、命令输出压缩、
只读的屏幕与语音工具、三个本机技能，以及「不进任务栏 / 随时拖得动 / 改名 Blossom」。

全程临时目录、假对象和假工具层：不碰真实用户数据，不联网，不下载，也不装任何东西。
"""

import importlib.util
import base64
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters import (
    BuiltinRuntime,
    CommandOutputReducer,
    LocalMemoryBackend,
    PiRuntimeAdapter,
    WhisperCppProvider,
    build_memory_backend,
    build_runtime,
    build_vision_provider,
    build_voice_provider,
)
from adapters.memory_backend import CogneeAdapter
from adapters.vision_provider import LiteAiVisionProvider, NullVisionProvider, UiaVisionProvider
from adapters.voice_provider import NullVoiceProvider, TenVoiceProvider
from extensions import licenses
from extensions.catalog import builtin_catalog
from extensions.health import BLOCKED, MISSING, OK
from extensions.manifest import ExtensionManifest
from extensions.registry import ExtensionRegistry
from extensions.store import ExtensionStore
from security.config import APP_NAME, DATA_DIR_NAME, data_root
from skills.loader import SkillLoader
from skills.validator import SkillValidator
from tools import screen as screen_tool
from tools import voice as voice_tool
from tools import workspace as workspace_tool
from tools.registry import Tool, ToolExecutor, ToolRegistry
from ui.capability_view import CapabilityWindow
from ui.desktop import DesktopPet
from ui.pet_host import PetHost, hide_from_taskbar, hide_process_windows


def temp_store():
    folder = tempfile.mkdtemp(prefix="blossom-ext-")
    return ExtensionStore(root=folder), folder


def isolated_registry(catalog=None):
    store, folder = temp_store()
    return ExtensionRegistry(catalog=catalog, store=store), store, folder


def by_id(catalog, ext_id):
    for manifest in catalog:
        if manifest.id == ext_id:
            return manifest
    raise AssertionError("目录里没有 " + ext_id)


class ExtensionCatalogTests(unittest.TestCase):
    """目录里登记的事实要能对上，不能凭印象写。"""

    def test_the_catalog_lists_every_audited_entry(self):
        # 17 条 V9 的老底 + 12 条 Qoder 技能（3 个指定 + 9 个候选）+ 2 个外部连接器。
        self.assertEqual(len(builtin_catalog()), 31)

    def test_ids_are_unique(self):
        ids = [item.id for item in builtin_catalog()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_manifest_validates(self):
        for manifest in builtin_catalog():
            self.assertEqual(manifest.validate(), [], manifest.id)

    def test_optional_entries_pin_a_version_and_source(self):
        for manifest in builtin_catalog():
            if manifest.source != "optional":
                continue
            self.assertTrue(manifest.pinned, manifest.id)
            self.assertTrue(manifest.homepage.startswith("http"), manifest.id)

    def test_the_gpl_component_is_marked_blocked(self):
        manifest = by_id(builtin_catalog(), "lite-ai-toolkit")
        self.assertEqual(manifest.license, "GPL-3.0")
        self.assertEqual(manifest.license_status, "blocked")
        self.assertTrue(manifest.bundle_blocked())
        self.assertFalse(manifest.bundled)

    def test_unreviewed_licenses_are_flagged(self):
        ten = by_id(builtin_catalog(), "ten-framework")
        self.assertEqual(ten.license_status, "review-needed")
        self.assertTrue(ten.needs_review())

    def test_builtin_adapters_are_marked_bundled(self):
        builtins = [item for item in builtin_catalog() if item.source == "builtin"]
        self.assertEqual(len(builtins), 5)
        self.assertTrue(all(item.bundled for item in builtins))

    def test_summary_counts_match_the_catalog(self):
        registry, _store, folder = isolated_registry()
        summary = registry.summary()
        self.assertEqual(summary["total"], 31)
        # blocked 两条：GPL-3.0 的 lite-ai-toolkit，和 AGPL-3.0 的 superpowers。
        self.assertEqual(summary["blocked"], 2)
        self.assertEqual(summary["optional"], 11)
        self.assertEqual(summary["need_review"], 14)


class ExtensionGateTests(unittest.TestCase):
    """没装不能开、许可证没过不能开、带脚本要单独确认。"""

    def setUp(self):
        self.registry, self.store, self.folder = isolated_registry()

    def test_an_optional_extension_cannot_run_before_it_is_installed(self):
        ok, reason = self.registry.enable("cognee")
        self.assertFalse(ok)
        self.assertIn("还没安装", reason)

    def test_installing_then_enabling_works(self):
        self.assertTrue(self.registry.install("cognee")[0])
        self.assertTrue(self.registry.enable("cognee")[0])
        self.assertTrue(self.registry.is_enabled("cognee"))

    def test_an_extension_without_its_environment_is_not_usable(self):
        self.registry.install("cognee")
        self.registry.enable("cognee")
        self.assertFalse(self.registry.usable("cognee"))

    def test_disabling_takes_effect_at_once(self):
        self.registry.install("cognee")
        self.registry.enable("cognee")
        self.assertTrue(self.registry.disable("cognee")[0])
        self.assertFalse(self.registry.is_enabled("cognee"))

    def test_a_blocked_license_cannot_be_enabled(self):
        ok, reason = self.registry.enable("lite-ai-toolkit")
        self.assertFalse(ok)
        self.assertIn("许可证", reason)

    def test_a_scripted_extension_needs_explicit_confirmation(self):
        base = by_id(builtin_catalog(), "cognee")
        scripted = replace(base, id="demo.scripted", name="带脚本的演示", scripts=("install.py",))
        registry, _store, _folder = isolated_registry([scripted])
        registry.install("demo.scripted")
        allowed, reason = registry.can_enable("demo.scripted")
        self.assertFalse(allowed)
        self.assertIn("单独确认", reason)
        ok, reason = registry.enable("demo.scripted")
        self.assertFalse(ok)
        self.assertIn("确认", reason)
        self.assertTrue(registry.enable("demo.scripted", confirmed=True)[0])

    def test_an_unknown_extension_is_refused_everywhere(self):
        self.assertEqual(self.registry.enable("nope")[0], False)
        self.assertEqual(self.registry.disable("nope")[0], False)
        self.assertEqual(self.registry.install("nope")[0], False)
        self.assertEqual(self.registry.uninstall("nope")[0], False)
        self.assertFalse(self.registry.is_enabled("nope"))
        self.assertTrue(self.registry.health("nope")["detail"])

    def test_builtin_extensions_cannot_be_uninstalled(self):
        ok, reason = self.registry.uninstall("blossom.memory.local")
        self.assertFalse(ok)
        self.assertIn("内置", reason)

    def test_uninstalling_clears_enabled_and_installed(self):
        self.registry.install("rtk")
        self.registry.enable("rtk")
        self.assertTrue(self.registry.uninstall("rtk")[0])
        self.assertFalse(self.registry.installed("rtk"))
        self.assertFalse(self.registry.is_enabled("rtk"))

    def test_builtin_extensions_are_on_by_default(self):
        self.assertTrue(self.registry.is_enabled("blossom.tools.reducer"))

    def test_duplicate_ids_are_refused(self):
        manifest = by_id(builtin_catalog(), "cognee")
        with self.assertRaises(ValueError):
            self.registry.register(manifest)

    def test_a_broken_manifest_is_refused(self):
        with self.assertRaises(ValueError):
            self.registry.register(ExtensionManifest(id="Bad Id", name=""))

    def test_health_check_reports_the_platform_and_missing_tools(self):
        manifest = ExtensionManifest(
            id="demo.health",
            name="环境演示",
            health=(
                {"kind": "platform", "name": "windows"},
                {"kind": "binary", "name": "no-such-binary-blossom"},
            ),
        )
        result = self.registry.checker(manifest)
        self.assertEqual(result["status"], MISSING)
        self.assertTrue(result["checks"])
        self.assertTrue(result["detail"])

    def test_a_blocked_extension_reports_blocked_health(self):
        manifest = by_id(builtin_catalog(), "lite-ai-toolkit")
        self.assertEqual(self.registry.checker(manifest)["status"], BLOCKED)

    def test_rows_expose_the_state_the_ui_needs(self):
        rows = {row["id"]: row for row in self.registry.rows()}
        cognee = rows["cognee"]
        for key in ("installed", "enabled", "usable", "health", "can_enable", "license_status"):
            self.assertIn(key, cognee)
        self.assertFalse(cognee["installed"])
        self.assertFalse(cognee["can_enable"])

    def test_rows_can_be_filtered(self):
        everything = self.registry.rows()
        builtin = self.registry.rows("builtin")
        self.assertTrue(builtin)
        self.assertLess(len(builtin), len(everything))
        self.assertTrue(all(row["source"] == "builtin" for row in builtin))

    def test_the_unusable_filter_lists_what_cannot_be_turned_on(self):
        """「不可用」这一档是「开不了」，不是「许可证有问题」。"""
        rows = self.registry.rows("blocked")
        ids = [row["id"] for row in rows]
        self.assertIn("lite-ai-toolkit", ids)
        # 没装的、带脚本的、许可证没过的都进这一档，规则就一条：现在开不了。
        self.assertTrue(all(row["can_enable"] is False for row in rows))

    def test_the_license_wording_stays_distinct_from_the_filter(self):
        rows = {row["id"]: row for row in self.registry.rows()}
        self.assertEqual(rows["lite-ai-toolkit"]["license_status"], "blocked")
        self.assertIn("许可证", rows["lite-ai-toolkit"]["can_enable_reason"])

    def test_filters_carry_counts(self):
        rows = self.registry.filters()
        labels = [row["label"] for row in rows]
        self.assertIn("已安装", labels)
        self.assertIn("不可用", labels)
        for row in rows:
            self.assertIsInstance(row["count"], int)
            self.assertIsInstance(row["key"], str)


class ExtensionStoreTests(unittest.TestCase):
    """状态写在用户数据目录里，读坏了先备份再重来。"""

    def test_state_round_trips(self):
        store, folder = temp_store()
        self.assertTrue(store.set_enabled("cognee", True))
        self.assertTrue(store.path.exists())
        again = ExtensionStore(root=folder)
        self.assertTrue(again.state("cognee").get("enabled"))

    def test_a_corrupt_state_file_is_backed_up_and_reset(self):
        store, folder = temp_store()
        store.path.write_text("{ 这不是 JSON", encoding="utf-8")
        self.assertEqual(store.state("cognee"), {})
        self.assertTrue(store.last_error)
        self.assertTrue(store.path.with_suffix(".json.bak").exists())
        self.assertTrue(store.set_enabled("cognee", True))

    def test_a_write_failure_is_reported_not_hidden(self):
        store, folder = temp_store()
        store.state("cognee")
        with mock.patch("extensions.store.open", side_effect=OSError("磁盘满了"), create=True):
            self.assertFalse(store.set_enabled("cognee", True))
        self.assertTrue(store.last_error)

    def test_blank_ids_are_ignored(self):
        store, folder = temp_store()
        self.assertFalse(store.set_enabled("", True))


class ExtensionLicenseTests(unittest.TestCase):
    """许可证清单：不能随包分发的一律排前面，写进 NOTICE 也不含糊。"""

    def test_blocked_components_come_first(self):
        rows = licenses.rows(builtin_catalog())
        self.assertEqual(rows[0]["license_status"], "blocked")
        self.assertEqual(rows[0]["id"], "lite-ai-toolkit")

    def test_bundle_blocked_lists_the_component(self):
        blocked = [row["id"] for row in licenses.blocked_from_bundle(builtin_catalog())]
        self.assertIn("lite-ai-toolkit", blocked)
        self.assertIn("ten-framework", blocked)

    def test_components_without_a_known_license_need_review(self):
        pending = [row["id"] for row in licenses.review_pending(builtin_catalog())]
        self.assertIn("anthropic-skills", pending)
        self.assertIn("mcp-servers", pending)

    def test_the_notice_says_which_components_stay_out(self):
        text = licenses.notice_text(builtin_catalog())
        self.assertIn(APP_NAME, text)
        self.assertIn("GPL-3.0", text)
        self.assertIn("不得随包分发", text)


class CapabilityViewTests(unittest.TestCase):
    """能力中心的两个新分类直接读注册表，注册表没挂上就如实空着。"""

    def build_window(self, extensions):
        window = CapabilityWindow.__new__(CapabilityWindow)
        window.pet = SimpleNamespace(agent=SimpleNamespace(extensions=extensions))
        return window

    def test_the_extension_tab_lists_every_entry(self):
        registry, _store, _folder = isolated_registry()
        window = self.build_window(registry)
        self.assertEqual(len(window._extension_rows()), 31)
        self.assertEqual(len(window._extension_rows("builtin")), 5)

    def test_the_license_tab_lists_every_entry(self):
        registry, _store, _folder = isolated_registry()
        self.assertEqual(len(self.build_window(registry)._license_rows()), 31)

    def test_no_registry_means_empty_lists_not_fake_rows(self):
        window = self.build_window(None)
        self.assertEqual(window._extension_rows(), [])
        self.assertEqual(window._license_rows(), [])

    def test_the_filter_keys_match_the_registry(self):
        keys = [key for key, _label in CapabilityWindow.EXTENSION_FILTERS]
        self.assertEqual(keys[0], "")
        self.assertIn("blocked", keys)


class MemoryAdapterTests(unittest.TestCase):
    """记忆后端默认是本地那套，别的后端没装就明说没装。"""

    def test_cognee_is_not_available_until_it_is_installed(self):
        adapter = CogneeAdapter()
        self.assertFalse(adapter.available())
        result = adapter.remember("测试")
        self.assertFalse(result["ok"])
        self.assertIn("Cognee", result["reason"])

    def test_cognee_import_plan_never_touches_the_existing_database(self):
        plan = CogneeAdapter().plan_import(12, scope="最近十二次会话")
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["session_count"], 12)
        self.assertIn("不会被替换", plan["warning"])

    def test_forget_on_an_unavailable_backend_is_refused(self):
        self.assertFalse(CogneeAdapter().forget("1")["ok"])

    def test_the_local_backend_wraps_the_existing_memory(self):
        memory = FakeMemory()
        backend = LocalMemoryBackend(memory)
        self.assertTrue(backend.available())
        self.assertTrue(backend.remember("用户喜欢清淡口味")["ok"])
        found = backend.recall("口味", limit=3)
        self.assertTrue(found["ok"])
        self.assertEqual(found["source"], "local")
        self.assertTrue(found["items"])

    def test_a_backend_without_a_store_says_so(self):
        backend = LocalMemoryBackend(None)
        self.assertFalse(backend.available())
        self.assertFalse(backend.remember("x")["ok"])

    def test_remember_failure_is_reported(self):
        class Broken(FakeMemory):
            def remember(self, text, **meta):
                raise RuntimeError("库锁住了")

        result = LocalMemoryBackend(Broken()).remember("x")
        self.assertFalse(result["ok"])
        self.assertIn("库锁住了", result["reason"])

    def test_the_default_backend_is_the_local_one(self):
        self.assertIsInstance(build_memory_backend(None, FakeMemory()), LocalMemoryBackend)


class RuntimeAdapterTests(unittest.TestCase):
    """Pi 只是接口：没有 Node 或没有权限闸门都不准跑工具。"""

    def test_pi_is_not_available_without_its_runtime(self):
        adapter = PiRuntimeAdapter()
        self.assertFalse(adapter.available())
        self.assertTrue(adapter.last_error)

    def test_pi_refuses_tools_without_the_permission_gate(self):
        adapter = PiRuntimeAdapter()
        allowed, reason = adapter.check_tool("write_file")
        self.assertFalse(allowed)
        self.assertIn("权限中心", reason)

    def test_pi_defers_to_the_permission_gate(self):
        calls = []

        def gate(name, level, params):
            calls.append(name)
            return (name == "read_file"), "" if name == "read_file" else "拒绝"

        adapter = PiRuntimeAdapter(permission_gate=gate, sdk=object())
        self.assertTrue(adapter.can_run_tools())
        self.assertTrue(adapter.check_tool("read_file")[0])
        self.assertFalse(adapter.check_tool("write_file")[0])
        self.assertEqual(calls, ["read_file", "write_file"])

    def test_a_failing_gate_refuses_instead_of_allowing(self):
        def gate(name, level, params):
            raise RuntimeError("闸门坏了")

        adapter = PiRuntimeAdapter(permission_gate=gate)
        allowed, reason = adapter.check_tool("read_file")
        self.assertFalse(allowed)
        self.assertIn("坏", reason)

    def test_pi_without_a_working_environment_streams_an_honest_message(self):
        events = list(PiRuntimeAdapter().stream("帮我改代码"))
        self.assertEqual(events[0]["type"], "unavailable")

    def test_the_default_runtime_is_the_builtin_agent(self):
        agent = SimpleNamespace()
        self.assertIsInstance(build_runtime(None, agent), BuiltinRuntime)

    def test_the_builtin_runtime_only_calls_what_the_agent_exposes(self):
        calls = []

        class Reply:
            text = "在"
            ok = True

        class Agent:
            def respond(self, session_id, prompt):
                calls.append((session_id, prompt))
                return Reply()

        runtime = BuiltinRuntime(Agent())
        self.assertTrue(runtime.available())
        events = list(runtime.stream("你好"))
        self.assertEqual(events[0], {"type": "text", "text": "在"})
        self.assertTrue(events[-1]["ok"])
        self.assertEqual(calls, [("cli", "你好")])

    def test_the_builtin_runtime_reports_a_broken_agent(self):
        class Agent:
            def respond(self, session_id, prompt):
                raise RuntimeError("大脑没接上")

        events = list(BuiltinRuntime(Agent()).stream("你好"))
        self.assertEqual(events[0]["type"], "error")
        self.assertIn("没接上", events[0]["text"])

    def test_the_builtin_runtime_says_it_has_nothing_to_run_on(self):
        self.assertFalse(BuiltinRuntime(None).available())


class VoiceAdapterTests(unittest.TestCase):
    """语音后端：隐私开关关着谁都不准开麦，外部的必须标成要联网。"""

    def test_a_closed_microphone_switch_blocks_every_backend(self):
        provider = build_voice_provider(privacy=lambda: False)
        self.assertIsInstance(provider, NullVoiceProvider)
        self.assertFalse(provider.available())
        self.assertFalse(provider.listen()["ok"])

    def test_nothing_wired_up_reports_no_voice_ability(self):
        provider = NullVoiceProvider()
        data = provider.describe()
        self.assertFalse(data["available"])
        self.assertTrue(data["reason"])

    def test_the_system_backend_only_claims_what_it_has(self):
        provider = build_voice_provider()
        self.assertIsInstance(provider, NullVoiceProvider)

    def test_the_external_backend_is_marked_online_and_keyed(self):
        data = TenVoiceProvider().describe()
        self.assertFalse(data["offline"])
        self.assertTrue(data["requires_network"])
        self.assertTrue(data["requires_api_key"])
        self.assertEqual(data["license_status"], "review-needed")

    def test_the_local_recognition_needs_its_own_model(self):
        self.assertFalse(WhisperCppProvider(model_path="").available())
        self.assertFalse(WhisperCppProvider().transcribe("a.wav")["ok"])

    def test_the_external_backend_does_not_pretend_to_listen(self):
        self.assertFalse(TenVoiceProvider().listen()["ok"])


class VisionAdapterTests(unittest.TestCase):
    """屏幕感知：先语义、后像素，默认关着，GPL 的那个一步都不走。"""

    def test_the_default_backend_is_the_null_one(self):
        self.assertIsInstance(build_vision_provider(), NullVisionProvider)
        self.assertFalse(build_vision_provider().available())

    def test_the_null_backend_says_why_it_cannot_see(self):
        data = NullVisionProvider().describe()
        self.assertFalse(data["available"])
        self.assertTrue(data["reason"])

    def test_a_closed_privacy_switch_blocks_reading(self):
        provider = UiaVisionProvider(reader=lambda: {"text": "秘密"})
        provider.privacy = lambda: False
        self.assertFalse(provider.available())
        result = provider.snapshot()
        self.assertFalse(result["ok"])
        self.assertIn("关着", result["reason"])

    def test_semantics_are_read_only_when_a_reader_is_wired(self):
        provider = UiaVisionProvider(reader=lambda: {"text": "记事本"}, privacy=lambda: True)
        self.assertTrue(provider.available())
        result = provider.snapshot()
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["text"], "记事本")

    def test_a_failing_reader_is_reported(self):
        def broken():
            raise RuntimeError("读不到")

        provider = UiaVisionProvider(reader=broken, privacy=lambda: True)
        result = provider.snapshot()
        self.assertFalse(result["ok"])
        self.assertIn("读不到", result["reason"])

    def test_the_gpl_backend_never_loads(self):
        provider = LiteAiVisionProvider(module=object())
        self.assertFalse(provider.available())
        self.assertIn("GPL-3.0", provider.describe()["license"])
        self.assertFalse(provider.snapshot()["ok"])

    def test_no_reader_means_no_semantics_claim(self):
        result = UiaVisionProvider().snapshot()
        self.assertFalse(result["ok"])
        self.assertTrue(result["reason"])


class OutputReducerTests(unittest.TestCase):
    """压缩是省上下文，不是把信息藏起来：报错行一条都不许丢。"""

    def setUp(self):
        self.reducer = CommandOutputReducer()

    def test_only_whitelisted_commands_are_reduced(self):
        self.assertTrue(self.reducer.supported("ls -la"))
        self.assertTrue(self.reducer.supported("git status"))
        self.assertFalse(self.reducer.supported("rm -rf /"))

    def test_an_unknown_command_is_passed_through_untouched(self):
        raw = "\n".join("line %d" % index for index in range(300))
        result = self.reducer.reduce("curl something", raw)
        self.assertEqual(result["tool"], "passthrough")
        self.assertEqual(result["text"], raw)
        self.assertEqual(result["saved_bytes"], 0)

    def test_a_long_listing_is_folded_but_keeps_its_shape(self):
        raw = "\n".join("file-%03d.txt" % index for index in range(400))
        result = self.reducer.reduce("ls -la", raw)
        self.assertEqual(result["tool"], "local")
        self.assertGreater(result["saved_bytes"], 0)
        self.assertIn("这里略过", result["text"])
        self.assertTrue(result["text"].startswith("file-000.txt"))
        self.assertTrue(result["text"].rstrip().endswith("file-399.txt"))

    def test_error_lines_survive_the_fold(self):
        lines = ["file-%03d.txt" % index for index in range(400)]
        lines[200] = "ERROR: build failed"
        lines[250] = "permission denied"
        result = self.reducer.reduce("ls -la", "\n".join(lines))
        self.assertIn("ERROR: build failed", result["text"])
        self.assertIn("permission denied", result["text"])

    def test_the_original_is_kept_for_inspection(self):
        raw = "\n".join("file-%03d.txt" % index for index in range(400))
        result = self.reducer.reduce("ls -la", raw)
        self.assertEqual(result["original"], raw)
        self.assertNotEqual(result["text"], result["original"])

    def test_the_original_can_be_dropped_on_request(self):
        raw = "\n".join("file-%03d.txt" % index for index in range(400))
        self.assertEqual(self.reducer.reduce("ls -la", raw, keep_full=False)["original"], "")

    def test_a_short_listing_is_left_alone(self):
        raw = "a.txt\nb.txt"
        result = self.reducer.reduce("ls -la", raw)
        self.assertFalse(result["changed"])
        self.assertEqual(result["text"], raw)


class OutputReducerExternalTests(unittest.TestCase):
    """外部压缩器只是可选加速：它出错就退回本地，绝不因此丢内容。"""

    class FakeRegistry:
        def __init__(self, enabled=True):
            self.enabled = enabled

        def is_enabled(self, ext_id):
            return self.enabled and ext_id == "rtk"

    def build(self, runner, enabled=True):
        return CommandOutputReducer(registry=self.FakeRegistry(enabled), runner=runner)

    def test_a_failing_external_tool_falls_back_to_the_local_fold(self):
        with mock.patch("adapters.output_reducer.shutil.which", return_value="C:/rtk.exe"):
            reducer = self.build(lambda argv, text: None)
            raw = "\n".join("file-%03d.txt" % index for index in range(400))
            result = reducer.reduce("ls -la", raw)
        self.assertTrue(result["fallback"])
        self.assertEqual(result["tool"], "local")
        self.assertIn("退回本地压缩", result["note"])
        self.assertGreater(result["saved_bytes"], 0)

    def test_a_result_that_drops_a_failure_line_is_rejected(self):
        raw = "\n".join("file-%03d.txt" % index for index in range(400))
        raw_lines = raw.splitlines()
        raw_lines[200] = "ERROR: build failed"
        raw = "\n".join(raw_lines)

        reducer = self.build(lambda argv, text: "everything is fine")
        with mock.patch("adapters.output_reducer.shutil.which", return_value="C:/rtk.exe"):
            result = reducer.reduce("ls -la", raw)
        self.assertIn("ERROR: build failed", result["text"])
        self.assertIn("关键行", reducer.last_error)

    def test_a_clean_external_result_is_used_as_is(self):
        def runner(argv, text):
            return "压缩后的清单"

        with mock.patch("adapters.output_reducer.shutil.which", return_value="C:/rtk.exe"):
            result = self.build(runner).reduce("ls -la", "plain listing")
        self.assertEqual(result["tool"], "rtk")
        self.assertEqual(result["text"], "压缩后的清单")

    def test_the_external_tool_stays_off_when_it_is_not_enabled(self):
        calls = []

        def runner(argv, text):
            calls.append(argv)
            return "压缩后的清单"

        with mock.patch("adapters.output_reducer.shutil.which", return_value="C:/rtk.exe"):
            result = self.build(runner, enabled=False).reduce("ls -la", "plain listing")
        self.assertEqual(calls, [])
        self.assertEqual(result["tool"], "local")

    def test_it_says_what_it_can_do(self):
        data = CommandOutputReducer().describe()
        self.assertIn("files", data["families"])
        self.assertIn("原始输出保留", data["rules"])


class WorkspaceReducerHookTests(unittest.TestCase):
    """压缩只影响给模型看的那份，原始输出另存一份完整文件。"""

    def tearDown(self):
        workspace_tool.set_output_reducer(None)

    def test_no_reducer_means_the_output_is_untouched(self):
        text, note, path = workspace_tool._reduced_view("ls -la", "a\nb")
        self.assertEqual(text, "a\nb")
        self.assertEqual(note, "")
        self.assertEqual(path, "")

    def test_the_full_output_is_saved_beside_the_folded_one(self):
        folder = tempfile.mkdtemp(prefix="blossom-out-")
        raw = "\n".join("file-%03d.txt" % index for index in range(400))
        workspace_tool.set_output_reducer(CommandOutputReducer())
        with mock.patch("security.config.data_root", return_value=folder):
            text, note, path = workspace_tool._reduced_view("ls -la", raw)
        self.assertIn("这里略过", text)
        self.assertIn("字节", note)
        self.assertTrue(path.endswith(".log"))
        self.assertEqual(Path(path).read_text(encoding="utf-8"), raw)

    def test_a_broken_reducer_falls_back_to_the_full_output(self):
        class Broken:
            def reduce(self, command, output):
                raise RuntimeError("压缩器坏了")

        workspace_tool.set_output_reducer(Broken())
        text, note, _path = workspace_tool._reduced_view("ls -la", "a\nb")
        self.assertEqual(text, "a\nb")
        self.assertIn("完整输出", note)


class ModelVisibilityTests(unittest.TestCase):
    """完整输出是给人看的，不塞进模型上下文。"""

    class AllowAll:
        class Decision:
            allowed = True
            reason = ""
            needs_confirmation = False
            level = "READ"

        def check(self, name, level, risk, arguments):
            return self.Decision()

    def build_executor(self, result):
        registry = ToolRegistry()
        registry.register(
            Tool(name="demo", description="演示", permission="READ", handler=lambda: result, readonly=True)
        )
        return ToolExecutor(registry, self.AllowAll())

    def test_the_raw_output_is_not_sent_to_the_model(self):
        outcome = self.build_executor({"output": "短结果", "output_raw": "很长很长" * 100}).execute("demo", {})
        self.assertTrue(outcome.ok)
        self.assertIn("短结果", outcome.result)
        self.assertNotIn("output_raw", outcome.result)

    def test_other_results_are_untouched(self):
        outcome = self.build_executor({"output": "短结果"}).execute("demo", {})
        self.assertIn("短结果", outcome.result)


class ScreenToolTests(unittest.TestCase):
    """屏幕感知默认关着；打开了也只读窗口标题，不截图。"""

    def tearDown(self):
        screen_tool.set_vision_provider(None, lambda: False)

    def test_it_is_off_by_default(self):
        screen_tool.set_vision_provider(None, lambda: False)
        self.assertFalse(screen_tool.perception_on())
        with self.assertRaises(Exception):
            screen_tool.screen_context_text()

    def test_no_privacy_switch_at_all_counts_as_off(self):
        screen_tool._privacy = None
        self.assertFalse(screen_tool.perception_on())

    def test_a_broken_privacy_switch_counts_as_off(self):
        def broken():
            raise RuntimeError("读不了开关")

        screen_tool.set_vision_provider(None, broken)
        self.assertFalse(screen_tool.perception_on())

    def test_semantics_say_so_when_no_reader_is_wired(self):
        screen_tool._provider = None
        screen_tool.set_vision_provider(None, lambda: True)
        result = screen_tool.semantics()
        self.assertFalse(result["ok"])
        self.assertTrue(result["reason"])

    def test_semantics_come_from_the_wired_backend(self):
        class FakeProvider:
            def snapshot(self):
                return {"ok": True, "data": {"text": "记事本"}}

        screen_tool.set_vision_provider(FakeProvider(), lambda: True)
        result = screen_tool.semantics()
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "记事本")

    def test_a_refusing_backend_is_reported(self):
        class FakeProvider:
            def snapshot(self):
                return {"ok": False, "reason": "用户关着"}

        screen_tool.set_vision_provider(FakeProvider(), lambda: True)
        result = screen_tool.semantics()
        self.assertFalse(result["ok"])
        self.assertIn("关着", result["reason"])

    def test_a_backend_without_snapshot_is_reported(self):
        screen_tool.set_vision_provider(object(), lambda: True)
        self.assertFalse(screen_tool.semantics()["ok"])

    @unittest.skipUnless(sys.platform.startswith("win"), "只在 Windows 上有窗口接口")
    def test_the_foreground_window_is_read_for_real(self):
        screen_tool.set_vision_provider(None, lambda: True)
        info = screen_tool.foreground_window()
        self.assertTrue(info["ok"])
        self.assertTrue(info["title"])

    def test_the_tool_is_registered_read_only(self):
        registry = ToolRegistry()
        screen_tool.register_screen_tools(registry, config=None, privacy=lambda: False)
        tool = registry.get("screen_context")
        self.assertIsNotNone(tool)
        self.assertEqual(tool.permission, "READ")
        self.assertTrue(tool.is_readonly)
        with self.assertRaises(Exception):
            tool.handler()

    def test_a_config_without_the_flag_stays_off(self):
        registry = ToolRegistry()
        screen_tool.register_screen_tools(registry, config=SimpleNamespace(screen_perception=False))
        with self.assertRaises(Exception):
            registry.get("screen_context").handler()


class VoiceToolTests(unittest.TestCase):
    """语音工具只报状态，不开麦、不朗读。"""

    def tearDown(self):
        voice_tool.set_voice_provider(None)
        voice_tool._provider = None

    def test_without_a_backend_it_says_so(self):
        voice_tool._provider = None
        data = voice_tool.provider_state()
        self.assertFalse(data["available"])
        self.assertTrue(data["reason"])

    def test_a_backend_that_raises_is_reported(self):
        class Broken:
            name = "broken"

            def describe(self):
                raise RuntimeError("后端没回应")

        voice_tool.set_voice_provider(Broken())
        data = voice_tool.provider_state()
        self.assertFalse(data["available"])
        self.assertIn("没回应", data["reason"])

    def test_the_status_comes_from_the_real_configuration(self):
        folder = tempfile.mkdtemp(prefix="blossom-voices-")
        pack = Path(folder) / "xiaozhuo"
        pack.mkdir()
        (pack / "voicepack.json").write_text(
            json.dumps(
                {
                    "id": "xiaozhuo",
                    "name": "小桌声音",
                    "version": "1.0",
                    "engine": "sapi",
                    "voice": "Microsoft Huihui",
                    "rate": 0,
                    "volume": 100,
                    "note": "演示",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        config = SimpleNamespace(voices_dir=folder, voice_output=True, stt_engine="")
        text = voice_tool.voice_status_text(config)
        self.assertIn("小桌声音", text)
        self.assertIn("开着", text)
        self.assertIn("没配", text)

    def test_no_voice_pack_installed_is_said_plainly(self):
        folder = tempfile.mkdtemp(prefix="blossom-voices-")
        config = SimpleNamespace(voices_dir=folder, voice_output=False, stt_engine="whisper")
        self.assertIn("一个语音包都没装", voice_tool.voice_pack_text(config))

    def test_the_registered_tools_are_read_only(self):
        registry = ToolRegistry()
        voice_tool.register_voice_tools(registry, config=SimpleNamespace())
        for name in ("voice_status", "voice_pack_list"):
            tool = registry.get(name)
            self.assertIsNotNone(tool)
            self.assertTrue(tool.is_readonly)
            self.assertTrue(tool.handler())


class SkillLibraryTests(unittest.TestCase):
    """三个新技能：能装上、声明工具是真的、跑起来不靠模型。"""

    def build_registry(self):
        from tools.local import register_local_tools
        from tools.screen import register_screen_tools
        from tools.voice import register_voice_tools

        registry = ToolRegistry()
        register_local_tools(registry)
        register_screen_tools(registry, privacy=lambda: False)
        register_voice_tools(registry, config=None)
        for name in ("list_calendar_events", "get_weather"):
            registry.register(Tool(name=name, description="演示", handler=lambda **kwargs: ""))
        return registry

    def load_impl(self, name):
        path = ROOT / "skills" / "library" / name / "impl.py"
        spec = importlib.util.spec_from_file_location("probe_" + name.replace("-", "_"), path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_the_three_new_skills_are_discovered(self):
        found = SkillLoader(tools=self.build_registry()).discover()
        for name in ("screen-context", "voice-assistant", "daily-brief"):
            self.assertIn(name, found)

    def test_each_new_skill_validates_against_the_real_tool_list(self):
        registry = self.build_registry()
        loader = SkillLoader(tools=registry)
        for name in ("screen-context", "voice-assistant", "daily-brief"):
            report = SkillValidator(tools=registry).validate(loader.load(name))
            self.assertTrue(report.ok, "%s: %s" % (name, report.errors))

    def test_daily_brief_stitches_the_local_readings(self):
        module = self.load_impl("daily-brief")
        context = FakeContext(
            {
                "local_time": FakeOutcome(result="今天是 2026-10-13，周二。"),
                "todo_list": FakeOutcome(result="有 1 条：写文档"),
                "list_calendar_events": FakeOutcome(result="10:00 开会"),
                "get_weather": FakeOutcome(result="多云 18 度"),
                "system_status": FakeOutcome(result="开机 2 小时。"),
            }
        )
        result = module.run(context)
        self.assertTrue(result["ok"])
        self.assertIn("2026-10-13", result["summary"])
        self.assertIn("写文档", result["summary"])
        self.assertIn("多云", result["summary"])
        self.assertIn("没有让模型编内容", result["detail"])
        self.assertEqual([name for name, _args in context.calls][0], "local_time")

    def test_daily_brief_skips_what_it_cannot_read(self):
        module = self.load_impl("daily-brief")
        context = FakeContext(
            {
                "local_time": FakeOutcome(result="今天是 2026-10-13。"),
                "todo_list": FakeOutcome(ok=False, error="待办库读不到"),
                "list_calendar_events": FakeOutcome(ok=False, error="没有日程"),
                "get_weather": FakeOutcome(ok=False, error="天气服务不可用"),
                "system_status": FakeOutcome(result="开机 2 小时。"),
            }
        )
        result = module.run(context)
        self.assertTrue(result["ok"])
        self.assertIn("没有让模型编内容", result["detail"])
        self.assertIn("天气服务不可用", result["detail"])
        self.assertNotIn("多云", result["summary"])

    def test_daily_brief_admits_when_it_read_nothing(self):
        module = self.load_impl("daily-brief")
        context = FakeContext({})
        result = module.run(context)
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])

    def test_voice_assistant_picks_the_pack_list(self):
        module = self.load_impl("voice-assistant")
        context = FakeContext(
            {
                "voice_status": FakeOutcome(result="状态"),
                "voice_pack_list": FakeOutcome(result="装了 1 个语音包"),
            },
            request="有哪些语音包",
        )
        result = module.run(context)
        self.assertTrue(result["ok"])
        self.assertIn("语音包", result["summary"])
        self.assertEqual([name for name, _args in context.calls], ["voice_pack_list"])

    def test_voice_assistant_reports_a_failed_reading(self):
        module = self.load_impl("voice-assistant")
        context = FakeContext({"voice_status": FakeOutcome(ok=False, error="没接后端")})
        result = module.run(context)
        self.assertFalse(result["ok"])
        self.assertIn("没接后端", result["error"])

    def test_the_screen_skill_respects_the_off_switch(self):
        module = self.load_impl("screen-context")
        context = FakeContext(
            {"screen_context": FakeOutcome(ok=False, error="屏幕感知默认关着。")}
        )
        result = module.run(context)
        self.assertFalse(result["ok"])
        self.assertIn("屏幕感知默认关着", result["error"])
        self.assertIn("不会去动这个开关", result["detail"])

    def test_the_screen_skill_passes_the_read_through(self):
        module = self.load_impl("screen-context")
        context = FakeContext({"screen_context": FakeOutcome(result="前台是「记事本」。")})
        result = module.run(context)
        self.assertTrue(result["ok"])
        self.assertIn("记事本", result["summary"])
        self.assertIn("没有截图", result["detail"])


class PetHostShapeTests(unittest.TestCase):
    """兜底碰撞体积：角色在画就得拖得动。"""

    def build_host(self, hit=None, hidden=False):
        host = PetHost.__new__(PetHost)
        host._hit = hit
        host.hidden = hidden
        host.logger = None
        host.paints = []
        host._paint_hit = lambda: host.paints.append(1)
        host._apply_alpha = lambda: True
        return host

    def test_without_a_mask_the_mouse_would_pass_through(self):
        self.assertFalse(self.build_host().has_hit())

    def test_the_fallback_mask_sits_in_the_middle(self):
        host = self.build_host()
        self.assertTrue(host.set_fallback_hit())
        cols, rows, bits = host.hit_mask()
        self.assertEqual((cols, rows), (12, 12))
        self.assertTrue(any(bits))
        self.assertTrue(host.has_hit())

    def test_a_hidden_pet_does_not_claim_a_hit_area(self):
        host = self.build_host()
        host.set_fallback_hit()
        host.hidden = True
        self.assertFalse(host.has_hit())

    def test_a_bad_mask_is_refused_without_breaking_the_pet(self):
        host = self.build_host()
        host.set_fallback_hit()
        self.assertFalse(host.set_hit_mask(0, 4, b""))
        self.assertFalse(host.set_hit_mask(4, 4, b"\x00"))
        self.assertTrue(host.has_hit())

    def test_clearing_the_mask_goes_back_to_the_placeholder(self):
        host = self.build_host()
        host.set_fallback_hit()
        host.clear_hit_mask()
        self.assertFalse(host.has_hit())

    def test_an_unusable_window_is_reported_not_crashed(self):
        class Bad:
            def winfo_id(self):
                raise RuntimeError("窗口没了")

        self.assertFalse(hide_from_taskbar(Bad()))

    def test_hiding_from_the_taskbar_touches_only_this_process(self):
        self.assertIsInstance(hide_process_windows(), int)

    def test_raising_the_pet_is_refused_when_it_is_not_topmost(self):
        host = self.build_host()
        host.topmost = False
        self.assertFalse(host.keep_on_top())

    def test_raising_the_pet_lifts_the_window(self):
        host = self.build_host()
        host.topmost = True
        raised = []
        host.root = SimpleNamespace(
            attributes=lambda *args: True, lift=lambda: raised.append(1)
        )
        self.assertTrue(host.keep_on_top())
        self.assertEqual(raised, [1])


class FakeRoot:
    def __init__(self):
        self.scheduled = []

    def after(self, ms, fn=None):
        self.scheduled.append(ms)
        return "id"


class FakeHost:
    def __init__(self, hidden=False, hit=None):
        self.hidden = hidden
        self._hit = hit
        self.fallback = 0
        self.raised = 0
        self.masks = []

    def has_hit(self):
        return self._hit is not None and not self.hidden

    def set_fallback_hit(self):
        self.fallback += 1
        self._hit = "fallback"
        return True

    def set_hit_mask(self, cols, rows, bits):
        self.masks.append((cols, rows, bytes(bits)))
        self._hit = "mask"
        return True

    def keep_on_top(self):
        self.raised += 1
        return True


class FakeLive2d:
    def __init__(self):
        self.hit_requests = 0
        self.synced = 0

    def request_hit(self):
        self.hit_requests += 1

    def sync_geometry(self):
        self.synced += 1


class DesktopKeepAliveTests(unittest.TestCase):
    """桌宠要一直在最前、一直拖得动。"""

    def build_pet(self, host=None, live2d=None, ready=True, topmost=True):
        pet = DesktopPet.__new__(DesktopPet)
        pet.host = host or FakeHost()
        pet.live2d = live2d or FakeLive2d()
        pet._live2d_ready = ready
        pet._hit_misses = 0
        pet.root = FakeRoot()
        pet._warn = lambda *args, **kwargs: None
        pet._topmost_wanted = lambda: topmost
        return pet

    def test_the_repeat_rates_are_sane(self):
        self.assertGreaterEqual(DesktopPet.SHELL_MS, 500)
        self.assertGreaterEqual(DesktopPet.HIT_MS, 500)

    def test_the_pet_keeps_being_raised_and_hidden_from_the_taskbar(self):
        pet = self.build_pet()
        with mock.patch("ui.desktop.hide_from_taskbar") as hide:
            pet._shell_tick()
        self.assertEqual(pet.host.raised, 1)
        self.assertEqual(pet.live2d.synced, 1)
        hide.assert_called_once()
        self.assertEqual(pet.root.scheduled, [DesktopPet.SHELL_MS])

    def test_the_shell_tick_stays_quiet_when_the_pet_is_not_topmost(self):
        pet = self.build_pet(topmost=False)
        with mock.patch("ui.desktop.hide_from_taskbar"):
            pet._shell_tick()
        self.assertEqual(pet.host.raised, 0)

    def test_a_missing_hit_area_is_asked_for_again(self):
        pet = self.build_pet()
        pet._hit_tick()
        self.assertEqual(pet.live2d.hit_requests, 1)
        self.assertEqual(pet.host.fallback, 0)

    def test_after_two_misses_the_fallback_area_kicks_in(self):
        pet = self.build_pet()
        pet._hit_tick()
        pet._hit_tick()
        self.assertEqual(pet.host.fallback, 1)
        self.assertEqual(pet._hit_misses, 0)

    def test_a_working_hit_area_resets_the_counter(self):
        pet = self.build_pet(host=FakeHost(hit="mask"))
        pet._hit_misses = 1
        pet._hit_tick()
        self.assertEqual(pet._hit_misses, 0)
        self.assertEqual(pet.live2d.hit_requests, 0)

    def test_nothing_happens_while_the_character_is_not_ready(self):
        pet = self.build_pet(ready=False)
        pet._hit_tick()
        self.assertEqual(pet.live2d.hit_requests, 0)
        self.assertEqual(pet.host.fallback, 0)

    def test_a_hidden_pet_is_left_alone(self):
        pet = self.build_pet(host=FakeHost(hidden=True))
        pet._hit_tick()
        self.assertEqual(pet.live2d.hit_requests, 0)

    def test_an_empty_mask_falls_back_to_the_clickable_area(self):
        pet = self.build_pet()
        payload = {"data": {"cols": 4, "rows": 4, "data": base64.b64encode(b"\x00" * 2).decode()}}
        self.assertTrue(pet._apply_hit_volume(payload))
        self.assertEqual(pet.host.fallback, 1)

    def test_a_real_mask_becomes_the_clickable_area(self):
        pet = self.build_pet()
        bits = b"\x0f\x0f"
        payload = {"data": {"cols": 4, "rows": 4, "data": base64.b64encode(bits).decode()}}
        self.assertTrue(pet._apply_hit_volume(payload))
        self.assertEqual(pet.host.masks, [(4, 4, bits)])

    def test_a_broken_mask_is_refused(self):
        pet = self.build_pet()
        self.assertFalse(pet._apply_hit_volume({"data": {"cols": "x", "rows": 4, "data": "!!"}}))
        self.assertEqual(pet.host.fallback, 0)


class BrandTests(unittest.TestCase):
    """改名 Blossom，但用户数据目录故意保持原样，记忆不能丢。"""

    def test_the_product_name_is_blossom(self):
        self.assertEqual(APP_NAME, "Blossom")

    def test_the_data_folder_keeps_its_old_name(self):
        self.assertEqual(DATA_DIR_NAME, "DeskPet")

    def test_the_installed_app_still_uses_the_old_folder(self):
        """打包之后数据落在用户数据目录下的 DeskPet 文件夹，改名不搬数据。"""
        import security.config as config_module

        probe = tempfile.mkdtemp(prefix="blossom-lappdata-")
        with mock.patch.dict(
            os.environ, {"DESKPET_ROOT": "", "LOCALAPPDATA": probe}, clear=False
        ):
            with mock.patch.object(config_module.sys, "frozen", True, create=True):
                root = config_module.data_root()
        self.assertEqual(Path(root).name, "DeskPet")
        self.assertEqual(Path(root).parent, Path(probe))

    def test_the_version_module_uses_the_new_name(self):
        import app.version as version

        self.assertEqual(version.APP_NAME, "Blossom")

    def test_the_single_instance_name_and_window_title_are_sane(self):
        source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn('APP_NAME = "Blossom"', source)
        self.assertIn('MUTEX_NAME = "Blossom.SingleInstance"', source)
        self.assertIn('WINDOW_TITLE = "桌宠"', source)

    def test_the_spec_file_is_renamed(self):
        self.assertTrue((ROOT / "Blossom.spec").is_file())
        self.assertFalse((ROOT / "DeskPet.spec").exists())

    def test_the_build_script_points_at_the_new_exe_and_shortcut(self):
        text = (ROOT / "build.ps1").read_text(encoding="utf-8")
        self.assertIn("dist\\Blossom\\Blossom.exe", text)
        self.assertIn("Blossom.lnk", text)
        self.assertIn("DeskPet.lnk", text)

    def test_the_diagnostic_report_names_blossom(self):
        text = (ROOT / "logkit" / "report.py").read_text(encoding="utf-8")
        self.assertIn("Blossom", text)


class FakeMemory:
    def __init__(self):
        self.saved = []

    def remember(self, text, **meta):
        self.saved.append(text)
        return {"id": len(self.saved)}

    def search(self, query, limit=8):
        return [{"text": "记得 " + str(query), "source": "local"}][:limit]

    def update(self, memory_id, note):
        return True

    def forget(self, memory_id=""):
        return True


class FakeOutcome:
    def __init__(self, ok=True, result="", error="", verified=True):
        self.ok = ok
        self.result = result
        self.error = error
        self.verified = verified


class FakeContext:
    def __init__(self, outcomes, request="", inputs=None):
        self._outcomes = dict(outcomes)
        self.request = request
        self.inputs = dict(inputs or {})
        self.calls = []

    def tool(self, name, **arguments):
        self.calls.append((name, arguments))
        return self._outcomes.get(name, FakeOutcome(ok=False, error="没接这个工具"))


if __name__ == "__main__":
    unittest.main()
