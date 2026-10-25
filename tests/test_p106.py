"""发行相关的两件事：随包角色模型、桌面快捷方式。

用户反馈过"别人装完看到的是旧形象"，根因就是发行包里没带角色模型，
而且模型选择是按目录名排序取第一个。这份测试守住现在的做法：
包里的模型跟着程序走，装完就能看到用户做好的那个。

另一半是"装完桌面没有图标"：安装脚本建图标，桌宠自己也能补一个。
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from avatar import live2d
from security import startup


class FakeLogger:
    def __init__(self):
        self.warnings = []
        self.infos = []

    def warning(self, message, *args):
        self.warnings.append(message % args if args else message)

    def info(self, message, *args):
        self.infos.append(message % args if args else message)


def make_model(folder, name):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / (name + ".model3.json")).write_text(
        '{"Version": 3, "FileReferences": {"Moc": "%s.moc3", "Textures": ["x/texture_00.png"]}}'
        % name,
        encoding="utf-8",
    )
    (folder / (name + ".moc3")).write_bytes(b"moc")
    return folder


class FakeConfig:
    def __init__(self, live2d_dir):
        self.live2d_dir = str(live2d_dir)


class SeedTests(unittest.TestCase):
    """种子逻辑本身：只补不覆盖。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.bundled = self.base / "bundled"
        make_model(self.bundled / "live2D0", "live2D0")
        self.user = self.base / "user-live2d"
        self.config = FakeConfig(self.user)
        patcher = mock.patch.object(live2d, "bundled_models_dir", return_value=self.bundled)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_first_run_puts_the_pet_into_the_user_folder(self):
        logger = FakeLogger()
        copied = live2d.seed_bundled_models(self.config, logger=logger)
        self.assertEqual([Path(item).name for item in copied], ["live2D0"])
        self.assertTrue((self.user / "live2D0" / "live2D0.model3.json").is_file())
        self.assertTrue((self.user / "live2D0" / "live2D0.moc3").is_file())
        self.assertTrue(logger.infos, "放进去之后要留一条日志，方便排查")
        self.assertEqual(logger.warnings, [])

    def test_the_second_run_changes_nothing(self):
        live2d.seed_bundled_models(self.config)
        self.assertEqual(live2d.seed_bundled_models(self.config), [])

    def test_a_users_own_model_is_never_overwritten(self):
        mine = self.user / "live2D0"
        mine.mkdir(parents=True)
        marker = mine / "mine.txt"
        marker.write_text("我的模型", encoding="utf-8")
        live2d.seed_bundled_models(self.config)
        self.assertTrue(marker.is_file(), "用户自己的模型被覆盖了")
        self.assertFalse((mine / "live2D0.moc3").exists())

    def test_a_missing_or_absent_bundle_is_not_an_error(self):
        with mock.patch.object(live2d, "bundled_models_dir", return_value=None):
            self.assertEqual(live2d.seed_bundled_models(self.config), [])
        with mock.patch.object(
            live2d, "bundled_models_dir", return_value=self.base / "not-there"
        ):
            self.assertEqual(live2d.seed_bundled_models(self.config), [])

    def test_a_failed_copy_is_reported_and_does_not_raise(self):
        logger = FakeLogger()
        with mock.patch.object(live2d.shutil, "copytree", side_effect=OSError("磁盘满了")):
            self.assertEqual(live2d.seed_bundled_models(self.config, logger=logger), [])
        self.assertEqual(len(logger.warnings), 1)
        self.assertIn("磁盘满了", logger.warnings[0])

    def test_the_seeded_pet_is_the_one_that_gets_picked(self):
        live2d.seed_bundled_models(self.config)
        models = live2d.find_models(self.user)
        self.assertEqual([model.id for model in models], ["live2D0"])
        self.assertEqual(live2d.pick_model(models, "").id, "live2D0")
        self.assertEqual(live2d.pick_model(models, "live2D0").id, "live2D0")


class RealBundleTests(unittest.TestCase):
    """真正要随包发出去的那份资源。不 mock，直接看仓库。"""

    def test_the_project_ships_exactly_one_model(self):
        folder = ROOT / "resources" / "live2d_models"
        self.assertTrue(folder.is_dir(), "找不到随包模型目录")
        names = sorted(item.name for item in folder.iterdir() if item.is_dir())
        self.assertEqual(names, ["live2D0"], "旧模型不能再跟着发包")

    def test_seeding_the_real_bundle_gives_a_usable_model(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        target = Path(tmp.name) / "live2d"
        copied = live2d.seed_bundled_models(FakeConfig(target))
        self.assertEqual([Path(item).name for item in copied], ["live2D0"])
        models = live2d.find_models(target)
        self.assertEqual(live2d.pick_model(models, "").id, "live2D0")
        self.assertEqual(live2d.pick_model(models, "").missing_files(), ())


class ShortcutTests(unittest.TestCase):
    """桌面快捷方式：能建就建，建不成说清原因。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.desktop = self.base / "Desktop"
        self.desktop.mkdir()
        self.exe = self.base / "Blossom.exe"
        self.exe.write_bytes(b"exe")
        self.link = self.desktop / "Blossom.lnk"

    def frozen(self):
        """把这次调用伪装成打包后的 EXE 在跑。"""
        return [
            mock.patch.object(sys, "frozen", True, create=True),
            mock.patch.object(sys, "executable", str(self.exe)),
            mock.patch.object(startup, "desktop_dir", return_value=self.desktop),
        ]

    def run_frozen(self, side_effect):
        """在伪装环境里跑一次，把传给 PowerShell 的参数也带回来。"""
        patches = self.frozen()
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        with mock.patch.object(startup.subprocess, "run") as runner:
            runner.side_effect = side_effect
            result = startup.create_desktop_shortcut()
        return result, (runner.call_args[0][0] if runner.call_args else [])

    @unittest.skipUnless(os.name == "nt", "桌面快捷方式是 Windows 的事")
    def test_the_helper_is_honest_in_development_mode(self):
        result = startup.create_desktop_shortcut()
        self.assertFalse(result["ok"])
        self.assertIn("开发模式", result["error"])

    def test_another_system_says_so_instead_of_pretending(self):
        with mock.patch.object(startup.os, "name", "posix"):
            result = startup.create_desktop_shortcut()
            self.assertFalse(result["ok"])
            self.assertIn("不支持", result["error"])
            self.assertIsNone(startup.desktop_dir())
            self.assertIsNone(startup.desktop_shortcut_path())
            self.assertFalse(startup.has_desktop_shortcut())

    def test_the_icon_is_called_blossom_and_sits_on_the_desktop(self):
        with mock.patch.object(startup, "desktop_dir", return_value=self.desktop):
            self.assertEqual(startup.desktop_shortcut_path(), self.link)
            self.assertFalse(startup.has_desktop_shortcut())
            self.link.write_text("", encoding="utf-8")
            self.assertTrue(startup.has_desktop_shortcut())

    def test_a_frozen_app_gets_a_shortcut_pointing_at_the_exe(self):
        def fake_powershell(command, **kwargs):
            Path(command[6]).write_bytes(b"lnk")
            return subprocess.CompletedProcess(command, 0, "", "")

        result, command = self.run_frozen(fake_powershell)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["path"], str(self.link))
        self.assertTrue(self.link.is_file())
        # 参数按位置给：图标路径、EXE、工作目录，空格和中文都不会被拆错。
        self.assertEqual(command[:5], ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"])
        self.assertEqual(command[6], str(self.link))
        self.assertEqual(command[7], str(self.exe))
        self.assertEqual(command[8], str(self.exe.parent))
        # 用完就删，别在用户的临时目录里留脚本。
        self.assertFalse(Path(command[5]).exists())

    def test_a_failure_is_reported_with_the_reason(self):
        def fake_powershell(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, "", "拒绝访问")

        result, _command = self.run_frozen(fake_powershell)
        self.assertFalse(result["ok"])
        self.assertIn("拒绝访问", result["error"])

    def test_a_silently_missing_icon_counts_as_failure(self):
        def fake_powershell(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, "", "")

        result, _command = self.run_frozen(fake_powershell)
        self.assertFalse(result["ok"], "PowerShell 说成功但图标不在，不能算成功")

    def test_the_old_icon_name_is_cleaned_up(self):
        stale = self.desktop / "DeskPet.lnk"
        stale.write_bytes(b"lnk")

        def fake_powershell(command, **kwargs):
            Path(command[6]).write_bytes(b"lnk")
            return subprocess.CompletedProcess(command, 0, "", "")

        result, _command = self.run_frozen(fake_powershell)
        self.assertTrue(result["ok"], result)
        self.assertFalse(stale.exists(), "改名前留下的图标要收掉，桌面上别留两个")

    def test_the_status_line_matches_reality(self):
        with mock.patch.object(startup, "desktop_dir", return_value=self.desktop):
            state = startup.describe_shortcut()
            self.assertTrue(state["supported"])
            self.assertFalse(state["exists"])
            self.link.write_bytes(b"lnk")
            self.assertTrue(startup.describe_shortcut()["exists"])


if __name__ == "__main__":
    unittest.main()
