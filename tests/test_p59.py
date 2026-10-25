"""P59 阶段 I：角色包的新建、删除、备份与恢复。

要点：
    新建只写一份清单，不复制也不生成模型文件，引用的模型必须真实存在；
    删除一定要确认，角色包里放着模型源文件时程序拒绝删；
    备份只收清单类小文件，模型、音频、数据库、密钥都不进去；
    恢复只解小文件，脚本、绝对路径、上层目录一律跳过；
    全程不碰 live2d/ 里的任何用户文件。
"""

import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from characters import store  # noqa: E402
from characters.manager import CharacterManager  # noqa: E402

MODEL = {"Version": 3, "FileReferences": {"Moc": "hero.moc3"}}
FILES = ("hero.moc3",)


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.chars = self.root / "characters"
        self.live2d = self.root / "live2d"
        self.chars.mkdir()
        self.write_model()

    def write_model(self, name="hero"):
        base = self.live2d / name
        base.mkdir(parents=True, exist_ok=True)
        for item in FILES:
            (base / item).write_bytes(b"x")
        (base / f"{name}.model3.json").write_text(
            json.dumps(MODEL, ensure_ascii=False), encoding="utf-8"
        )


class CreateTests(TempCase):
    def test_new_package_only_records_the_real_model(self):
        result = store.create_package(
            self.chars, "hero", name="英雄", model="hero.model3.json", live2d_root=self.live2d
        )
        self.assertTrue(result["ok"], result.get("error"))
        manifest = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["id"], "hero")
        self.assertEqual(manifest["avatar"]["model"], "hero.model3.json")
        self.assertEqual(manifest["avatar"]["provider"], "live2d")
        # 只写了清单，没有复制任何模型文件
        names = sorted(item.name for item in (self.chars / "hero").iterdir())
        self.assertEqual(names, ["character.json"])

    def test_unknown_model_is_refused_not_written(self):
        result = store.create_package(
            self.chars, "hero", model="nope.model3.json", live2d_root=self.live2d
        )
        self.assertFalse(result["ok"])
        self.assertIn("找不到模型文件", result["error"])
        self.assertFalse((self.chars / "hero").exists())

    def test_second_package_with_the_same_id_is_refused(self):
        store.create_package(self.chars, "hero", live2d_root=self.live2d)
        again = store.create_package(self.chars, "hero", live2d_root=self.live2d)
        self.assertFalse(again["ok"])
        self.assertIn("已经有一个", again["error"])

    def test_id_cannot_escape_the_characters_folder(self):
        result = store.create_package(self.chars, "../../evil", live2d_root=self.live2d)
        self.assertTrue(result["ok"])
        self.assertEqual(result["id"], "evil")
        self.assertTrue(Path(result["path"]).is_file())
        self.assertEqual(
            Path(result["path"]).parent.resolve(), (self.chars / "evil").resolve()
        )

    def test_empty_or_unusable_id_is_refused(self):
        for raw in ("", "   ", "...", "/"):
            self.assertFalse(store.create_package(self.chars, raw)["ok"], raw)


class DeleteTests(TempCase):
    def setUp(self):
        super().setUp()
        store.create_package(self.chars, "hero", name="英雄", live2d_root=self.live2d)

    def test_delete_needs_a_confirmation_and_reports_the_scope(self):
        result = store.delete_package(self.chars, "hero")
        self.assertFalse(result["ok"])
        self.assertTrue(result["needs_confirm"])
        self.assertIn("character.json", result["files"])
        self.assertTrue((self.chars / "hero" / "character.json").is_file())

    def test_confirmed_delete_removes_only_the_package(self):
        result = store.delete_package(self.chars, "hero", confirm=True)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertFalse((self.chars / "hero").exists())
        self.assertTrue((self.live2d / "hero" / "hero.model3.json").is_file())

    def test_package_holding_model_sources_is_refused(self):
        target = self.chars / "hero"
        (target / "hero.moc3").write_bytes(b"real model")
        result = store.delete_package(self.chars, "hero", confirm=True)
        self.assertFalse(result["ok"])
        self.assertIn("模型源文件", result["error"])
        self.assertTrue((target / "hero.moc3").is_file())

    def test_deleting_something_that_is_not_a_package_is_refused(self):
        self.assertFalse(store.delete_package(self.chars, "nobody")["ok"])
        self.assertFalse(store.delete_package(self.chars, "")["ok"])


class BackupTests(TempCase):
    def setUp(self):
        super().setUp()
        store.create_package(self.chars, "hero", name="英雄", live2d_root=self.live2d)
        (self.chars / "hero" / "notes.md").write_text("说明", encoding="utf-8")
        (self.chars / "hero" / "keys.json").write_text('{"sk":"secret"}', encoding="utf-8")
        (self.chars / "hero" / "run.py").write_text("print(1)", encoding="utf-8")
        (self.chars / "hero" / "art.png").write_bytes(b"png")
        (self.chars / "hero" / "deskpet.sqlite3").write_bytes(b"db")

    def _backup(self):
        target = self.root / "out" / "packs.zip"
        result = store.backup(self.chars, target=target)
        self.assertTrue(result["ok"], result.get("error"))
        return target

    def test_backup_takes_small_text_only(self):
        target = self._backup()
        with zipfile.ZipFile(target) as archive:
            names = sorted(archive.namelist())
        self.assertEqual(names, ["hero/character.json", "hero/keys.json", "hero/notes.md"])
        self.assertNotIn("hero/run.py", names)
        self.assertNotIn("hero/art.png", names)
        self.assertNotIn("hero/deskpet.sqlite3", names)

    def test_backup_with_nothing_to_save_is_refused(self):
        empty = self.root / "empty"
        empty.mkdir()
        self.assertFalse(store.backup(empty)["ok"])
        self.assertFalse(store.backup(self.root / "nowhere")["ok"])

    def test_restore_round_trips_the_manifest(self):
        target = self._backup()
        # 这个包里带着贴图，程序会拒绝自己删（见 DeleteTests），这里由测试自己清掉。
        shutil.rmtree(self.chars / "hero")
        self.assertFalse((self.chars / "hero").exists())
        result = store.restore(self.chars, target)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["count"], 3)
        manifest = json.loads((self.chars / "hero" / "character.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "英雄")

    def test_restore_skips_scripts_and_parent_paths(self):
        fresh = self.root / "fresh"
        fresh.mkdir()
        target = self.root / "evil.zip"
        with zipfile.ZipFile(target, "w") as archive:
            archive.writestr("hero/character.json", '{"id": "hero"}')
            archive.writestr("hero/run.py", "print(1)")
            archive.writestr("../outside.json", "{}")
            archive.writestr("/abs.json", "{}")
            archive.writestr("hero/big.moc3", "x")
        result = store.restore(fresh, target)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["files"], ["hero/character.json"])
        self.assertFalse((self.root / "outside.json").exists())
        self.assertFalse((fresh / "hero" / "run.py").exists())
        self.assertFalse((fresh / "hero" / "big.moc3").exists())

    def test_restore_keeps_existing_files_unless_told_otherwise(self):
        target = self._backup()
        (self.chars / "hero" / "character.json").write_text('{"id": "mine"}', encoding="utf-8")
        result = store.restore(self.chars, target)
        self.assertFalse(result["ok"])
        self.assertTrue(store.restore(self.chars, target, overwrite=True)["ok"])
        manifest = json.loads((self.chars / "hero" / "character.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "英雄")

    def test_bad_zip_is_reported_not_crashed(self):
        bad = self.root / "bad.zip"
        bad.write_bytes(b"not a zip at all")
        self.assertFalse(store.restore(self.chars, bad)["ok"])
        self.assertFalse(store.restore(self.chars, self.root / "missing.zip")["ok"])


class DescribeTests(TempCase):
    def test_counts_come_from_real_files(self):
        self.assertEqual(store.describe(self.chars)["count"], 0)
        store.create_package(self.chars, "hero", live2d_root=self.live2d)
        store.create_package(self.chars, "second", live2d_root=self.live2d)
        data = store.describe(self.chars)
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["files"], 2)
        self.assertGreater(data["bytes"], 0)
        self.assertEqual(store.describe("")["count"], 0)
        self.assertEqual(store.describe(self.root / "nope")["count"], 0)


class ManagerIntegrationTests(TempCase):
    def test_manager_sees_packages_after_create_and_not_after_delete(self):
        manager = CharacterManager(
            characters_root=str(self.chars),
            live2d_root=str(self.live2d),
            selected="hero",
            default_model="hero.model3.json",
        )
        self.assertEqual(manager.ids(), [])
        store.create_package(self.chars, "hero", name="英雄", live2d_root=self.live2d)
        manager.scan()
        self.assertEqual(manager.ids(), ["hero"])
        self.assertFalse(manager.get("hero").blocking())
        result = manager.select("hero")
        self.assertTrue(result["ok"], result.get("error"))
        store.delete_package(self.chars, "hero", confirm=True)
        manager.scan()
        self.assertEqual(manager.ids(), [])

    def test_selected_package_survives_a_failed_scan(self):
        store.create_package(self.chars, "hero", live2d_root=self.live2d)
        manager = CharacterManager(
            characters_root=str(self.chars),
            live2d_root=str(self.live2d),
            selected="hero",
            default_model="hero.model3.json",
        )
        manager.scan()
        (self.chars / "hero" / "character.json").write_text("{broken", encoding="utf-8")
        manager.scan()
        self.assertEqual(manager.selected, "hero")
        self.assertEqual(manager.ids(), [])


if __name__ == "__main__":
    unittest.main()