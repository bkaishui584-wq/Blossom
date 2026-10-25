"""V9.1 第三阶段验收：本地知识库（文件索引与检索）。

只读用户明确加进来的目录，不自动全盘导入，不联网。
检索走 SQLite FTS5（trigram 分词），索引用不了就退回 LIKE。
索引是派生数据：删掉文档记录，索引里那条也要跟着没。
本轮测试全部用临时目录和临时数据库，不碰真实用户数据。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database.db import Database  # noqa: E402
from knowledge import index as index_mod  # noqa: E402
from knowledge.service import KnowledgeError, KnowledgeService  # noqa: E402
from skills.manager import SkillManager  # noqa: E402
from tools.knowledge import register_knowledge_tools  # noqa: E402
from tools.permission import PermissionManager  # noqa: E402
from tools.registry import ToolExecutor, ToolRegistry  # noqa: E402


class TempEnv(unittest.TestCase):
    """临时工作区：一个数据库 + 一个可授权的源目录。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.db = Database(str(self.base / "t.sqlite3"))
        self.db.migrate()
        self.src = self.base / "docs"
        self.src.mkdir()

    def service(self, roots=None):
        return KnowledgeService(
            database=self.db, authorized=lambda: list(roots if roots is not None else [str(self.src)])
        )

    def write(self, name, text):
        target = self.src / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target


# ---------- 切段 ----------


class ChunkTests(unittest.TestCase):
    def test_short_paragraphs_merge_into_one_chunk(self):
        chunks = index_mod.split_chunks("第一段。\n\n第二段。\n\n第三段。")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0][0], "第 1-3 段")

    def test_each_paragraph_gets_a_locating_label(self):
        body = "\n\n".join(["这是第 %d 段的内容。" % i for i in range(1, 200)])
        chunks = index_mod.split_chunks(body, size=200)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(mark.startswith("第 ") for mark, _ in chunks))

    def test_long_paragraph_is_split_with_overlap_and_section_marks(self):
        body = "甲" * 3000
        chunks = index_mod.split_chunks(body, size=500)
        self.assertGreater(len(chunks), 1)
        self.assertIn("节", chunks[0][0])
        # 相邻两片要有重叠，不能把句子断成接不上。
        self.assertGreater(index_mod.LONG_PARA_OVERLAP, 0)
        self.assertIn(chunks[0][1][-index_mod.LONG_PARA_OVERLAP:], chunks[1][1])

    def test_empty_text_has_no_chunks(self):
        self.assertEqual(index_mod.split_chunks("   \n\n  \n"), [])


class QueryTermTests(unittest.TestCase):
    def test_terms_split_on_punctuation_and_keep_case_insensitive(self):
        self.assertIn("live2d", index_mod.query_terms("Live2D，口型 同步"))
        self.assertIn("口型", index_mod.query_terms("Live2D，口型 同步"))

    def test_match_query_drops_terms_too_short_for_trigram(self):
        self.assertEqual(index_mod.match_query(["a"]), "")
        # 两个字进不了 trigram 查询，交给 LIKE 兜底。
        self.assertEqual(index_mod.match_query(["记忆"]), "")
        self.assertIn("长期记忆", index_mod.match_query(["长期记忆"]))

    def test_terms_are_capped(self):
        text = " ".join("word%02d" % i for i in range(50))
        self.assertLessEqual(len(index_mod.query_terms(text)), index_mod.MAX_TERMS)


# ---------- 索引 ----------


class IndexTests(TempEnv):
    def test_index_table_is_created_and_reports_its_state(self):
        idx = index_mod.DocumentIndex(self.db)
        state = idx.describe()
        self.assertTrue(state["ready"], state["reason"])
        self.assertEqual(state["table"], index_mod.TABLE)
        self.assertIn(state["tokenizer"], index_mod.TOKENIZERS)

    def test_store_and_counts(self):
        idx = index_mod.DocumentIndex(self.db)
        idx.ensure()
        doc_id = idx.store(str(self.src / "a.md"), name="a.md", chunks=[("第 1 段", "内容")])
        self.assertTrue(doc_id)
        self.assertEqual(idx.count_documents(), 1)
        self.assertEqual(idx.count_chunks(), 1)
        self.assertEqual(idx.count_indexed(), 1)

    def test_storing_the_same_path_replaces_the_old_record(self):
        idx = index_mod.DocumentIndex(self.db)
        idx.ensure()
        path_value = str(self.src / "a.md")
        idx.store(path_value, name="a.md", chunks=[("第 1 段", "旧")])
        idx.store(path_value, name="a.md", chunks=[("第 1 段", "新")])
        self.assertEqual(idx.count_documents(), 1)
        self.assertEqual(idx.count_chunks(), 1)

    def test_deleting_a_document_removes_its_index_rows(self):
        idx = index_mod.DocumentIndex(self.db)
        idx.ensure()
        doc_id = idx.store(str(self.src / "a.md"), name="a.md", chunks=[("第 1 段", "内容")])
        self.assertEqual(idx.count_indexed(), 1)
        idx.remove_id(doc_id)
        self.assertEqual(idx.count_indexed(), 0)
        self.assertEqual(idx.count_chunks(), 0)


class SearchTests(TempEnv):
    def setUp(self):
        super().setUp()
        self.service = self.service()
        self.service.add_root(str(self.src))
        self.write("note.md", "# 项目笔记\n\n桌宠的长期记忆用 SQLite 存事实。\n")
        self.write("sub/tips.txt", "Live2D 口型要跟音频能量走。\n")
        self.service.scan_root(str(self.src))

    def test_chinese_keyword_hits_the_right_document(self):
        hits = self.service.search("长期记忆", limit=5)
        self.assertTrue(hits, "中文关键词应该命中")
        self.assertTrue(any(hit["name"] == "note.md" for hit in hits))

    def test_ascii_keyword_hits(self):
        hits = self.service.search("Live2D", limit=5)
        self.assertTrue(hits)
        self.assertEqual(hits[0]["name"], "tips.txt")

    def test_result_carries_path_and_locator(self):
        hit = self.service.search("长期记忆", limit=1)[0]
        self.assertTrue(os.path.isfile(hit["path"]))
        self.assertTrue(hit["locator"])
        self.assertIn("note.md", hit["relative"])

    def test_missing_keyword_returns_nothing_and_no_lie(self):
        self.assertEqual(self.service.search("完全不存在的词条 zzz", limit=5), [])

    def test_short_term_falls_back_to_like(self):
        # 单字太短，trigram 命中不了；LIKE 兜底要能找出来。
        hits = self.service.search("桌", limit=5)
        self.assertTrue(hits)

    def test_when_the_fts_layer_is_down_like_still_works(self):
        idx = self.service.index
        idx.ensure()
        idx._ready = False
        idx.error = "测试里故意关掉"
        hits = self.service.search("长期记忆", limit=5)
        self.assertTrue(hits, "索引不可用时要退回 LIKE，而不是什么都搜不到")

    def test_empty_query_returns_nothing(self):
        self.assertEqual(self.service.search("   "), [])


# ---------- 目录与授权 ----------


class SourceTests(TempEnv):
    def test_unauthorized_directory_is_refused(self):
        other = self.base / "elsewhere"
        other.mkdir()
        service = self.service()
        with self.assertRaises(KnowledgeError):
            service.add_root(str(other))

    def test_scan_refuses_a_directory_that_was_never_added(self):
        service = self.service(roots=[])
        with self.assertRaises(KnowledgeError):
            service.scan_root(str(self.src))

    def test_adding_a_subdirectory_is_allowed_by_the_parent_root(self):
        (self.src / "deep").mkdir()
        service = self.service()
        self.assertTrue(service.is_authorized(str(self.src / "deep")))

    def test_scan_skips_unsupported_and_hidden_directories(self):
        self.write("a.md", "# 标题\n\n正文内容。\n")
        (self.src / "raw.bin").write_bytes(b"\x00\x01\x02")
        (self.src / "__pycache__").mkdir()
        (self.src / "__pycache__" / "x.py").write_text("secret = 1\n", encoding="utf-8")
        service = self.service()
        service.add_root(str(self.src))
        report = service.scan_root(str(self.src))
        self.assertEqual(report["found"], 1)
        self.assertEqual(report["indexed"], 1)

    def test_removing_a_root_deletes_its_index_records(self):
        self.write("a.md", "# 标题\n\n正文内容。\n")
        service = self.service()
        service.add_root(str(self.src))
        service.scan_root(str(self.src))
        self.assertEqual(service.describe()["documents"], 1)
        result = service.remove_root(str(self.src))
        self.assertEqual(result["documents"], 1)
        state = service.describe()
        self.assertEqual(state["documents"], 0)
        self.assertEqual(state["chunks"], 0)
        self.assertEqual(state["roots"], [])

    def test_source_count_is_recorded_after_a_scan(self):
        self.write("a.md", "# 标题\n\n正文内容。\n")
        self.write("b.txt", "另一份文件。\n")
        service = self.service()
        service.add_root(str(self.src))
        service.scan_root(str(self.src))
        rows = {row["root"]: row for row in service.index.sources()}
        self.assertEqual(rows[str(self.src)]["docs"], 2)

    def test_unreadable_content_is_skipped_not_faked(self):
        (self.src / "empty.md").write_text("   \n\n  ", encoding="utf-8")
        service = self.service()
        service.add_root(str(self.src))
        report = service.scan_root(str(self.src))
        self.assertEqual(report["indexed"], 0)
        self.assertEqual(report["skipped"], 1)


class MigrationTests(TempEnv):
    def test_migration_16_creates_the_knowledge_tables(self):
        tables = set(self.db.health()["tables"])
        self.assertIn("documents", tables)
        self.assertIn("doc_chunks", tables)
        self.assertIn("doc_sources", tables)

    def test_migration_is_idempotent(self):
        self.assertEqual(self.db.migrate(), [])

    def test_old_data_survives_the_knowledge_migration(self):
        self.db.insert("INSERT INTO memories (content) VALUES (?)", ("旧的记忆",))
        self.db.migrate()
        row = self.db.query_one("SELECT content FROM memories LIMIT 1")
        self.assertEqual(row["content"], "旧的记忆")


# ---------- 工具 ----------


class ToolTests(TempEnv):
    def setUp(self):
        super().setUp()
        self.registry = ToolRegistry()
        self.service = self.service()
        register_knowledge_tools(self.registry, service=self.service)
        self.executor = ToolExecutor(
            self.registry, PermissionManager(audit=None), audit=None
        )

    def test_both_tools_are_registered_readonly(self):
        for name in ("knowledge_search", "knowledge_status"):
            tool = self.registry.get(name)
            self.assertIsNotNone(tool, name)
            self.assertTrue(tool.is_readonly, name)

    def test_search_without_documents_says_so(self):
        out = self.executor.execute("knowledge_search", {"query": "随便"})
        self.assertTrue(out.ok, out.error)
        self.assertIn("还没有文档", out.result)

    def test_search_finds_indexed_content(self):
        self.write("a.md", "# 标题\n\n桌宠的长期记忆存在本机。\n")
        self.service.add_root(str(self.src))
        self.service.scan_root(str(self.src))
        out = self.executor.execute("knowledge_search", {"query": "长期记忆"})
        self.assertTrue(out.ok, out.error)
        self.assertIn("a.md", out.result)
        self.assertIn("第", out.result)

    def test_search_requires_a_keyword(self):
        out = self.executor.execute("knowledge_search", {"query": "   "})
        self.assertFalse(out.ok)
        self.assertIn("关键词", out.error)

    def test_status_reports_counts_and_roots(self):
        self.write("a.md", "# 标题\n\n正文。\n")
        self.service.add_root(str(self.src))
        self.service.scan_root(str(self.src))
        out = self.executor.execute("knowledge_status", {})
        self.assertTrue(out.ok, out.error)
        self.assertIn("1 份文档", out.result)
        self.assertIn(str(self.src), out.result)

    def test_tools_without_a_service_stay_honest(self):
        registry = ToolRegistry()
        register_knowledge_tools(registry, service=None)
        executor = ToolExecutor(registry, PermissionManager(audit=None), audit=None)
        out = executor.execute("knowledge_search", {"query": "x"})
        self.assertFalse(out.ok)
        self.assertIn("没有接入知识库", out.error)


# ---------- 技能 ----------


class SkillTests(TempEnv):
    def setUp(self):
        super().setUp()
        self.registry = ToolRegistry()
        register_knowledge_tools(
            self.registry, service=self.service()
        )
        self.executor = ToolExecutor(
            self.registry, PermissionManager(audit=None), audit=None
        )
        self.manager = SkillManager.build(
            config=None, tools=self.executor, database=None
        )

    def test_knowledge_skill_loads_and_validates(self):
        self.assertIn("knowledge-base", self.manager.names())
        report = self.manager.report_of("knowledge-base")
        self.assertTrue(report.ok, report.to_dict())

    def test_skill_only_declares_readonly_tools(self):
        meta = self.manager.registry.get("knowledge-base")
        self.assertEqual(set(meta.tools), {"knowledge_search", "knowledge_status"})

    def test_skill_run_without_a_keyword_asks_for_one(self):
        result = self.manager.run("knowledge-base", inputs={"query": ""})
        self.assertFalse(result.ok)
        self.assertIn("关键词", result.error)


if __name__ == "__main__":
    unittest.main()
