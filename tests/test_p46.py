"""P46：启动兜底与记忆归属隔离（桌面应用化 + 阶段 F / I）。

这一层要保证的事：
    界面卡在启动、窗口一直不出现时，用户能收到一句人话，而不是对着空桌面；
    已经在别处报过启动错误就不再重复弹窗；
    每只宠物的私有记忆互不可见，公共记忆仍然共享；
    老数据库升级后原有记忆一条不少，仍能被检索到；
    界面不再把「说话输入」说成已经能用。
"""

import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import main as app_main
from database.db import Database
from memory.manager import MemoryManager
from memory.repository import MemoryRepository
from ui import tray as tray_module
from ui.desktop import DesktopPet


def build(tmp, pet_id=None):
    database = Database(Path(tmp) / "deskpet.sqlite3")
    applied = database.migrate()
    return MemoryManager(MemoryRepository(database), pet_id=pet_id), database, applied


# ------------------------------------------------------------ 启动兜底
class StartupWatchdogTests(unittest.TestCase):
    def test_non_windows_counts_as_shown(self):
        with mock.patch.object(app_main.os, "name", "posix"):
            self.assertTrue(app_main.deskpet_window_exists())

    def test_it_stays_quiet_once_the_window_shows_up(self):
        shown = []
        with mock.patch.object(
            app_main, "deskpet_window_exists", return_value=True
        ), mock.patch.object(
            app_main, "notify_startup_stalled", side_effect=lambda: shown.append(1)
        ):
            thread = app_main.start_startup_watchdog(None, seconds=5)
            thread.join(timeout=10)
        self.assertEqual(shown, [])
        self.assertFalse(thread.is_alive())

    def test_a_window_that_never_shows_up_gets_one_plain_sentence(self):
        shown = []
        with mock.patch.object(
            app_main, "deskpet_window_exists", return_value=False
        ), mock.patch.object(app_main, "_fatal_reported", False), mock.patch.object(
            app_main, "notify_startup_stalled", side_effect=lambda: shown.append(1)
        ):
            thread = app_main.start_startup_watchdog(None, seconds=1)
            thread.join(timeout=10)
        self.assertEqual(shown, [1])

    def test_an_already_reported_failure_is_not_reported_twice(self):
        shown = []
        with mock.patch.object(
            app_main, "deskpet_window_exists", return_value=False
        ), mock.patch.object(app_main, "_fatal_reported", True), mock.patch.object(
            app_main, "notify_startup_stalled", side_effect=lambda: shown.append(1)
        ):
            thread = app_main.start_startup_watchdog(None, seconds=1)
            thread.join(timeout=10)
        self.assertEqual(shown, [])

    def test_reporting_a_crash_silences_the_watchdog(self):
        with mock.patch.object(app_main, "_fatal_reported", False), mock.patch.object(
            app_main, "Config", side_effect=RuntimeError("没配置")
        ), mock.patch.object(
            app_main.ctypes, "windll", mock.MagicMock(), create=True
        ), mock.patch.object(
            app_main.os, "name", "nt"
        ):
            app_main.report_fatal(ValueError("随便什么错"))
            self.assertTrue(app_main._fatal_reported)

    def test_the_stalled_message_has_no_traceback(self):
        shown = []
        with mock.patch.object(
            app_main.ctypes, "windll", mock.MagicMock(), create=True
        ) as windll, mock.patch.object(app_main.os, "name", "nt"):
            windll.user32.MessageBoxW.side_effect = lambda h, m, t, f: shown.append(m)
            app_main.notify_startup_stalled()
        self.assertTrue(shown)
        self.assertIn("日志", shown[0])
        self.assertNotIn("Traceback", shown[0])


# ------------------------------------------------------------ 记忆归属
class PetMemoryIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_the_owner_column_arrives_with_a_migration(self):
        _, database, applied = build(self.tmp.name)
        self.assertIn(10, applied)
        columns = {row["name"] for row in database.query("PRAGMA table_info(memories)")}
        self.assertIn("pet_id", columns)

    def test_a_private_memory_stays_with_its_own_pet(self):
        alpha, _, _ = build(self.tmp.name, pet_id="alpha")
        alpha.remember("我的猫叫糯米", kind="long_term", importance=2)
        beta, _, _ = build(self.tmp.name, pet_id="beta")
        self.assertEqual(beta.search("我的猫"), [])
        self.assertEqual(len(alpha.search("我的猫")), 1)

    def test_a_memory_without_an_owner_is_shared(self):
        shared, _, _ = build(self.tmp.name, pet_id=None)
        shared.remember("我的猫叫糯米", kind="long_term", importance=2)
        alpha, _, _ = build(self.tmp.name, pet_id="alpha")
        self.assertEqual(len(alpha.search("我的猫")), 1)

    def test_switching_pet_switches_what_gets_written(self):
        memory, _, _ = build(self.tmp.name, pet_id="alpha")
        memory.remember("alpha 喜欢喝茶", kind="long_term", importance=2)
        memory.use_pet("beta")
        memory.remember("beta 喜欢喝咖啡", kind="long_term", importance=2)
        beta_rows = {row["content"] for row in memory.search("喜欢喝")}
        self.assertEqual(beta_rows, {"beta 喜欢喝咖啡"})
        memory.use_pet("alpha")
        alpha_rows = {row["content"] for row in memory.search("喜欢喝")}
        self.assertEqual(alpha_rows, {"alpha 喜欢喝茶"})

    def test_remembering_the_same_thing_twice_does_not_duplicate_it(self):
        alpha, _, _ = build(self.tmp.name, pet_id="alpha")
        first = alpha.remember("我的猫叫糯米", kind="long_term", importance=2)
        second = alpha.remember("我的猫叫糯米", kind="long_term", importance=2)
        self.assertEqual(first, second)

    def test_two_pets_may_hold_the_same_sentence_separately(self):
        alpha, _, _ = build(self.tmp.name, pet_id="alpha")
        alpha.remember("我的猫叫糯米", kind="long_term", importance=2)
        beta, _, _ = build(self.tmp.name, pet_id="beta")
        beta.remember("我的猫叫糯米", kind="long_term", importance=2)
        alpha_row = alpha.search("我的猫")[0]
        beta_row = beta.search("我的猫")[0]
        self.assertNotEqual(alpha_row["id"], beta_row["id"])
        self.assertEqual(alpha_row["pet_id"], "alpha")
        self.assertEqual(beta_row["pet_id"], "beta")

    def test_an_old_database_keeps_its_memories_readable(self):
        database = Database(Path(self.tmp.name) / "old.sqlite3")
        database.migrate()
        legacy = MemoryManager(MemoryRepository(database))
        legacy.remember("以前记下的事", kind="long_term", importance=2)
        # 老记录没有归属，任何一只宠物都读得到，绝不会被升级吃掉。
        for pet_id in (None, "alpha", "beta"):
            memory = MemoryManager(MemoryRepository(database), pet_id=pet_id)
            self.assertEqual(len(memory.search("以前记下")), 1)

    def test_writing_memories_without_a_pet_still_works(self):
        memory, _, _ = build(self.tmp.name)
        self.assertIsNone(memory.owner())
        self.assertIsNotNone(memory.remember("随便记一句", kind="long_term"))
        self.assertEqual(len(memory.search("随便记")), 1)


# ------------------------------------------------------------ 语音状态口径
class VoiceInputNoteTests(unittest.TestCase):
    def note(self, **kwargs):
        stub = types.SimpleNamespace(
            stt=kwargs.get("stt"),
            voice=kwargs.get("voice"),
            _privacy_allows=lambda key: kwargs.get("allowed", False),
        )
        return DesktopPet._voice_input_note(stub)

    def test_without_an_engine_it_says_you_can_only_type(self):
        note = self.note()
        self.assertIn("打字", note)
        self.assertNotIn("可用", note)

    def test_a_real_engine_is_reported_as_usable(self):
        class Engine:
            def available(self):
                return True

        self.assertEqual(self.note(stt=Engine(), allowed=True), "可以用说话输入")

    def test_a_microphone_without_an_engine_is_explained_honestly(self):
        note = self.note(stt=None, voice=object(), allowed=True)
        self.assertIn("没有识别引擎", note)


# ------------------------------------------------------------ 托盘
class FakeOwner:
    def __init__(self):
        self.calls = []

    def toggle_visible(self):
        self.calls.append("toggle_visible")

    def toggle_chat(self):
        self.calls.append("toggle_chat")

    def open_settings(self):
        self.calls.append("open_settings")

    def close(self):
        self.calls.append("close")


class TrayTests(unittest.TestCase):
    def test_the_shell_brings_its_own_window_class(self):
        shell = tray_module._load_shell()
        if shell is None:
            self.skipTest("当前平台没有系统托盘")
        self.assertIn("WNDCLASSW", shell)
        self.assertEqual(shell["WNDCLASSW"]().lpszClassName, None)

    def test_dispatch_takes_priority_over_the_owner(self):
        owner = FakeOwner()
        seen = []

        def dispatch(name):
            seen.append(name)
            return True

        icon = tray_module.TrayIcon(owner, dispatch=dispatch)
        self.assertTrue(icon.handle_command(1))
        self.assertEqual(seen, ["toggle_visible"])
        self.assertEqual(owner.calls, [])

    def test_without_dispatch_the_owner_is_called_directly(self):
        owner = FakeOwner()
        icon = tray_module.TrayIcon(owner)
        self.assertTrue(icon.handle_command(2))
        self.assertEqual(owner.calls, ["toggle_chat"])

    def test_a_broken_dispatch_is_reported_not_raised(self):
        def dispatch(_name):
            raise RuntimeError("主线程队列满了")

        icon = tray_module.TrayIcon(FakeOwner(), dispatch=dispatch)
        self.assertFalse(icon.handle_command(1))

    def test_unknown_command_is_still_refused(self):
        icon = tray_module.TrayIcon(FakeOwner(), dispatch=lambda name: True)
        self.assertFalse(icon.handle_command(99))


class TrayDispatchTests(unittest.TestCase):
    """托盘在自己的线程里，菜单命令必须切回界面线程再动控件。"""

    def stub(self, captured):
        return types.SimpleNamespace(
            post=lambda fn: captured.append(fn),
            logger=None,
            _tray_handlers=DesktopPet._tray_handlers,
            _tray_call=lambda name: bool(captured.append(name)) or True,
        )

    def test_an_unknown_command_is_refused(self):
        captured = []
        self.assertFalse(DesktopPet._tray_dispatch(self.stub(captured), "nope"))
        self.assertEqual(captured, [])

    def test_a_menu_command_is_handed_to_the_ui_thread(self):
        captured = []
        stub = self.stub(captured)
        self.assertTrue(DesktopPet._tray_dispatch(stub, "toggle_visible"))
        self.assertEqual(len(captured), 1)
        captured[0]()
        self.assertIn("toggle_visible", captured)

    def test_the_hotkey_is_dispatched_the_same_way(self):
        captured = []
        self.assertTrue(
            DesktopPet._tray_dispatch(self.stub(captured), "toggle_chat")
        )
        self.assertEqual(len(captured), 1)


class PetMemoryManagementTests(unittest.TestCase):
    """记忆管理界面这条路同样守规矩：拿别只宠物的 id，读不到也改不动。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.alpha, _, _ = build(self.tmp.name, pet_id="alpha")
        self.beta, _, _ = build(self.tmp.name, pet_id="beta")
        self.alpha_id = self.alpha.remember("alpha 的私事", kind="long_term", importance=2)
        self.beta_id = self.beta.remember("beta 的私事", kind="long_term", importance=2)
        shared, _, _ = build(self.tmp.name, pet_id=None)
        self.shared_id = shared.remember("大家都能看的事", kind="long_term", importance=2)

    def content_of(self, memory_id, pet_id):
        row = MemoryRepository(
            self.alpha.repository.db
        ).get_memory(memory_id, pet_id=pet_id)
        return row["content"] if row else None

    def test_the_list_only_holds_your_own_and_the_shared_ones(self):
        seen = {row["content"] for row in self.alpha.browse()}
        self.assertEqual(seen, {"alpha 的私事", "大家都能看的事"})

    def test_taking_someone_elses_id_reads_nothing(self):
        self.assertIsNone(self.alpha.get(self.beta_id))
        self.assertIsNotNone(self.alpha.get(self.alpha_id))
        self.assertIsNotNone(self.alpha.get(self.shared_id))

    def test_editing_another_pets_memory_is_refused(self):
        self.assertFalse(self.beta.edit(self.alpha_id, content="被改掉了"))
        self.assertEqual(self.content_of(self.alpha_id, "alpha"), "alpha 的私事")
        self.assertTrue(self.alpha.edit(self.alpha_id, content="我自己改的"))
        self.assertEqual(self.content_of(self.alpha_id, "alpha"), "我自己改的")

    def test_locking_and_confirming_another_pets_memory_do_nothing(self):
        self.assertFalse(self.beta.set_locked(self.alpha_id, True))
        self.assertFalse(self.beta.confirm_memory(self.alpha_id, True))
        row = MemoryRepository(self.alpha.repository.db).get_memory(self.alpha_id)
        self.assertEqual(int(row["locked"]), 0)
        self.assertEqual(int(row["confirmed"]), 1)
        self.assertTrue(self.alpha.set_locked(self.alpha_id, True))
        row = MemoryRepository(self.alpha.repository.db).get_memory(self.alpha_id)
        self.assertEqual(int(row["locked"]), 1)

    def test_archiving_another_pets_memory_does_nothing(self):
        self.beta.archive_memory(self.alpha_id, True)
        row = MemoryRepository(self.alpha.repository.db).get_memory(self.alpha_id)
        self.assertEqual(int(row["archived"]), 0)
        self.assertTrue(self.alpha.archive_memory(self.alpha_id, True))
        row = MemoryRepository(self.alpha.repository.db).get_memory(self.alpha_id)
        self.assertEqual(int(row["archived"]), 1)

    def test_forgetting_another_pets_memory_is_refused(self):
        self.assertFalse(self.beta.forget(self.alpha_id, confirm=True))
        self.assertIsNotNone(self.content_of(self.alpha_id, "alpha"))
        self.assertTrue(self.alpha.forget(self.alpha_id, confirm=True))
        self.assertIsNone(self.content_of(self.alpha_id, "alpha"))

    def test_the_shared_memory_is_still_manageable_by_anyone(self):
        self.assertTrue(self.alpha.set_locked(self.shared_id, True))
        row = MemoryRepository(self.alpha.repository.db).get_memory(self.shared_id)
        self.assertEqual(int(row["locked"]), 1)

    def test_bulk_deletes_stay_inside_your_own_rows(self):
        repository = MemoryRepository(self.alpha.repository.db)
        repository.delete_kind("long_term", pet_id="alpha")
        self.assertEqual(repository.count_memories("long_term"), 2)
        repository.delete_all_memories(pet_id="alpha")
        self.assertEqual(repository.count_memories("long_term"), 2)


if __name__ == "__main__":
    unittest.main()
