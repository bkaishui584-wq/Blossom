"""发行准备验收：能力包、路径分离、安装与卸载的安全约束。

盯这几件事：
  1. 能力包能装能卸，装坏了不留残骸，越界路径一律拒绝；
  2. 程序目录、数据目录、能力包目录三者分开，数据不往安装目录写；
  3. 卸载默认保留用户数据，只有显式 --purge 才删；
  4. 基础包不带文档解析和离线语音；发行包不带 .env；
  5. 项目许可是专有许可，并指向第三方声明。

全程临时目录，不碰真实用户数据。
"""

import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from extensions import packs  # noqa: E402
from security.config import Config  # noqa: E402


def make_pack(path, name="demo", title="演示包", extra=None, manifest_name=None):
    """做一个最小的能力包，用来验装卸流程。"""
    payload = {"name": name, "title": title, "version": "1.0.0", "provides": ["demo_module"]}
    payload.update(extra or {})
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(manifest_name or packs.MANIFEST_NAME, json.dumps(payload))
        archive.writestr("site/demo_module.py", "VALUE = 42\n")
    return Path(path)


class PackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"DESKPET_ROOT": str(self.base)})
        env.start()
        self.addCleanup(env.stop)
        self.config = Config(load_env=False)

    def test_without_any_pack_everything_is_empty(self):
        self.assertEqual(packs.installed(self.config), [])
        self.assertEqual(packs.activate(self.config), [])
        self.assertTrue(packs.packs_dir(self.config).as_posix().endswith("packs"))

    def test_install_then_use_then_remove(self):
        archive = make_pack(self.base / "demo.zip")
        result = packs.install(self.config, archive)
        self.assertTrue(result["ok"], result)
        site = Path(result["path"]) / "site"
        self.assertTrue(site.is_dir())
        names = [row["name"] for row in packs.installed(self.config)]
        self.assertEqual(names, ["demo"])
        sys.path.insert(0, str(site))
        try:
            import demo_module

            self.assertEqual(demo_module.VALUE, 42)
        finally:
            sys.path.remove(str(site))
            sys.modules.pop("demo_module", None)
        self.assertTrue(packs.remove(self.config, "demo")["ok"])
        self.assertEqual(packs.installed(self.config), [])

    def test_installing_the_same_pack_twice_is_refused(self):
        archive = make_pack(self.base / "demo.zip")
        self.assertTrue(packs.install(self.config, archive)["ok"])
        again = packs.install(self.config, archive)
        self.assertFalse(again["ok"])
        self.assertIn("已经装过", again["error"])

    def test_a_pack_that_tries_to_escape_is_refused(self):
        bad = self.base / "bad.zip"
        with zipfile.ZipFile(bad, "w") as archive:
            archive.writestr("pack.json", json.dumps({"name": "bad"}))
            archive.writestr("../outside.py", "x = 1\n")
        result = packs.install(self.config, bad)
        self.assertFalse(result["ok"])
        self.assertFalse((self.base.parent / "outside.py").exists())
        self.assertEqual(packs.installed(self.config), [])

    def test_a_folder_whose_name_does_not_match_its_manifest_is_ignored(self):
        folder = packs.packs_dir(self.config) / "renamed"
        (folder / "site").mkdir(parents=True)
        (folder / packs.MANIFEST_NAME).write_text(
            json.dumps({"name": "other", "title": "改过名"}), encoding="utf-8"
        )
        self.assertEqual(packs.installed(self.config), [])

    def test_a_broken_zip_leaves_nothing_behind(self):
        broken = self.base / "broken.zip"
        broken.write_text("这不是 zip", encoding="utf-8")
        result = packs.install(self.config, broken)
        self.assertFalse(result["ok"])
        folder = packs.packs_dir(self.config)
        self.assertFalse(folder.exists() and any(folder.iterdir()), "坏包不该留下半个目录")

    def test_removing_something_that_is_not_there_is_reported(self):
        result = packs.remove(self.config, "没有这个包")
        self.assertFalse(result["ok"])
        self.assertIn("没有装", result["error"])

    def test_packs_live_under_the_data_root_not_the_program_dir(self):
        root = packs.packs_dir(self.config).resolve()
        self.assertTrue(str(root).startswith(str(self.base.resolve())))


class LayoutTests(unittest.TestCase):
    """程序目录、数据目录、能力包目录分开。"""

    def test_a_frozen_app_keeps_user_data_away_from_the_install_dir(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\someone\AppData\Local"}):
            with mock.patch.object(sys, "frozen", True, create=True):
                from security.config import data_root

                root = data_root(r"C:\Program Files\Blossom")
        self.assertNotIn("Program Files", str(root))
        self.assertIn("DeskPet", str(root))

    def test_a_frozen_app_reads_resources_next_to_the_exe(self):
        from security.config import resource_path

        with mock.patch.object(sys, "_MEIPASS", r"C:\tmp\unpacked", create=True):
            path = resource_path("deskpet.ico")
        self.assertEqual(Path(path), Path(r"C:\tmp\unpacked\resources\deskpet.ico"))

    def test_the_working_directory_does_not_decide_where_data_goes(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name) / "data"
        home.mkdir()
        before = os.getcwd()
        try:
            os.chdir(tmp.name)
            with mock.patch.dict(os.environ, {"DESKPET_ROOT": str(home)}):
                config = Config(load_env=False)
        finally:
            os.chdir(before)
        self.assertEqual(config.data_root.resolve(), home.resolve())


class ReleaseAssetTests(unittest.TestCase):
    """发行脚本与清单里的硬约束，防止以后改回去。"""

    def spec_text(self):
        return (ROOT / "Blossom.spec").read_text(encoding="utf-8")

    def test_the_base_package_leaves_the_optional_stacks_out(self):
        spec = self.spec_text()
        for name in (
            "markitdown",
            "sherpa_onnx",
            "soundcard",
            "onnxruntime",
            "numpy",
            "magika",
        ):
            self.assertIn('"%s"' % name, spec, name)
        self.assertNotIn('collect_optional("sherpa_onnx")', spec)
        self.assertNotIn('collect_optional("markitdown")', spec)
        self.assertNotIn("HAS_MARKITDOWN", spec)

    def test_the_base_package_drops_packages_nothing_imports(self):
        spec = self.spec_text()
        for name in ("flask", "redis", "sentry_sdk", "opentelemetry"):
            self.assertIn('"%s"' % name, spec, name)

    def test_the_release_package_never_carries_a_config_file(self):
        text = (ROOT / "package.ps1").read_text(encoding="utf-8")
        self.assertIn('".env"', text)
        self.assertIn("Remove-Item", text)

    def test_uninstall_keeps_user_data_unless_asked(self):
        text = (ROOT / "packaging" / "uninstall.ps1").read_text(encoding="utf-8")
        self.assertIn("$Purge", text)
        self.assertIn("YES", text, "删数据要有二次确认")
        self.assertIn("DeskPet", text)
        # 删数据那一段只能在 -Purge 分支里出现。
        purge_block = text.split("if ($Purge)", 1)[1]
        self.assertIn("Remove-Item -LiteralPath $data -Recurse -Force", purge_block)
        # 薄壳要能把 --purge 转成 -Purge，否则用户永远删不掉数据。
        wrapper = (ROOT / "packaging" / "uninstall.cmd").read_text(encoding="utf-8")
        self.assertIn("--purge", wrapper)
        self.assertIn("uninstall.ps1", wrapper)

    def test_the_installer_does_not_need_admin_rights_and_makes_a_shortcut(self):
        text = (ROOT / "packaging" / "install.ps1").read_text(encoding="utf-8")
        self.assertIn("Programs\\Blossom", text)
        self.assertNotIn("runas", text.lower())
        self.assertNotIn("Program Files", text)
        # 装完必须留下桌面图标，这是用户明确提过的要求。
        self.assertIn("Blossom.lnk", text)
        self.assertIn("GetFolderPath(\"Desktop\")", text)

    def test_the_batch_wrappers_stay_plain_ascii_without_a_bom(self):
        """cmd 会把 BOM 当成命令，非 ASCII 又会被代码页改写，两种都会让安装脚本直接不跑。"""
        for name in ("install.cmd", "uninstall.cmd", "setup-bootstrap.cmd"):
            raw = (ROOT / "packaging" / name).read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), name + " 带了 BOM")
            self.assertLess(max(raw), 128, name + " 里有非 ASCII 字节")

    def test_the_power_shell_helpers_carry_a_bom_for_their_chinese_text(self):
        """PowerShell 5.1 读无 BOM 文件按 ANSI 解，中文串会把语法读坏。"""
        for name in ("install.ps1", "uninstall.ps1"):
            raw = (ROOT / "packaging" / name).read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), name + " 少了 BOM")

    def test_the_release_carries_the_whole_installer(self):
        text = (ROOT / "package.ps1").read_text(encoding="utf-8")
        for name in ("install.cmd", "install.ps1", "uninstall.cmd", "uninstall.ps1"):
            self.assertIn(name, text, "发行包少了 %s" % name)

    def test_the_release_replaces_superseded_capability_packs(self):
        text = (ROOT / "package.ps1").read_text(encoding="utf-8")
        self.assertIn("$incomingPacks", text)
        self.assertIn("Group-Object", text)
        self.assertIn("${family}-*.zip", text)
        self.assertIn("${family}-*.zip.sha256", text)
        self.assertIn("Remove-Item", text)

    def test_the_bundled_model_is_the_only_model_that_ships(self):
        models = ROOT / "resources" / "live2d_models"
        folders = sorted(item.name for item in models.iterdir() if item.is_dir())
        self.assertEqual(folders, ["live2D0"], "随包只带用户做好的那个模型")
        folder = models / "live2D0"
        self.assertTrue((folder / "live2D0.model3.json").is_file())
        self.assertTrue((folder / "live2D0.moc3").is_file())
        self.assertIn("live2d_models", self.spec_text(), "随包模型要真的打进 EXE")

    def test_the_project_license_is_proprietary_and_points_at_notices(self):
        text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("保留所有权利", text)
        self.assertIn("THIRD-PARTY-NOTICES.md", text)
        notices = (ROOT / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
        self.assertIn("基础包里的组件", notices)
        self.assertIn("能力包里的组件", notices)
        for name in ("openai", "pywebview", "sherpa", "markitdown"):
            self.assertIn(name, notices, name)

    def test_the_pack_builder_knows_its_pack_names(self):
        text = (ROOT / "build_packs.py").read_text(encoding="utf-8")
        self.assertIn('"docs"', text)
        self.assertIn('"voice"', text)
        # 发行名和模块名不一样，写错了包会悄悄少东西，所以种子必须是发行名。
        self.assertIn("python-pptx", text)
        self.assertNotIn('"pptx",', text)

    def test_the_install_guide_exists_and_covers_the_essentials(self):
        text = (ROOT / "docs" / "install-guide.md").read_text(encoding="utf-8")
        for needle in ("%LOCALAPPDATA%\\DeskPet", "uninstall.cmd", "--purge", "备份", "没有密钥也能用"):
            self.assertIn(needle, text, needle)

    def test_the_runtime_version_matches_the_release_name(self):
        from app.version import VERSION, describe

        self.assertEqual(VERSION, "7.0.2")
        self.assertEqual(describe(), "Blossom 7.0.2")
        self.assertIn('version=str(ROOT / "resources" / "version_info.txt")', self.spec_text())

    def test_the_windows_version_resources_are_valid_and_branded(self):
        from PyInstaller.utils.win32.versioninfo import load_version_info_from_text_file

        app = load_version_info_from_text_file(str(ROOT / "resources" / "version_info.txt"))
        setup = load_version_info_from_text_file(
            str(ROOT / "resources" / "version_info_setup.txt")
        )
        self.assertIn("filevers=(7, 0, 2, 0)", str(app))
        self.assertIn("Blossom 桌面智能体", str(app))
        self.assertIn("OriginalFilename', 'Blossom.exe", str(app))
        self.assertIn("Blossom 安装程序", str(setup))
        self.assertIn("Blossom-7.0.2-win64-setup.exe", str(setup))

    def test_packaging_applies_version_info_to_the_setup_exe(self):
        text = (ROOT / "package.ps1").read_text(encoding="utf-8")
        self.assertIn("packaging\\apply_version_info.py", text)
        self.assertIn("resources\\version_info_setup.txt", text)

    def test_signing_is_optional_but_wired_into_packaging(self):
        package = (ROOT / "package.ps1").read_text(encoding="utf-8")
        signer = (ROOT / "packaging" / "sign-file.ps1").read_text(encoding="utf-8")
        self.assertIn("SignThumbprint", package)
        self.assertIn("packaging\\sign-file.ps1", package)
        self.assertIn("Set-AuthenticodeSignature", signer)
        self.assertIn("1.3.6.1.5.5.7.3.3", signer)
        self.assertIn("TimestampServer", signer)
        self.assertTrue((ROOT / "docs" / "release-signing.md").is_file())


if __name__ == "__main__":
    unittest.main()
