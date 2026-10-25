"""设置页「模型与 API 管理」和会话级模型切换。

真窗口、临时数据库、假密钥：只验证界面接线、配置落盘、密钥不明文、
以及切换模型只影响当前会话。不访问网络，不调用真实模型。
"""

import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.main import build_agent
from database.db import Database
from logkit.logger import get_logger
from model.base import ChatDelta, ChatResult, ModelProvider, Usage
from persona import profile as persona_profile
from security.config import Config
from ui.desktop import DesktopPet


class StubProvider(ModelProvider):
    name = "stub"

    def is_available(self):
        return True

    def chat(self, messages, tools=None, temperature=0.7):
        return ChatResult(ok=True, content="在。", usage=Usage(), model="stub")

    def stream(self, messages, tools=None, temperature=0.7):
        yield ChatDelta(done=True)


class ModelPanelTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        self.root.withdraw()
        self.addCleanup(self._destroy)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        config = Config(load_env=False)
        config.data_root = self.base
        config.data_dir = self.base / "data"
        config.log_dir = self.base / "logs"
        config.db_path = self.base / "t.sqlite3"
        config.characters_dir = self.base / "characters"
        config.live2d_dir = self.base / "live2d"
        config.devices_dir = self.base / "devices"
        config.live2d_render = False
        config.ensure_dirs()
        from model.config import ModelConfigRepository
        from security.credentials import SecretStore

        config.model_configs = ModelConfigRepository(config.data_dir)
        config.secrets = SecretStore(config.data_dir / "secrets")
        self.config = config
        database = Database(config.db_path)
        database.migrate()
        self.agent, self.memory = build_agent(config, database, StubProvider(), get_logger())
        self.pet = DesktopPet(self.agent, config, memory=self.memory, root=self.root)
        self.window = self.pet.open_settings()
        self.window.book.select(1)
        self.panel = self.window.model_panel

    def _destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def save_deepseek(self, key="sk-ui-test-1234567890", **overrides):
        panel = self.panel
        panel.provider_var.set("deepseek")
        panel.on_provider()
        panel.name_var.set(overrides.get("name", "DeepSeek 测试"))
        panel.model_var.set(overrides.get("model", "deepseek-chat"))
        panel.role_vars["companion"].set(True)
        panel.role_vars["task"].set(overrides.get("task", False))
        if key:
            panel.key_var.set(key)
        return panel.save()

    # ---------- 界面 ----------
    def test_the_form_starts_with_a_provider_preset(self):
        self.panel.provider_var.set("qwen")
        self.panel.on_provider()
        self.assertIn("dashscope.aliyuncs.com", self.panel.base_var.get())
        self.assertEqual(self.panel.model_var.get(), "qwen-plus")

    def test_the_key_field_is_masked(self):
        self.assertEqual(str(self.panel.key_entry.cget("show")), "*")

    def test_saving_writes_config_and_hides_the_key(self):
        self.assertTrue(self.save_deepseek(), self.panel.status_label.cget("text"))
        rows = self.config.model_configs.list()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["provider"], "deepseek")
        self.assertEqual(rows[0]["roles"], ["companion"])
        self.assertTrue(self.config.secrets.has(rows[0]["secret_id"]))
        text = Path(self.config.model_configs.path).read_text(encoding="utf-8")
        self.assertNotIn("sk-ui-test-1234567890", text)
        self.assertNotIn("api_key", text.lower())

    def test_saving_rebuilds_the_live_provider(self):
        self.assertTrue(self.save_deepseek())
        provider = self.agent.models.companion.provider
        inner = getattr(provider, "inner", provider)
        self.assertEqual(type(inner).__name__, "DeepSeekProvider")
        self.assertIn("deepseek-chat", self.pet.chat.model_text())

    def test_http_non_local_address_is_refused(self):
        self.panel.provider_var.set("custom")
        self.panel.on_provider()
        self.panel.base_var.set("http://example.com/v1")
        self.panel.model_var.set("m")
        self.panel.key_var.set("sk-x")
        self.assertFalse(self.panel.save())
        self.assertIn("https", self.panel.status_label.cget("text"))
        self.assertEqual(self.config.model_configs.list(), [])

    def test_the_docs_button_is_disabled_without_a_link(self):
        self.panel.provider_var.set("custom")
        self.panel.on_provider()
        self.assertEqual(str(self.panel.docs_button.cget("state")), "disabled")
        self.panel.provider_var.set("deepseek")
        self.panel.on_provider()
        self.assertEqual(str(self.panel.docs_button.cget("state")), "normal")

    def test_connection_test_uses_the_real_probe_and_reports(self):
        self.assertTrue(self.save_deepseek())
        row = self.config.model_configs.list()[0]
        canned = {"ok": True, "kind": "ok", "status": 200, "latency_ms": 12, "message": "连接正常（12 ms）"}
        with mock.patch(
            "model.adapter.DeepSeekProvider.probe", return_value=canned
        ) as probe, mock.patch("tkinter.messagebox.askyesno", return_value=True):
            self.assertTrue(self.panel.test_connection())
        self.assertTrue(probe.called)
        self.assertEqual(self.panel.last_probe[row["id"]]["status"], 200)
        self.assertIn("连接正常", self.panel.status_label.cget("text"))

    def test_fetching_models_fills_the_dropdown(self):
        self.assertTrue(self.save_deepseek())
        with mock.patch(
            "model.adapter.DeepSeekProvider.list_models",
            return_value=["deepseek-chat", "deepseek-reasoner"],
        ):
            self.assertTrue(self.panel.fetch_models())
        self.assertEqual(
            list(self.panel.model_combo.cget("values")),
            ["deepseek-chat", "deepseek-reasoner"],
        )

    def test_fetching_models_without_support_says_so(self):
        self.assertTrue(self.save_deepseek())
        with mock.patch("model.adapter.DeepSeekProvider.list_models", return_value=[]):
            self.assertFalse(self.panel.fetch_models())
        self.assertIn("手动填", self.panel.status_label.cget("text"))

    def test_deleting_can_keep_or_drop_the_key(self):
        self.assertTrue(self.save_deepseek())
        row = self.config.model_configs.list()[0]
        with mock.patch("tkinter.messagebox.askyesno", side_effect=[True, False]):
            self.assertTrue(self.panel.delete())
        self.assertEqual(self.config.model_configs.list(), [])
        self.assertTrue(self.config.secrets.has(row["secret_id"]), "只删配置时密钥要留着")

        self.assertTrue(self.save_deepseek(key="sk-ui-second-123456"))
        row = self.config.model_configs.list()[0]
        with mock.patch("tkinter.messagebox.askyesno", side_effect=[True, True]):
            self.assertTrue(self.panel.delete())
        self.assertFalse(self.config.secrets.has(row["secret_id"]))

    # ---------- 会话级切换 ----------
    def test_switching_affects_only_the_current_session(self):
        self.assertTrue(self.save_deepseek(name="聊天 A", model="deepseek-chat"))
        first = self.config.model_configs.list()[0]
        self.panel.new_config()
        self.assertTrue(
            self.save_deepseek(name="聊天 B", model="deepseek-reasoner", key="sk-ui-b-1234567890")
        )
        rows = {row["model"]: row for row in self.config.model_configs.list()}
        second = rows["deepseek-reasoner"]

        persona_profile.save_text(self.memory, "我的人格不动。", note="测试")
        self.assertTrue(self.agent.set_session_model("s1", second["id"]))
        self.assertEqual(self.agent.provider_for("s1").model, "deepseek-reasoner")
        self.assertEqual(self.agent.provider_for("s2").model, first["model"])
        self.assertEqual(persona_profile.text(self.memory), "我的人格不动。")

        self.assertTrue(self.agent.set_session_model("s1", ""))
        self.assertNotEqual(self.agent.provider_for("s1").model, "deepseek-reasoner")

    def test_an_unknown_config_cannot_be_selected(self):
        self.assertFalse(self.agent.set_session_model("s1", "not-a-config"))

    def test_the_chat_menu_lists_real_configs_and_can_switch(self):
        self.assertTrue(self.save_deepseek(name="菜单里的配置"))
        row = self.config.model_configs.list()[0]
        self.pet.show_chat()
        labels = []
        with mock.patch.object(tk.Menu, "tk_popup", lambda *a, **k: None):
            menu = self.pet._chat_model_menu()
        for index in range(menu.index("end") + 1):
            if menu.type(index) == "command":
                labels.append(str(menu.entrycget(index, "label")))
        self.assertTrue(any("菜单里的配置" in label for label in labels), labels)
        self.assertTrue(self.pet._chat_use_model(row["id"]))
        self.assertEqual(self.agent.session_model(self.pet.session_id), row["id"])


if __name__ == "__main__":
    unittest.main()
