"""角色画面起不来的原因要查清楚、说清楚。

用户反馈过「角色不显示，只剩占位形象」，但界面上只有一句泛指。
Windows 上最常见的原因其实是缺 Edge WebView2 运行时：pywebview 装得再全，
系统里没这个运行时也开不出窗口。这一层把结论查出来，把"装一次就好"写进提示。
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters import local_runtimes
from avatar import web2d
from avatar.web2d import NO_WEBVIEW2, WebLive2DHost
from security.config import Config
from ui import wizard


class WebView2ProbeTests(unittest.TestCase):
    """探测本身：查清了才下结论，查不清就说查不清。"""

    def test_other_systems_say_so(self):
        with mock.patch.object(local_runtimes.os, "name", "posix"):
            state = local_runtimes.webview2_state()
        self.assertIsNone(state["installed"])
        self.assertIn("Windows", state["reason"])

    def test_the_registry_version_is_the_first_thing_checked(self):
        with mock.patch.object(
            local_runtimes, "_webview2_registry_version", return_value=(True, "1.3.1234.5")
        ):
            state = local_runtimes.webview2_state()
        self.assertTrue(state["installed"])
        self.assertEqual(state["version"], "1.3.1234.5")

    def test_a_portable_runtime_folder_also_counts(self):
        with mock.patch.object(
            local_runtimes, "_webview2_registry_version", return_value=(True, "")
        ), mock.patch.object(local_runtimes, "_webview2_files_present", return_value=True):
            state = local_runtimes.webview2_state()
        self.assertTrue(state["installed"], "便携版运行时放在程序旁边也要认")
        self.assertEqual(state["version"], "")

    def test_a_real_absence_is_reported_as_missing(self):
        with mock.patch.object(
            local_runtimes, "_webview2_registry_version", return_value=(True, "")
        ), mock.patch.object(local_runtimes, "_webview2_files_present", return_value=False):
            state = local_runtimes.webview2_state()
        self.assertFalse(state["installed"])
        self.assertIn("WebView2", state["reason"])

    def test_an_unreadable_registry_is_not_reported_as_missing(self):
        """读不了注册表时不许说"没装"，否则能用的机器会被误判成不能用。"""
        with mock.patch.object(
            local_runtimes, "_webview2_registry_version", return_value=(False, "")
        ), mock.patch.object(local_runtimes, "_webview2_files_present", return_value=False):
            state = local_runtimes.webview2_state()
        self.assertIsNone(state["installed"])
        self.assertIn("查不出", state["reason"])

    def test_the_entry_lists_as_a_capability(self):
        entries = {item["id"]: item for item in local_runtimes.detect()["entries"]}
        entry = entries["live2d-render"]
        for key in ("id", "kind", "label", "installed", "ready", "needs", "license"):
            self.assertIn(key, entry)
        self.assertEqual(entry["kind"], "render")
        self.assertIn("render", local_runtimes.KIND_LABEL)
        if entry["ready"]:
            self.assertTrue(entry["installed"])

    def test_missing_runtime_shows_up_in_the_capability_text(self):
        with mock.patch.object(
            local_runtimes, "_webview2_registry_version", return_value=(True, "")
        ), mock.patch.object(local_runtimes, "_webview2_files_present", return_value=False):
            payload = local_runtimes.detect()
        body = local_runtimes.text(payload)
        self.assertIn("WebView2", body)
        self.assertIn("角色画面", body)

    def test_the_probe_never_installs_or_downloads(self):
        source = Path(local_runtimes.__file__).read_text(encoding="utf-8")
        for bad in ("pip install", "urlopen", "subprocess.run([\"winget", "DownloadFile"):
            self.assertNotIn(bad, source)


class RenderHostTests(unittest.TestCase):
    """渲染宿主：缺 WebView2 时给一句能照做的提示，而不是一句泛指。"""

    def host(self):
        return WebLive2DHost(logger=None)

    def test_missing_webview2_blocks_with_an_actionable_reason(self):
        host = self.host()
        with mock.patch.object(web2d, "_webview_ready", return_value=True), mock.patch.object(
            host, "missing_files", return_value=[]
        ), mock.patch.object(web2d, "webview2_missing", return_value=True):
            self.assertFalse(host.is_available())
        self.assertEqual(host.unavailable_reason(), NO_WEBVIEW2)
        self.assertIn("装一次", NO_WEBVIEW2)

    def test_an_unknown_state_does_not_block_rendering(self):
        """查不出装没装时照旧去开窗口：宁可试一次，也不要凭空说人家不能用。"""
        host = self.host()
        with mock.patch.object(web2d, "_webview_ready", return_value=True), mock.patch.object(
            host, "missing_files", return_value=[]
        ), mock.patch.object(web2d, "webview2_missing", return_value=False):
            self.assertTrue(host.is_available())

    def test_the_runtime_files_are_checked_before_webview2(self):
        host = self.host()
        with mock.patch.object(web2d, "_webview_ready", return_value=True), mock.patch.object(
            host, "missing_files", return_value=["renderer.html"]
        ), mock.patch.object(web2d, "webview2_missing", return_value=True):
            self.assertFalse(host.is_available())
        self.assertEqual(host.unavailable_reason(), web2d.NO_RUNTIME)

    def test_no_pywebview_is_still_reported_as_before(self):
        host = self.host()
        with mock.patch.object(web2d, "_webview_ready", return_value=False):
            self.assertFalse(host.is_available())
        self.assertIn("渲染环境", host.unavailable_reason())

    def test_the_note_matches_the_three_real_states(self):
        with mock.patch.object(
            web2d, "webview2_state", return_value={"installed": False, "version": "", "reason": "x"}
        ):
            self.assertEqual(web2d.webview2_note(), NO_WEBVIEW2)
        with mock.patch.object(
            web2d, "webview2_state", return_value={"installed": True, "version": "1.2.3", "reason": ""}
        ):
            self.assertIn("1.2.3", web2d.webview2_note())
        with mock.patch.object(
            web2d, "webview2_state", return_value={"installed": None, "version": "", "reason": "读不了"}
        ):
            self.assertIn("查不出", web2d.webview2_note())

    def test_a_broken_probe_does_not_break_the_note(self):
        with mock.patch.object(
            web2d, "webview2_state", side_effect=RuntimeError("探测层炸了")
        ):
            self.assertIn("查不出", web2d.webview2_note())
            self.assertFalse(web2d.webview2_missing())


class WizardTextTests(unittest.TestCase):
    """向导和设置页必须说同一句真话。"""

    def test_the_wizard_repeats_the_real_state(self):
        with mock.patch("avatar.web2d.webview2_state", return_value={
            "installed": False, "version": "", "reason": "没有找到 Edge WebView2 运行时"
        }):
            self.assertIn("WebView2", wizard.environment_text())

    def test_the_wizard_body_has_a_character_section(self):
        config = Config(load_env=False)
        text = wizard.body_text(None, config)
        self.assertIn("角色画面", text)
        self.assertIn("透明拖动区域", text)
        self.assertIn(str(config.live2d_dir), text)

    def test_a_broken_probe_does_not_break_the_wizard(self):
        with mock.patch("avatar.web2d.webview2_note", side_effect=RuntimeError("炸了")):
            self.assertTrue(wizard.environment_text())


if __name__ == "__main__":
    unittest.main()
