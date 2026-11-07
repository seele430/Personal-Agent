"""报告渲染与引用对账测试。

不联网：证据放在临时目录的 evidence.db 里。
"""

import tempfile
import unittest

from pathlib import Path

from src.research.prompts import (
    Paragraph,
    Section,
    WriterDraft,
)
from src.research.report import (
    ReportError,
    check_report,
    count_chars,
    render_report,
    summarize,
)
from src.web.evidence import EvidenceStore


def draft(ids, *, sections=2, text="这是一段用于测试的报告正文，包含足够的字符以验证字数口径。", limitations=""):
    built = []

    for index in range(sections):
        built.append(
            Section(
                heading=f"小节 {index + 1}",
                paragraphs=(
                    Paragraph(text=f"{text}（{index}）", evidence_ids=tuple(ids)),
                ),
            )
        )

    return WriterDraft(
        title="测试报告",
        summary="这是一份用于验证渲染与引用对账的摘要。",
        sections=tuple(built),
        limitations=limitations,
    )


class ReportTestCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)

        self.store = EvidenceStore(Path(self._temporary.name) / "evidence.db")
        self.ids = []

        for index in range(12):
            record = self.store.add_source(
                url=f"https://evidence.example.org/{index}",
                text=f"证据正文 {index} " * 20,
                title=f"证据 {index}",
            )
            self.ids.append(record.id)


class TestRendering(ReportTestCase):
    def test_renders_sections_and_inline_citations(self):
        report = render_report(
            draft(self.ids[:2]), self.store, min_chars=10, min_sources=2
        )

        self.assertIn("# 测试报告", report.markdown)
        self.assertIn("## 摘要", report.markdown)
        self.assertIn("## 小节 1", report.markdown)
        self.assertIn(f"[E{self.ids[0]}]", report.markdown)
        self.assertIn("## 参考来源", report.markdown)
        self.assertTrue(report.passed)

    def test_appendix_has_url_and_sha_prefix(self):
        report = render_report(
            draft(self.ids[:1]), self.store, min_chars=10, min_sources=1
        )
        record = self.store.get_source(self.ids[0])

        self.assertIn(record.url, report.markdown)
        self.assertIn(record.text_sha256[:12], report.markdown)

    def test_body_chars_exclude_appendix(self):
        report = render_report(
            draft(self.ids[:2]), self.store, min_chars=10, min_sources=2
        )

        self.assertLess(report.char_count, count_chars(report.markdown))
        self.assertGreater(report.char_count, 0)

    def test_duplicate_ids_are_counted_once(self):
        duplicated = [self.ids[0], self.ids[0], self.ids[1]]

        report = render_report(
            draft(duplicated), self.store, min_chars=10, min_sources=2
        )

        self.assertEqual(report.source_count, 2)
        self.assertEqual(report.markdown.count("## 参考来源"), 1)
        self.assertEqual(report.markdown.count(f"- [E{self.ids[0]}]"), 1)

    def test_limitations_rendered_only_when_present(self):
        with_limits = render_report(
            draft(self.ids[:1], limitations="缺少生产数据"),
            self.store,
            min_chars=10,
            min_sources=1,
        )
        without = render_report(
            draft(self.ids[:1]), self.store, min_chars=10, min_sources=1
        )

        self.assertIn("## 局限与未解问题", with_limits.markdown)
        self.assertNotIn("## 局限与未解问题", without.markdown)

    def test_question_line_is_optional(self):
        with_question = render_report(
            draft(self.ids[:1]),
            self.store,
            question="PagedAttention 如何管理显存",
            min_chars=10,
            min_sources=1,
        )
        without = render_report(
            draft(self.ids[:1]), self.store, min_chars=10, min_sources=1
        )

        self.assertIn("研究问题：PagedAttention", with_question.markdown)
        self.assertNotIn("研究问题", without.markdown)

    def test_title_newlines_are_collapsed(self):
        dirty = WriterDraft(
            title="第一行\n第二行",
            summary="摘要",
            sections=(
                Section("小节", (Paragraph("正文", (self.ids[0],)),)),
                Section("小节二", (Paragraph("正文", (self.ids[0],)),)),
            ),
        )

        report = render_report(dirty, self.store, min_chars=1, min_sources=1)

        self.assertIn("# 第一行 第二行", report.markdown)


class TestIntegrity(ReportTestCase):
    def test_missing_evidence_raises(self):
        with self.assertRaises(ReportError) as caught:
            render_report(draft([self.ids[0], 9999]), self.store, min_chars=1, min_sources=1)

        self.assertIn("E9999", str(caught.exception))

    def test_non_draft_input_is_rejected(self):
        with self.assertRaises(TypeError):
            render_report({"title": "x"}, self.store)

    def test_invalid_thresholds_are_rejected(self):
        for kwargs in ({"min_chars": -1}, {"min_sources": -1}, {"min_chars": True}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    render_report(draft([self.ids[0]]), self.store, **kwargs)


class TestThresholds(ReportTestCase):
    def test_short_report_is_partial_not_error(self):
        report = render_report(
            draft(self.ids[:2]), self.store, min_chars=100_000, min_sources=2
        )

        self.assertFalse(report.passed)
        self.assertTrue(any("字数不足" in item for item in report.problems))
        # 关键：不足也要给出产物，不许只抛异常了事
        self.assertIn("# 测试报告", report.markdown)

    def test_few_sources_is_partial(self):
        report = render_report(
            draft(self.ids[:2]), self.store, min_chars=10, min_sources=10
        )

        self.assertFalse(report.passed)
        self.assertTrue(any("来源不足" in item for item in report.problems))

    def test_custom_thresholds_pass(self):
        report = render_report(
            draft(self.ids), self.store, min_chars=10, min_sources=12
        )

        self.assertTrue(report.passed)
        self.assertEqual(report.source_count, 12)


class TestCheckReport(ReportTestCase):
    def test_valid_report_has_no_missing_ids(self):
        report = render_report(
            draft(self.ids[:3]), self.store, min_chars=10, min_sources=3
        )

        self.assertEqual(check_report(report.markdown, self.store), ())

    def test_fabricated_citation_is_detected(self):
        text = "# 报告\n\n正文 [E1] 又一处 [E9999]\n"

        missing = check_report(text, self.store)

        self.assertIn(9999, missing)

    def test_empty_markdown_is_rejected(self):
        for bad in ("", "   ", None):
            with self.subTest(value=bad):
                with self.assertRaises(ReportError):
                    check_report(bad, self.store)


class TestSummary(ReportTestCase):
    def test_summary_marks_partial(self):
        passing = render_report(
            draft(self.ids[:2]), self.store, min_chars=10, min_sources=2
        )
        failing = render_report(
            draft(self.ids[:2]), self.store, min_chars=100_000, min_sources=2
        )

        self.assertTrue(summarize(passing).startswith("PASS"))
        self.assertTrue(summarize(failing).startswith("PARTIAL"))
        self.assertIn("字数不足", summarize(failing))


if __name__ == "__main__":
    unittest.main()
