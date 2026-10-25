"""P71：打包后用户配置必须压过程序目录里的模板。

这一层要保证的事：
    程序目录里那份 .env 是打包时放的模板，只是默认值；
    用户数据目录里的 .env 才是用户真正改过的那份，它说了算；
    系统里本来就有的环境变量优先级最高，两份 .env 都盖不掉；
    默认调用（开发模式、模板自身）仍旧不覆盖已有变量；
    配好的模型名真的被选中，不会被「目录里第一个」顶掉。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from avatar.live2d import find_models, pick_model
from security import config as config_module
from security.config import Config, load_dotenv


def write_env(path, body):
    Path(path).write_text(body, encoding="utf-8")
    return Path(path)


class LoadOrderTests(unittest.TestCase):
    """模板先读，用户那份后读，用户那份能顶掉模板。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: os.environ.pop("DESKPET_TEST_MARK", None))
        os.environ.pop("DESKPET_TEST_MARK", None)

    def test_by_default_an_existing_value_is_never_touched(self):
        path = write_env(Path(self.tmp.name) / "a.env", "DESKPET_TEST_MARK=文件\n")
        with mock.patch.dict(os.environ, {"DESKPET_TEST_MARK": "环境"}):
            load_dotenv(path)
            self.assertEqual(os.environ["DESKPET_TEST_MARK"], "环境")

    def test_the_user_file_overrides_the_template(self):
        path = write_env(Path(self.tmp.name) / "b.env", "DESKPET_TEST_MARK=用户填的\n")
        with mock.patch.dict(os.environ, {"DESKPET_TEST_MARK": ""}):
            load_dotenv(path, override=True)
            self.assertEqual(os.environ["DESKPET_TEST_MARK"], "用户填的")

    def test_a_real_environment_variable_still_wins(self):
        path = write_env(Path(self.tmp.name) / "c.env", "DESKPET_TEST_MARK=文件里的\n")
        with mock.patch.dict(os.environ, {"DESKPET_TEST_MARK": "系统里的"}):
            load_dotenv(path, override=True, keep=set(os.environ))
            self.assertEqual(os.environ["DESKPET_TEST_MARK"], "系统里的")

    def test_an_empty_value_in_the_user_file_is_taken_as_is(self):
        path = write_env(Path(self.tmp.name) / "d.env", "DESKPET_TEST_MARK=\n")
        with mock.patch.dict(os.environ, {"DESKPET_TEST_MARK": "模板默认值"}):
            load_dotenv(path, override=True)
            self.assertEqual(os.environ["DESKPET_TEST_MARK"], "")

    def test_a_comment_and_a_bad_line_are_skipped(self):
        path = write_env(
            Path(self.tmp.name) / "e.env",
            "# 注释\n\nDESKPET_TEST_MARK=有用\n这一行不是键值对\n",
        )
        load_dotenv(path, override=True)
        self.assertEqual(os.environ["DESKPET_TEST_MARK"], "有用")

    def test_a_missing_file_is_not_an_error(self):
        self.assertFalse(load_dotenv(Path(self.tmp.name) / "没有这个文件.env"))


class PackagedConfigTests(unittest.TestCase):
    """打包后：先读程序目录模板，再读用户数据目录，用户那份说了算。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: os.environ.pop("DESKPET_LIVE2D_MODEL", None))

    def test_the_user_file_beats_the_shipped_template(self):
        user_root = Path(self.tmp.name) / "userdata"
        user_root.mkdir()
        template = write_env(
            Path(self.tmp.name) / "template.env", "DESKPET_LIVE2D_MODEL=\n"
        )
        mine = write_env(user_root / ".env", "DESKPET_LIVE2D_MODEL=live2D0\n")
        os.environ.pop("DESKPET_LIVE2D_MODEL", None)
        load_dotenv(template)
        load_dotenv(mine, override=True)
        self.assertEqual(os.environ["DESKPET_LIVE2D_MODEL"], "live2D0")

    def test_the_user_file_can_change_a_non_empty_template_default(self):
        template = write_env(Path(self.tmp.name) / "t2.env", "DESKPET_TRAY=1\n")
        mine = write_env(Path(self.tmp.name) / "u2.env", "DESKPET_TRAY=0\n")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DESKPET_TRAY", None)
            load_dotenv(template)
            self.assertEqual(os.environ.get("DESKPET_TRAY"), "1")
            load_dotenv(mine, override=True)
            self.assertEqual(os.environ.get("DESKPET_TRAY"), "0")
        os.environ.pop("DESKPET_TRAY", None)

    def test_config_gives_the_user_file_the_last_word(self):
        with mock.patch.dict(os.environ, {"DESKPET_ROOT": self.tmp.name}), mock.patch.object(
            sys, "frozen", True, create=True
        ), mock.patch.object(config_module, "load_dotenv", return_value=False) as loader:
            Config()
        self.assertTrue(
            any(call.kwargs.get("override") for call in loader.call_args_list)
        )

    def test_the_configured_model_wins_over_the_first_one_in_the_folder(self):
        root = Path(self.tmp.name) / "live2d"
        for name in ("live2D", "live2D0"):
            folder = root / name
            (folder / (name + ".1024")).mkdir(parents=True)
            (folder / (name + ".1024") / "texture_00.png").write_bytes(b"x")
            (folder / (name + ".model3.json")).write_text(
                '{"Version": 3, "FileReferences": {"Moc": "%s.moc3",'
                ' "Textures": ["%s.1024/texture_00.png"]}}' % (name, name),
                encoding="utf-8",
            )
            (folder / (name + ".moc3")).write_bytes(b"moc")
        models = find_models(str(root))
        self.assertEqual(sorted(m.id for m in models), ["live2D", "live2D0"])
        self.assertEqual(pick_model(models, "").id, "live2D")
        self.assertEqual(pick_model(models, "live2D0").id, "live2D0")


if __name__ == "__main__":
    unittest.main()
