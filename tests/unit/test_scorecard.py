"""ScoreCard 测试：分数必须由事实推导，硬门槛必须真的能一票否决。"""

import unittest

from src.eval.score import (
    AVERAGE_SCORE,
    PASS_SCORE,
    ScoreError,
    generic_items,
    report_items,
    score_report,
    score_snake,
    snake_items,
    summarize,
)


def good_report(citations=20, chars_per_para=120, sections=4, domains=5):
    paragraphs = []

    for index in range(sections):
        body = "证据支持的分析正文。" * (chars_per_para // 10)
        marks = "".join(
            f"[E{(index * 3 + offset) % citations + 1}]" for offset in range(3)
        )
        paragraphs.append(f"## 小节 {index}\n\n{body} {marks}\n")

    appendix = "\n".join(
        f"- [E{i}] 来源 {i} — https://site{i % domains}.example.org/x"
        for i in range(1, citations + 1)
    )

    return (
        "# 报告\n\n## 摘要\n\n这是摘要。\n\n"
        + "\n".join(paragraphs)
        + "\n## 局限与未解问题\n\n缺生产数据。\n\n## 参考来源\n\n"
        + appendix
        + "\n"
    )


class TestGeneric(unittest.TestCase):
    def test_full_marks(self):
        items = generic_items(
            artifact_exists=True,
            artifact_opens=True,
            citations_ok=True,
            reference_count=12,
            crash_free=True,
            safety_ok=True,
            within_budget=True,
            notified=True,
        )

        self.assertEqual(sum(item.score for item in items), 60.0)

    def test_missing_artifact_zeroes_requirement(self):
        items = generic_items(
            artifact_exists=False,
            artifact_opens=False,
            citations_ok=False,
            reference_count=0,
            crash_free=False,
            safety_ok=False,
            within_budget=False,
            notified=False,
        )

        self.assertEqual(sum(item.score for item in items), 2.0)

    def test_unverifiable_gets_partial_credit(self):
        items = generic_items(
            artifact_exists=True,
            artifact_opens=True,
            citations_ok=False,
            reference_count=5,
            crash_free=True,
            safety_ok=True,
            within_budget=True,
            notified=True,
        )

        verifiable = next(item for item in items if item.key == "verifiable")

        self.assertEqual(verifiable.score, 8.0)


class TestReportRubric(unittest.TestCase):
    def test_density_and_structure(self):
        items = report_items(good_report(), source_count=12)

        by_key = {item.key: item for item in items}

        self.assertEqual(by_key["structure"].score, 8.0)
        self.assertEqual(by_key["sources"].score, 10.0)
        self.assertGreater(by_key["evidence_density"].score, 0.0)

    def test_missing_sections_lose_points(self):
        text = "## 小节 1\n\n正文 [E1]\n\n## 参考来源\n\n- [E1] x — https://a.org\n"

        items = report_items(text, source_count=1)
        by_key = {item.key: item for item in items}

        self.assertLess(by_key["structure"].score, 8.0)
        self.assertEqual(by_key["sources"].score, 0.0)

    def test_density_bands(self):
        sparse = "## 摘要\n\n" + ("正文。" * 2000) + "\n## 局限\n\nx\n"

        items = report_items(sparse, source_count=3)
        density = next(item for item in items if item.key == "evidence_density")

        self.assertLessEqual(density.score, 2.0)

    def test_diversity_counts_domains(self):
        text = good_report(domains=6)

        items = report_items(text, source_count=12)

        self.assertEqual(
            next(item for item in items if item.key == "diversity").score, 6.0
        )

    def test_empty_report_is_rejected(self):
        for bad in ("", "   ", None):
            with self.subTest(value=bad):
                with self.assertRaises(ScoreError):
                    report_items(bad, source_count=0)


class TestSnakeRubric(unittest.TestCase):
    def test_full_marks(self):
        items = snake_items(
            loads=True,
            controls_ok=True,
            score_works=True,
            game_over_works=True,
            ticks_500_ok=True,
            mobile_ok=True,
            draws=True,
        )

        self.assertEqual(sum(item.score for item in items), 40.0)

    def test_partial_rules(self):
        items = snake_items(
            loads=True,
            controls_ok=False,
            score_works=False,
            game_over_works=False,
            ticks_500_ok=True,
            mobile_ok=False,
            draws=True,
        )

        by_key = {item.key: item for item in items}

        self.assertEqual(by_key["rules"].score, 5.0)
        self.assertEqual(by_key["mobile"].score, 0.0)


class TestHardGates(unittest.TestCase):
    def test_empty_report_fails_gate(self):
        card = score_report("", source_count=0)

        self.assertFalse(card.passed)
        self.assertIn("产物不存在", card.hard_gate_failures[0])
        self.assertEqual(card.total, 0.0)

    def test_missing_citation_fails_gate(self):
        class Store:
            def get_source(self, artifact_id):
                return None

        card = score_report(
            good_report(), source_count=12, evidence_store=Store()
        )

        self.assertFalse(card.passed)
        self.assertTrue(any("引用无法命中" in item for item in card.hard_gate_failures))

    def test_snake_without_load_fails_gate(self):
        card = score_snake(
            loads=False,
            controls_ok=False,
            score_works=False,
            game_over_works=False,
            ticks_500_ok=False,
            mobile_ok=False,
            draws=False,
        )

        self.assertFalse(card.passed)
        self.assertIn("产物打不开", card.hard_gate_failures[0])


class TestSummarize(unittest.TestCase):
    def test_all_pass(self):
        cards = [
            score_snake(
                loads=True,
                controls_ok=True,
                score_works=True,
                game_over_works=True,
                ticks_500_ok=True,
                mobile_ok=True,
                draws=True,
            )
            for _ in range(3)
        ]

        summary = summarize(cards)

        self.assertTrue(summary["overall"])
        self.assertEqual(summary["runs"], 3)
        self.assertFalse(summary["spread_warning"])

    def test_spread_warning(self):
        strong = score_snake(
            loads=True,
            controls_ok=True,
            score_works=True,
            game_over_works=True,
            ticks_500_ok=True,
            mobile_ok=True,
            draws=True,
        )
        weak = score_snake(
            loads=True,
            controls_ok=True,
            score_works=False,
            game_over_works=False,
            ticks_500_ok=False,
            mobile_ok=False,
            draws=False,
        )

        summary = summarize([strong, weak, strong])

        self.assertTrue(summary["spread_warning"])
        self.assertFalse(summary["overall"])

    def test_empty_raises(self):
        with self.assertRaises(ScoreError):
            summarize([])

    def test_constant_targets(self):
        self.assertEqual(PASS_SCORE, 85)
        self.assertEqual(AVERAGE_SCORE, 90)


class TestMoreBands(unittest.TestCase):
    """补齐：边界档位与硬门槛组合。"""

    def test_efficiency_partial_when_over_budget(self):
        items = generic_items(
            artifact_exists=True,
            artifact_opens=True,
            citations_ok=True,
            reference_count=10,
            crash_free=True,
            safety_ok=True,
            within_budget=False,
            notified=True,
        )

        self.assertEqual(
            next(item for item in items if item.key == "efficiency").score, 2.0
        )

    def test_missing_abstract_penalized(self):
        text = (
            "# 报告\n\n## 小节 1\n\n正文 [E1]\n\n## 小节 2\n\n正文 [E2]\n\n"
            "## 小节 3\n\n正文 [E3]\n\n## 参考来源\n\n- [E1] x — https://a.org\n"
        )

        items = report_items(text, source_count=3)

        self.assertLess(
            next(item for item in items if item.key == "structure").score, 8.0
        )

    def test_no_limitations_scores_zero(self):
        text = "## 摘要\n\n摘要\n\n## 小节\n\n正文 [E1]\n\n## 参考来源\n\n- [E1] x — https://a.org\n"

        items = report_items(text, source_count=1)

        self.assertEqual(
            next(item for item in items if item.key == "honesty").score, 0.0
        )

    def test_snake_visual_zero_when_not_drawn(self):
        items = snake_items(
            loads=True,
            controls_ok=True,
            score_works=True,
            game_over_works=True,
            ticks_500_ok=True,
            mobile_ok=True,
            draws=False,
        )

        self.assertEqual(
            next(item for item in items if item.key == "visual").score, 0.0
        )

    def test_crash_fails_report_gate(self):
        card = score_report(
            good_report(), source_count=12, crash_free=False
        )

        self.assertFalse(card.passed)
        self.assertIn("执行崩溃", card.hard_gate_failures)

    def test_summary_average_ok_but_one_run_failed(self):
        from src.eval.score import ScoreCard, Item

        def card(total):
            return ScoreCard(
                kind="x", items=(Item("a", "a", total, 100.0),)
            )

        summary = summarize([card(98.0), card(95.0), card(80.0)])

        self.assertFalse(summary["every_run_passed"])
        self.assertTrue(summary["average_ok"])
        self.assertFalse(summary["overall"])

    def test_report_maximum_is_one_hundred(self):
        card = score_report(good_report(), source_count=12)

        self.assertEqual(card.maximum, 100.0)
if __name__ == "__main__":
    unittest.main()
