"""P70：聊天区单条消息操作，以及打包脚本里的开始菜单入口。

这一层要保证的事：
    右键点在一条消息上，菜单知道「这条」是哪条；
    复制一条不会带上别的消息；只有最后一条回复能重新生成；
    点在空白处两个动作都不给点；
    开始菜单快捷方式是可选开关，默认不写进用户开始菜单。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tkinter as tk

from ui.chat_view import ChatView


class MessageActionTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except Exception as exc:
            self.skipTest("tkinter 不可用：%s" % exc)
        self.root.withdraw()
        self.addCleanup(self._safe_destroy)
        self.sent = []
        self.view = ChatView(self.root, on_send=lambda: self.sent.append(1))

    def _safe_destroy(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _labels(self):
        return [
            self.view.menu.entrycget(index, "label")
            for index in range(self.view.menu.index("end") + 1)
            if self.view.menu.type(index) == "command"
        ]

    def _line_of(self, tag, last=False):
        """按标签找起始行：消息之间有空隙行，不能再按固定行号断言。"""
        ranges = self.view.transcript.tag_ranges(tag)
        if not ranges:
            return ""
        return str(ranges[-1] if last else ranges[0])

    def test_the_menu_carries_the_two_single_message_actions(self):
        labels = self._labels()
        self.assertIn("复制这条", labels)
        self.assertIn("重新生成这条", labels)

    def test_a_line_points_at_the_message_it_carries(self):
        self.view.append("me", "帮我看看这段代码")
        self.view.append("pet", "好，把文件发我。")
        me_tag, me_text, _f, _l = self.view.message_at(self._line_of("me"))
        self.assertEqual(me_tag, "me")
        self.assertEqual(me_text, "帮我看看这段代码")
        pet_tag, pet_text, _f, _l = self.view.message_at(self._line_of("pet"))
        self.assertEqual(pet_tag, "pet")
        self.assertEqual(pet_text, "好，把文件发我。")

    def test_a_blank_line_belongs_to_no_message(self):
        self.view.append("me", "在吗")
        _tag, text, first, last = self.view.message_at("9.0")
        self.assertEqual((text, first, last), ("", 0, 0))

    def test_copying_a_message_copies_only_that_message(self):
        self.view.append("me", "第一句")
        self.view.append("pet", "第二句")
        copied = self.view.copy_message_at(self._line_of("me"))
        self.assertEqual(copied, "第一句")
        self.assertEqual(self.root.clipboard_get(), "第一句")
        self.assertNotIn("第二句", copied)

    def test_a_code_block_is_copied_with_its_body(self):
        self.view.append("pet", "看这段：")
        self.view.append_code("print(1)\nprint(2)", "python")
        tag, text, _f, last = self.view.message_at(self._line_of("code"))
        self.assertEqual(tag, "code")
        self.assertIn("print(1)", text)
        self.assertIn("print(2)", text)

    def test_only_the_newest_reply_can_be_regenerated(self):
        self.view.append("me", "第一问")
        self.view.append("pet", "第一答")
        self.view.append("me", "第二问")
        self.view.append("pet", "第二答")
        self.assertFalse(self.view.regenerate_message_at(self._line_of("pet")))
        self.assertEqual(self.sent, [])
        self.assertTrue(self.view.regenerate_message_at(self._line_of("pet", last=True)))
        self.assertEqual(self.sent, [1])

    def test_a_question_of_yours_is_not_something_to_regenerate(self):
        self.view.append("me", "在吗")
        self.assertFalse(self.view.regenerate_message_at(self._line_of("me")))
        self.assertEqual(self.sent, [])

    def test_nothing_regenerates_while_an_answer_is_still_coming(self):
        self.view.append("pet", "先说到一半")
        self.view.begin_stream()
        try:
            self.assertFalse(self.view.regenerate_message_at(self._line_of("pet")))
        finally:
            self.view.end_stream(body="")

    def test_the_menu_greys_out_what_does_not_apply(self):
        self.view.append("me", "在吗")
        self.view._menu_line = self._line_of("me")
        self.view._menu_state()
        self.assertEqual(self.view.menu.entrycget("复制这条", "state"), "normal")
        self.assertEqual(self.view.menu.entrycget("重新生成这条", "state"), "disabled")

    def test_the_menu_greys_out_on_empty_space(self):
        self.view.append("me", "在吗")
        self.view._menu_line = "12.0"
        self.view._menu_state()
        self.assertEqual(self.view.menu.entrycget("复制这条", "state"), "disabled")
        self.assertEqual(self.view.menu.entrycget("重新生成这条", "state"), "disabled")


class BuildScriptTests(unittest.TestCase):
    def script(self):
        return (ROOT / "build.ps1").read_text(encoding="utf-8")

    def test_the_start_menu_shortcut_is_an_opt_in_switch(self):
        body = self.script()
        self.assertIn("[switch]$StartMenu", body)
        self.assertIn("GetFolderPath(\"StartMenu\")", body)

    def test_one_helper_builds_both_shortcuts(self):
        body = self.script()
        self.assertIn("function New-Shortcut", body)
        self.assertEqual(body.count("New-Shortcut ("), 2)

    def test_the_desktop_shortcut_still_comes_from_the_same_place(self):
        body = self.script()
        self.assertIn("GetFolderPath(\"Desktop\")", body)
        self.assertIn("[switch]$NoShortcut", body)


if __name__ == "__main__":
    unittest.main()
