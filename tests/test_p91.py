"""V9.1 第五、六阶段验收：界面语义读取与记忆全文索引。

第五阶段（界面语义）：读的是应用自己公开的可访问性信息，不是像素。三条底线要守住——
密码框和密钥这类控件一条都不许进结果、前台窗口先按系统句柄定位（模拟器一类应用不吃
「找活动窗口」那种写法）、读不到就如实说读不到。

第六阶段（记忆全文检索）：索引用 SQLite FTS5，是派生数据。建得出就用，建不出退回原来的
关键词打分；记忆本体删掉，索引里那条也必须跟着没。
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters import local_runtimes  # noqa: E402
from behavior import awareness  # noqa: E402
from database.db import Database  # noqa: E402
from memory import fts as fts_mod  # noqa: E402
from memory.manager import MemoryManager  # noqa: E402
from memory.repository import MemoryRepository  # noqa: E402

HAS_FTS5 = local_runtimes.fts5_available()
HAS_TRIGRAM = "trigram" in fts_mod.TOKENIZERS and HAS_FTS5


# ---------- 第五阶段：界面语义读取 ----------


class FakeElement:
    """假的 pywinauto 控件。只提供读取要用的那几个属性。"""

    def __init__(self, text="", control_type="Text", password=False, password_style=None):
        self._text = text
        info = SimpleNamespace(control_type=control_type)
        if password_style == "callable":
            info.is_password = lambda: True
        elif password_style == "attribute":
            info.is_password = True
        elif password:
            self.IsPassword = True
        self.element_info = info

    def window_text(self):
        return self._text


class FakeWindow:
    def __init__(self, title, kids):
        self._title = title
        self._kids = list(kids)

    def window_text(self):
        return self._title

    def descendants(self):
        return list(self._kids)


class FakeDesktop:
    """假的 pywinauto Desktop。记下调用方走的是句柄还是活动窗口。"""

    def __init__(self, by_handle=None, active=None):
        self.by_handle = by_handle
        self.active = active
        self.calls = []

    def window(self, handle=None, active_only=False):
        self.calls.append(("handle" if handle else "active", handle))
        target = self.by_handle if handle else self.active
        if isinstance(target, Exception):
            raise target
        if target is None:
            raise LookupError("没有这个窗口")
        return target


class FakeDesktopFactory:
    def __init__(self, desktop):
        self.desktop = desktop

    def __call__(self, backend="uia"):
        return self.desktop


class StubPreferences:
    """替身偏好存储：分级得真的存下来才算数。"""

    def __init__(self):
        self.data = {}

    def get_preference(self, key):
        return self.data.get(key)

    def set_preference(self, key, value):
        self.data[key] = value
        return True


class StubActivity(awareness.ActivityProvider):
    """替身：假装读得到一个前台应用，好让分级门禁被测到。"""

    name = "stub"

    def is_available(self):
        return True

    def current(self):
        return {"app": "code.exe", "title": "main.py", "pid": 4321}

    def describe(self):
        return {"provider": self.name, "available": True, "note": "测试替身"}


def provider_with(desktop, handle=1234, module="pywinauto"):
    provider = awareness.UiAutomationProvider.__new__(awareness.UiAutomationProvider)
    provider._engine = FakeDesktopFactory(desktop)
    provider._module = module
    provider._last_error = ""
    provider._foreground_handle = staticmethod(lambda: handle)
    return provider


class PrivateControlTests(unittest.TestCase):
    """密码框和密钥这类控件连名字都不许带出来。"""

    def test_password_roles_are_dropped(self):
        self.assertFalse(awareness.UiAutomationProvider.keep("PasswordEdit", "登录"))

    def test_password_flag_alone_is_enough(self):
        self.assertFalse(awareness.UiAutomationProvider.keep("Edit", "随便", True))

    def test_private_words_in_the_name_are_dropped(self):
        for name in ("密码", "API Key", "access token", "密钥", "银行卡号"):
            self.assertFalse(
                awareness.UiAutomationProvider.keep("Edit", name, False), name
            )

    def test_ordinary_controls_are_kept(self):
        self.assertTrue(awareness.UiAutomationProvider.keep("Button", "保存"))
        self.assertTrue(awareness.UiAutomationProvider.keep("Document", "报告.docx"))

    def test_password_flag_is_read_from_several_shapes(self):
        keep = awareness.UiAutomationProvider._password_flag
        self.assertTrue(keep(FakeElement(password=True)))
        self.assertTrue(keep(FakeElement(password_style="attribute")))
        self.assertTrue(keep(FakeElement(password_style="callable")))
        self.assertFalse(keep(FakeElement(text="普通")))
        self.assertFalse(keep(SimpleNamespace()))


class SemanticReadTests(unittest.TestCase):
    """读取路径：先按句柄，再退活动窗口；结果里不出现控件内容。"""

    def test_handle_is_tried_first(self):
        window = FakeWindow("编辑器", [FakeElement("main.py", "Edit")])
        desktop = FakeDesktop(by_handle=window)
        provider = provider_with(desktop)
        data = provider.read(limit=4)
        self.assertEqual(data["page"], "编辑器")
        self.assertEqual([item["name"] for item in data["controls"]], ["main.py"])
        self.assertEqual(desktop.calls[0][0], "handle")

    def test_it_falls_back_to_the_active_window(self):
        window = FakeWindow("浏览器", [FakeElement("标签页", "TabItem")])
        desktop = FakeDesktop(by_handle=LookupError("句柄读不到"), active=window)
        provider = provider_with(desktop)
        data = provider.read(limit=4)
        self.assertEqual(data["page"], "浏览器")
        self.assertEqual([item["name"] for item in data["controls"]], ["标签页"])
        self.assertEqual([call[0] for call in desktop.calls], ["handle", "active"])

    def test_nothing_readable_gives_an_empty_read_not_a_crash(self):
        desktop = FakeDesktop(
            by_handle=LookupError("句柄读不到"), active=LookupError("活动窗口也读不到")
        )
        provider = provider_with(desktop)
        self.assertEqual(provider.read(limit=4), {})
        self.assertTrue(provider.describe()["available"])
        self.assertIn("读失败", provider.describe()["note"])

    def test_only_role_and_name_come_out(self):
        window = FakeWindow("记事本", [FakeElement("正文", "Document"), FakeElement("", "Text")])
        provider = provider_with(FakeDesktop(by_handle=window))
        data = provider.read(limit=8)
        for item in data["controls"]:
            self.assertEqual(set(item.keys()), {"role", "name"})
            self.assertIsInstance(item["name"], str)

    def test_password_controls_never_reach_the_result(self):
        window = FakeWindow(
            "登录",
            [
                FakeElement("账号", "Edit"),
                FakeElement("密码", "Edit"),
                FakeElement("输入", "Edit", password_style="attribute"),
                FakeElement("登录按钮", "Button"),
            ],
        )
        provider = provider_with(FakeDesktop(by_handle=window))
        names = [item["name"] for item in provider.read(limit=8)["controls"]]
        self.assertEqual(names, ["账号", "登录按钮"])

    def test_limit_is_respected(self):
        kids = [FakeElement("项目%d" % index, "ListItem") for index in range(10)]
        provider = provider_with(FakeDesktop(by_handle=FakeWindow("列表", kids)))
        self.assertEqual(len(provider.read(limit=3)["controls"]), 3)

    def test_without_a_binding_it_says_so(self):
        provider = awareness.UiAutomationProvider.__new__(awareness.UiAutomationProvider)
        provider._engine = None
        provider._module = ""
        provider._last_error = ""
        self.assertFalse(provider.is_available())
        self.assertEqual(provider.read(limit=4), {})
        self.assertIn("读不了界面语义", provider.describe()["note"])

    def test_the_real_reader_refuses_to_read_while_the_service_is_off(self):
        from behavior.awareness import AwarenessService

        class Counting(awareness.SemanticProvider):
            name = "counting"

            def __init__(self):
                self.calls = 0

            def is_available(self):
                return True

            def read(self, limit=8):
                self.calls += 1
                return {"page": "不该被读", "controls": []}

        semantic = Counting()
        service = AwarenessService(
            preferences=StubPreferences(),
            activity=StubActivity(),
            semantic=semantic,
            privacy=None,
        )
        self.assertFalse(service.observe()["ok"])
        self.assertEqual(semantic.calls, 0)
        # 第一层（只看应用名）也不该去读界面语义。
        service.set_level("metadata")
        service.observe()
        self.assertEqual(semantic.calls, 0)
        # 升到第二层才会真的读一次。
        service.set_level("semantic")
        service.observe()
        self.assertEqual(semantic.calls, 1)


class SemanticRuntimeEntryTests(unittest.TestCase):
    """能力页那两条要跟实际接线一致。"""

    def entries(self):
        return {item["id"]: item for item in local_runtimes.detect()["entries"]}

    def test_windows_uia_entry_tells_the_truth_about_the_binding(self):
        entry = self.entries()["windows-uia"]
        binding = local_runtimes._module_available("pywinauto") or local_runtimes._module_available(
            "uiautomation"
        )
        self.assertTrue(entry["installed"])
        self.assertEqual(entry["ready"], bool(binding))
        if binding:
            self.assertIn("能读", entry["detail"])
        else:
            self.assertIn("装 pywinauto", entry["detail"])

    def test_pywinauto_entry_points_at_the_second_layer(self):
        entry = self.entries()["pywinauto"]
        if entry["installed"]:
            self.assertIn("第二层", entry["detail"])


# ---------- 第六阶段：记忆全文索引 ----------


class MatchQueryTests(unittest.TestCase):
    """拼查询这一步：短词丢掉、引号转义、条数有上限。"""

    def test_terms_shorter_than_three_are_dropped(self):
        self.assertEqual(fts_mod.match_query(["ab", "晚上", "cde"]), '"cde"')

    def test_no_usable_term_gives_an_empty_query(self):
        self.assertEqual(fts_mod.match_query(["ab", "好"]), "")
        self.assertEqual(fts_mod.match_query([]), "")
   
    def test_quotes_are_escaped(self):
        query = fts_mod.match_query(['a"b'])
        self.assertEqual(query, '"a""b"')

    def test_the_number_of_terms_is_capped(self):
        query = fts_mod.match_query(["term%02d" % index for index in range(40)])
        self.assertEqual(query.count(" OR "), fts_mod.MAX_TERMS - 1)


class IndexBuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "t.sqlite3"
        self.database = Database(self.path)
        self.database.migrate()
        self.repo = MemoryRepository(self.database)
        self.memory = MemoryManager(self.repo)

    def tables(self):
        rows = self.database.query(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
        return [row["name"] for row in rows]

    @unittest.skipUnless(HAS_FTS5, "这台机器的 SQLite 没有 FTS5")
    def test_the_index_is_built_with_a_known_tokenizer(self):
        state = self.memory.fts.describe()
        self.assertTrue(state["ready"])
        self.assertIn(state["tokenizer"], fts_mod.TOKENIZERS)
        self.assertIn(fts_mod.TABLE, self.tables())

    @unittest.skipUnless(HAS_FTS5, "这台机器的 SQLite 没有 FTS5")
    def test_building_it_twice_is_harmless(self):
        first = self.memory.fts.describe()
        second = self.memory.fts.ensure(force=True)
        self.assertTrue(second)
        self.assertTrue(first["ready"])
        self.assertIn(self.tables().count(fts_mod.TABLE), (1,))

    @unittest.skipUnless(HAS_FTS5, "这台机器的 SQLite 没有 FTS5")
    def test_it_stamps_a_migration_row(self):
        self.memory.fts.ensure()
        rows = self.database.query(
            "SELECT name FROM schema_migrations WHERE version = 15"
        )
        self.assertEqual([row["name"] for row in rows], ["memory_fts"])

    @unittest.skipUnless(HAS_FTS5, "这台机器的 SQLite 没有 FTS5")
    def test_without_a_database_it_stays_off(self):
        index = fts_mod.MemoryFts(None)
        self.assertFalse(index.ensure())
        self.assertIsNone(index.search_ids(["记忆"]))
        self.assertIn("没有数据库", index.describe()["reason"])


class IndexContentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "t.sqlite3"
        self.database = Database(self.path)
        self.database.migrate()
        self.repo = MemoryRepository(self.database)
        self.memory = MemoryManager(self.repo)
        self.coffee = self.memory.remember("用户喜欢喝美式咖啡，不加糖")
        self.project = self.memory.remember("用户在做一个 YOLO 目标检测项目", kind="project")
        self.sleep = self.memory.remember("用户习惯晚上十一点睡觉")

    def contents(self, query, limit=5):
        return [row["content"] for row in self.memory.search(query, limit=limit)]

    @unittest.skipUnless(HAS_FTS5, "这台机器的 SQLite 没有 FTS5")
    def test_new_memories_land_in_the_index(self):
        self.assertTrue(self.memory.fts.ensure())
        self.assertEqual(self.memory.fts.count(), 3)

    @unittest.skipUnless(HAS_TRIGRAM, "没有 trigram 分词，中文子串查不了")
    def test_chinese_is_searchable_by_substring(self):
        self.assertEqual(self.contents("美式咖啡"), ["用户喜欢喝美式咖啡，不加糖"])
        self.assertEqual(self.contents("目标检测"), ["用户在做一个 YOLO 目标检测项目"])

    @unittest.skipUnless(HAS_FTS5, "这台机器的 SQLite 没有 FTS5")
    def test_deleting_a_memory_takes_it_out_of_the_index_too(self):
        self.assertTrue(self.memory.fts.ensure())
        self.repo.delete_memory(self.coffee)
        self.assertEqual(self.memory.fts.count(), 2)
        self.assertEqual(self.contents("美式咖啡"), [])

    @unittest.skipUnless(HAS_FTS5, "这台机器的 SQLite 没有 FTS5")
    def test_editing_a_memory_reindexes_it(self):
        self.assertTrue(self.memory.fts.ensure())
        self.repo.update_memory(self.coffee, content="用户改喝手冲咖啡了")
        self.assertEqual(self.contents("手冲咖啡"), ["用户改喝手冲咖啡了"])
        self.assertEqual(self.memory.fts.count(), 3)

    @unittest.skipUnless(HAS_FTS5, "这台机器的 SQLite 没有 FTS5")
    def test_superseded_memories_stay_out_of_the_results(self):
        self.repo.supersede([self.sleep], self.coffee)
        self.assertEqual(self.contents("晚上十一点"), [])

    @unittest.skipUnless(HAS_FTS5, "这台机器的 SQLite 没有 FTS5")
    def test_archived_memories_stay_out_of_the_results(self):
        self.repo.set_archived(self.sleep, True)
        self.assertEqual(self.contents("晚上十一点"), [])

    @unittest.skipUnless(HAS_FTS5, "这台机器的 SQLite 没有 FTS5")
    def test_only_this_pet_sees_its_own_and_public_memories(self):
        mine = MemoryManager(self.repo, pet_id="bella")
        other = MemoryManager(self.repo, pet_id="cheng")
        other.remember("程程的私人记忆：爱喝乌龙")
        self.assertEqual(mine.search("乌龙", limit=5), [])
        self.assertEqual(len(other.search("乌龙", limit=5)), 1)

    @unittest.skipUnless(HAS_FTS5, "这台机器的 SQLite 没有 FTS5")
    def test_it_still_works_when_the_index_is_unusable(self):
        with mock.patch.object(self.memory.fts, "search_ids", return_value=None):
            found = self.contents("美式咖啡")
        self.assertIn("用户喜欢喝美式咖啡，不加糖", found)

    def test_repository_lookup_keeps_the_given_order(self):
        rows = self.repo.memories_by_ids([self.sleep, self.coffee])
        self.assertEqual([row["id"] for row in rows], [self.sleep, self.coffee])


class IndexStateTests(unittest.TestCase):
    """能力页那条只读状态：不建索引、也不改用户的库。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "deskpet.sqlite3"

    def test_a_missing_database_is_reported_not_invented(self):
        state = local_runtimes.memory_index_state(str(self.path))
        self.assertFalse(state["exists"])
        self.assertEqual(state["indexed"], 0)

    def test_a_missing_index_is_reported_and_not_created(self):
        database = Database(self.path)
        database.migrate()
        state = local_runtimes.memory_index_state(str(self.path))
        self.assertFalse(state["exists"])
        self.assertIn("还没建", state["reason"])
        names = [
            row["name"]
            for row in database.query(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        ]
        self.assertNotIn(fts_mod.TABLE, names)

    @unittest.skipUnless(HAS_FTS5, "这台机器的 SQLite 没有 FTS5")
    def test_a_built_index_is_reported_with_its_size(self):
        database = Database(self.path)
        database.migrate()
        memory = MemoryManager(MemoryRepository(database))
        memory.remember("用户喜欢喝美式咖啡")
        memory.fts.ensure()
        state = local_runtimes.memory_index_state(str(self.path))
        self.assertTrue(state["exists"])
        self.assertEqual(state["indexed"], 1)
        self.assertIn(state["tokenizer"], fts_mod.TOKENIZERS)

    def test_the_directory_form_also_works(self):
        database = Database(self.path)
        database.migrate()
        state = local_runtimes.memory_index_state(self.tmp.name)
        self.assertIn("exists", state)

    def test_the_entry_reports_the_index_state(self):
        database = Database(self.path)
        database.migrate()
        if HAS_FTS5:
            MemoryManager(MemoryRepository(database)).fts.ensure()
        entry = [
            item
            for item in local_runtimes.detect(self.tmp.name)["entries"]
            if item["id"] == "sqlite-fts5"
        ][0]
        self.assertEqual(entry["ready"], HAS_FTS5)
        if HAS_FTS5:
            self.assertIn("已建", entry["detail"])
        else:
            self.assertIn("没有 FTS5", entry["detail"])


if __name__ == "__main__":
    unittest.main()

