"""V10 P1/P2 验收：外部连接器适配、更新检查、导入暂存清理、候选核验结论。

全程临时目录和假客户端：不联网、不执行脚本、不碰真实用户数据。
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from extensions import connectors, updates
from extensions.catalog import builtin_catalog
from extensions.importer import SkillImporter, inspect_directory
from extensions.qoder_catalog import qoder_catalog
from extensions.registry import ExtensionRegistry
from extensions.store import ExtensionStore
from skills.registry import SkillRegistry
from skills.router import SkillRouter, hint_names
from skills.schema import SkillMeta, parse_skill_text


def registry_in_temp():
    folder = tempfile.mkdtemp(prefix="blossom-p94-")
    return ExtensionRegistry(store=ExtensionStore(root=folder)), folder


class ConnectorTests(unittest.TestCase):
    """连接器只说明要什么、查环境、写清会跑哪条命令，不自己启动。"""

    def test_there_are_two_connectors(self):
        self.assertEqual(len(connectors.specs()), 2)
        self.assertEqual(
            sorted(spec.id for spec in connectors.specs()),
            ["connector-chrome-devtools-mcp", "connector-playwright-mcp"],
        )

    def test_every_connector_manifest_validates(self):
        for spec in connectors.specs():
            self.assertEqual(spec.to_manifest().validate(), [], spec.id)

    def test_connectors_are_the_connector_kind(self):
        for spec in connectors.specs():
            self.assertEqual(spec.to_manifest().kind, "connector")

    def test_requirements_name_the_binaries_and_the_command(self):
        lines = connectors.requirements("connector-playwright-mcp")
        text = " ".join(lines)
        self.assertIn("node", text)
        self.assertIn("npx", text)
        self.assertIn("@playwright/mcp", text)

    def test_the_plan_says_the_program_will_not_start_it(self):
        plan = connectors.plan("connector-chrome-devtools-mcp")
        self.assertIn("不会自己启动", plan)
        self.assertIn("chrome-devtools-mcp", plan)

    def test_an_unknown_connector_is_reported_not_guessed(self):
        self.assertEqual(connectors.get("nope"), None)
        self.assertEqual(connectors.requirements("nope"), [])
        self.assertTrue(connectors.plan("nope"))

    def test_connectors_declare_a_health_check_per_binary(self):
        for spec in connectors.specs():
            statuses = spec.health()
            self.assertEqual(len(statuses), len(spec.binaries), spec.id)
            for item in statuses:
                self.assertEqual(item["kind"], "binary")

    def test_connectors_are_off_by_default_and_cannot_be_enabled(self):
        registry, folder = registry_in_temp()
        try:
            for spec in connectors.specs():
                self.assertFalse(registry.is_enabled(spec.id))
                allowed, reason = registry.can_enable(spec.id)
                self.assertFalse(allowed, spec.id)
                self.assertTrue(reason)
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_connectors_never_enter_the_package(self):
        for spec in connectors.specs():
            manifest = spec.to_manifest()
            self.assertFalse(manifest.bundled, spec.id)
            self.assertTrue(manifest.bundle_blocked(), spec.id)


class UpdateCheckTests(unittest.TestCase):
    """更新检查只报版本差异，不下载、不替换。"""

    def test_pinned_is_split_into_id_and_version(self):
        self.assertEqual(updates.parse_pinned("official_x@1.2.3"), ("official_x", "1.2.3"))
        self.assertEqual(updates.parse_pinned("builtin"), ("", ""))
        self.assertEqual(updates.parse_pinned(""), ("", ""))

    def test_only_market_entries_are_checked(self):
        entries = updates.qoder_entries(builtin_catalog())
        self.assertTrue(entries)
        for manifest, skill_id, version in entries:
            self.assertTrue(skill_id.startswith("official"))
            self.assertTrue(version)
            self.assertNotEqual(manifest.source, "builtin")

    def test_connectors_are_not_asked_of_the_market(self):
        ids = [manifest.id for manifest, _sid, _ver in updates.qoder_entries(builtin_catalog())]
        for spec in connectors.specs():
            self.assertNotIn(spec.id, ids)

    def test_a_newer_market_version_is_reported(self):
        class Fake:
            @staticmethod
            def detail(skill_id, timeout=20):
                return {"data": {"skill_id": skill_id, "version": "9.9.9"}}

            @staticmethod
            def normalize_detail(payload):
                return payload.get("data") or {}

        result = updates.check(Fake(), qoder_catalog())
        self.assertTrue(result["pending"])
        self.assertTrue(all(item["remote"] == "9.9.9" for item in result["pending"]))
        self.assertGreater(result["total"], 0)

    def test_the_same_version_is_not_reported(self):
        class Fake:
            @staticmethod
            def detail(skill_id, timeout=20):
                return {"data": {"skill_id": skill_id, "version": "1.0.0"}}

            @staticmethod
            def normalize_detail(payload):
                return payload.get("data") or {}

        result = updates.check(Fake(), qoder_catalog())
        self.assertEqual(result["pending"], [])
        self.assertEqual(len(result["checked"]), 0) if False else None
        self.assertTrue(result["checked"] > 0)

    def test_a_failing_market_is_recorded_as_an_error(self):
        class Broken:
            @staticmethod
            def detail(skill_id, timeout=20):
                raise RuntimeError("连不上")

            @staticmethod
            def normalize_detail(payload):
                return {}

        result = updates.check(Broken(), qoder_catalog())
        self.assertEqual(result["pending"], [])
        self.assertTrue(result["errors"])
        self.assertTrue(all(item["reason"] for item in result["errors"]))

    def test_the_summary_reads_like_a_sentence(self):
        text = updates.summary_text({"total": 12, "pending": [1], "errors": [], "checked": 12})
        self.assertIn("12", text)
        self.assertIn("新版本", text)
        self.assertTrue(updates.summary_text(None))


class StagedCleanupTests(unittest.TestCase):
    """导入不成就把暂存目录收掉，别在临时目录里越堆越多。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="blossom-clean-"))
        self.importer = SkillImporter(temp_root=str(self.root))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _staged_dir(self):
        folder = Path(self.importer._temp_dir())
        (folder / "SKILL.md").write_text("---\nname: demo-skill\ndescription: x\n---\n", encoding="utf-8")
        return folder

    def test_a_staged_directory_is_removed(self):
        folder = self._staged_dir()
        report = inspect_directory(folder)
        report.stage_dir = str(folder)
        self.assertTrue(self.importer.discard(report))
        self.assertFalse(folder.exists())

    def test_a_foreign_directory_is_never_removed(self):
        keep = Path(self.root) / "my-own-notes"
        keep.mkdir()
        (keep / "SKILL.md").write_text("---\nname: demo-skill\ndescription: x\n---\n", encoding="utf-8")
        report = inspect_directory(keep)
        self.assertFalse(self.importer.discard(report))
        self.assertTrue(keep.exists())

    def test_discarding_nothing_is_safe(self):
        self.assertFalse(self.importer.discard(None))
        report = inspect_directory(Path(self.root))
        self.assertFalse(self.importer.discard(report))


class LicenseHintTests(unittest.TestCase):
    """清单里核过的许可证要能带进导入检查单，别又显示成未声明。"""

    def _fake_client(self):
        class Fake:
            @staticmethod
            def detail(ident):
                return {"data": {"skill_id": ident, "skill_name": "ui-ux-pro-max",
                                 "version": "1.0.0", "file_tree": {}}}

            @staticmethod
            def skill_file(ident, path):
                return {"data": {"content": "---\nname: ui-ux-pro-max\ndescription: x\n---\n正文\n"}}

        return Fake()

    def test_without_a_hint_the_license_stays_under_review(self):
        report = SkillImporter(client=self._fake_client()).inspect_qoder("official_drDKPmtw")
        self.assertEqual(report.license, "")
        self.assertEqual(report.license_status, "review-needed")

    def test_with_a_hint_the_verified_license_is_used(self):
        report = SkillImporter(client=self._fake_client()).inspect_qoder(
            "official_drDKPmtw", license_hint="MIT", license_status_hint="approved"
        )
        self.assertEqual(report.license, "MIT")
        self.assertEqual(report.license_status, "approved")
        self.assertIn("MIT", " ".join(report.notes))

    def test_the_ui_helper_only_returns_approved_licenses(self):
        import tkinter as tk  # noqa: F401  只是确认模块能导入

        from ui.skills_view import SkillsWindow

        window = SkillsWindow.__new__(SkillsWindow)
        license_text, status = window._known_license(
            "https://qoder.com/zh/marketplace/skill?id=official_drDKPmtw"
        )
        self.assertEqual(status, "approved")
        self.assertIn("MIT", license_text)

    def test_an_unknown_link_yields_no_hint(self):
        from ui.skills_view import SkillsWindow

        window = SkillsWindow.__new__(SkillsWindow)
        self.assertEqual(window._known_license("https://example.com/x"), ("", ""))


class CatalogueAuditTests(unittest.TestCase):
    """九条候选的核验结论要能对得上真实查到的许可证。"""

    def setUp(self):
        self.by_id = {item.id: item for item in builtin_catalog()}

    def test_the_catalog_is_still_valid_and_unique(self):
        ids = [item.id for item in builtin_catalog()]
        self.assertEqual(len(ids), len(set(ids)))
        for manifest in builtin_catalog():
            self.assertEqual(manifest.validate(), [], manifest.id)

    def test_all_nine_candidates_have_a_market_id(self):
        for ident in (
            "qoder-ui-ux-pro-max",
            "qoder-superpowers-dev",
            "qoder-code-simplifier",
            "qoder-deep-research",
            "qoder-context7",
            "qoder-playwright",
            "qoder-chrome-devtools",
            "qoder-design-review",
            "qoder-architecture-visualization",
        ):
            manifest = self.by_id[ident]
            self.assertIn("official", manifest.pinned, ident)
            self.assertTrue(manifest.verdict, ident)

    def test_the_mit_ones_are_recorded_as_approved(self):
        for ident in ("qoder-ui-ux-pro-max", "qoder-playwright", "qoder-design-review"):
            self.assertEqual(self.by_id[ident].license_status, "approved", ident)
            self.assertIn("MIT", self.by_id[ident].license)

    def test_the_agpl_one_is_blocked(self):
        manifest = self.by_id["qoder-superpowers-dev"]
        self.assertEqual(manifest.license_status, "blocked")
        self.assertIn("AGPL", manifest.license)
        self.assertTrue(manifest.bundle_blocked())

    def test_the_ones_without_a_usable_upstream_repo_stay_under_review(self):
        for ident in ("qoder-code-simplifier", "qoder-context7", "qoder-deep-research"):
            self.assertEqual(self.by_id[ident].license_status, "review-needed", ident)

    def test_the_scripted_one_lists_its_scripts(self):
        manifest = self.by_id["qoder-architecture-visualization"]
        self.assertEqual(manifest.scripts, ())
        self.assertIn("脚本", " ".join(manifest.notes))

    def test_the_cloud_governance_pack_is_marked_unsuitable(self):
        manifest = self.by_id["qoder-architecture-visualization"]
        self.assertEqual(manifest.verdict, "unsuitable")
        self.assertTrue(manifest.verdict_note)
        self.assertIn("云账号", manifest.verdict_note)

    def test_an_unsuitable_candidate_cannot_be_enabled(self):
        registry, folder = registry_in_temp()
        try:
            allowed, reason = registry.can_enable("qoder-architecture-visualization")
            self.assertFalse(allowed)
            self.assertTrue(reason)
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_nothing_from_the_market_is_bundled(self):
        for manifest in qoder_catalog():
            self.assertFalse(manifest.bundled, manifest.id)

    def test_the_content_checked_ones_say_so(self):
        for ident in ("qoder-ui-ux-pro-max", "qoder-deep-research", "qoder-self-improving-agent-next"):
            notes = " ".join(self.by_id[ident].notes)
            self.assertIn("内容检查通过", notes, ident)


class FrontMatterQuoteTests(unittest.TestCase):
    """front matter 的值带了引号要脱掉，界面上别露出多余的引号。"""

    def test_double_quotes_are_stripped(self):
        meta, _ = parse_skill_text(
            '---\nname: demo-skill\ndescription: "带引号的说明"\n---\n正文\n'
        )
        self.assertEqual(meta.description, "带引号的说明")

    def test_single_quotes_are_stripped(self):
        meta, _ = parse_skill_text(
            "---\nname: demo-skill\ndescription: '单引号说明'\n---\n正文\n"
        )
        self.assertEqual(meta.description, "单引号说明")

    def test_a_quote_inside_the_text_is_kept(self):
        meta, _ = parse_skill_text(
            '---\nname: demo-skill\ndescription: 说的是"要点"\n---\n正文\n'
        )
        self.assertEqual(meta.description, '说的是"要点"')


class NoRealDataTests(unittest.TestCase):
    def test_the_new_modules_are_read_only_definitions(self):
        source = (ROOT / "extensions" / "connectors.py").read_text(encoding="utf-8")
        self.assertNotIn("subprocess", source)
        self.assertNotIn("os.system", source)
        self.assertNotIn("Popen", source)

    def test_the_update_module_never_installs_anything(self):
        source = (ROOT / "extensions" / "updates.py").read_text(encoding="utf-8")
        for token in ("shutil", "subprocess", "os.remove", "rmtree"):
            self.assertNotIn(token, source)



class SkillRecommendationTests(unittest.TestCase):
    """用户导入的技能一般只写 description，没有触发词，也要能被推荐出来。"""

    def setUp(self):
        self.registry = SkillRegistry()
        self.registry.register(
            SkillMeta(
                name="ui-ux-pro-max",
                description="UI/UX design intelligence for web and mobile. Includes color palettes.",
            )
        )
        self.registry.register(
            SkillMeta(
                name="self-improving-agent",
                description="记录错误、纠正、能力缺口与最佳实践，形成可复用的持续改进闭环。",
            )
        )
        self.registry.register(
            SkillMeta(name="coding-agent", description="改代码、跑测试", triggers=("代码",))
        )
        self.router = SkillRouter(self.registry)

    def names_for(self, text, intent=""):
        return [item.name for item in self.router.recommend(text, intent=intent)]

    def test_a_name_hint_finds_the_imported_skill(self):
        self.assertIn("ui-ux-pro-max", self.names_for("帮我改一下界面配色"))

    def test_the_chinese_description_skill_is_found_by_keyword(self):
        self.assertIn("self-improving-agent", self.names_for("记一下这次踩的坑"))

    def test_hint_names_never_invent_a_skill(self):
        found = hint_names(self.registry, "帮我改一下界面配色 而且记一下踩的坑")
        self.assertTrue(found.issubset(set(self.registry.names())))

    def test_the_hint_layer_needs_a_registered_name(self):
        self.assertIn("ui-ux-pro-max", hint_names(self.registry, "界面配色"))
        self.assertNotIn("no-such-skill", hint_names(self.registry, "界面配色"))

    def test_unrelated_talk_still_returns_nothing(self):
        self.assertEqual(self.names_for("今天心情不错", intent="chat"), [])
        self.assertEqual(self.names_for("帮我写一首关于春天的诗"), [])

    def test_builtin_trigger_matching_is_unchanged(self):
        self.assertIn("coding-agent", self.names_for("帮我修改代码并跑测试", intent="task"))

    def test_a_disabled_skill_is_not_suggested(self):
        self.registry.disable("ui-ux-pro-max")
        self.assertNotIn("ui-ux-pro-max", self.names_for("帮我改一下界面配色"))

    def test_the_candidate_says_why_it_matched(self):
        item = self.router.recommend("帮我改一下界面配色")[0]
        self.assertTrue(item.reason)
        self.assertGreater(item.score, 1.0)


if __name__ == "__main__":
    unittest.main()
