import unittest

from app.utils.release_notes_html import render_release_notes_html


class RenderReleaseNotesHtmlTests(unittest.TestCase):
    def test_headings_lists_and_inline_formatting(self) -> None:
        html = render_release_notes_html(
            "# Music Motion Studio 1.2.0.0\n\n"
            "## 주요 변경 사항\n\n"
            "- **굵게** 그리고 `코드` 와 [링크](https://example.com)\n"
            "- 두 번째 항목\n\n"
            "일반 문단입니다.\n",
            korean=True,
        )
        self.assertIn("<h1>Music Motion Studio 1.2.0.0</h1>", html)
        self.assertIn("<h2>주요 변경 사항</h2>", html)
        self.assertIn("<ul><li><b>굵게</b>", html)
        self.assertIn("<code>코드</code>", html)
        self.assertIn('<a href="https://example.com">링크</a>', html)
        self.assertIn("<p>일반 문단입니다.</p>", html)

    def test_github_admonition_becomes_a_styled_box_with_label(self) -> None:
        html = render_release_notes_html(
            "> [!CAUTION]\n"
            "> 충분한 여유 공간을 확보하세요.\n"
            "> 두 번째 줄.\n",
            korean=True,
        )
        self.assertNotIn("[!CAUTION]", html)
        self.assertIn("경고", html)
        self.assertIn("충분한 여유 공간을 확보하세요. 두 번째 줄.", html)
        self.assertIn("<table", html)

    def test_callouts_never_fill_a_background_and_dark_mode_shifts_the_accent(
        self,
    ) -> None:
        source = "> [!CAUTION]\n> 텍스트\n"
        light = render_release_notes_html(source, korean=True, dark=False)
        dark = render_release_notes_html(source, korean=True, dark=True)
        # A fixed background is what hid the body text in dark mode.
        self.assertNotIn("background-color", light)
        self.assertNotIn("background-color", dark)
        self.assertIn("#CF222E", light)
        self.assertIn("#FF7B72", dark)

    def test_tables_and_code_blocks_and_rules(self) -> None:
        html = render_release_notes_html(
            "| 증분 | 내용 |\n"
            "|---|---|\n"
            "| 1 | 첫 번째 |\n"
            "| 2 | 두 번째 |\n\n"
            "```\nplain code\n```\n\n"
            "---\n",
        )
        self.assertIn("<th", html)
        self.assertIn("<td>첫 번째</td>", html)
        self.assertIn("<pre", html)
        self.assertIn("plain code", html)
        self.assertIn("<hr>", html)

    def test_task_list_items_render_checkboxes(self) -> None:
        html = render_release_notes_html("- [x] 완료\n- [ ] 예정\n")
        self.assertIn("☑ 완료", html)
        self.assertIn("☐ 예정", html)

    def test_html_in_text_is_escaped(self) -> None:
        html = render_release_notes_html("텍스트 <script>alert(1)</script> 끝\n")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_empty_notes_have_a_placeholder(self) -> None:
        self.assertIn("릴리즈 설명이 없습니다", render_release_notes_html("", korean=True))
        self.assertIn("No release notes", render_release_notes_html("   "))


if __name__ == "__main__":
    unittest.main()
