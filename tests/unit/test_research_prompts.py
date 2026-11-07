"""提示词契约单元测试：模型返回的 JSON 必须逐个字段过关。

夹具在 `tests/fixtures/research/`，取值故意贴近真实输出
（含"score 与 verdict 不一致"这类真实会出现的矛盾）。
"""

import json
import unittest

from pathlib import Path

from src.research.prompts import (
    PASS_SCORE,
    PromptContractError,
    build_evidence_digest,
    critic_messages,
    planner_messages,
    validate_analyst,
    validate_critic,
    validate_planner,
    validate_writer,
)


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "research"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class TestPlannerContract(unittest.TestCase):
    def test_valid_plan(self):
        plan = validate_planner(load("planner.json"), question="Q")

        self.assertEqual(plan.question, "Q")
        self.assertEqual(len(plan.sub_queries), 4)
        self.assertIn("PagedAttention", plan.title_hint)

    def test_too_few_queries_is_rejected(self):
        payload = load("planner.json")
        payload["sub_queries"] = payload["sub_queries"][:2]

        with self.assertRaises(PromptContractError):
            validate_planner(payload, question="Q")

    def test_duplicate_queries_are_rejected(self):
        payload = load("planner.json")
        payload["sub_queries"][1]["query"] = payload["sub_queries"][0]["query"]

        with self.assertRaises(PromptContractError):
            validate_planner(payload, question="Q")

    def test_non_object_payload_is_rejected(self):
        for bad in ([], "x", None, 3):
            with self.subTest(payload=bad):
                with self.assertRaises(PromptContractError):
                    validate_planner(bad, question="Q")

    def test_missing_query_field_is_rejected(self):
        payload = load("planner.json")
        del payload["sub_queries"][0]["query"]

        with self.assertRaises(PromptContractError):
            validate_planner(payload, question="Q")


class TestAnalystContract(unittest.TestCase):
    def test_gap_payload(self):
        analysis = validate_analyst(load("analyst_gap.json"))

        self.assertTrue(analysis.has_gaps)
        self.assertEqual(len(analysis.findings), 2)
        self.assertEqual(analysis.findings[0].evidence_ids, (1, 2))
        self.assertIn("production", analysis.gaps[0].query)

    def test_no_gap_payload(self):
        analysis = validate_analyst(load("analyst_ok.json"))

        self.assertFalse(analysis.has_gaps)
        self.assertEqual(len(analysis.findings), 3)

    def test_finding_without_evidence_is_rejected(self):
        payload = load("analyst_ok.json")
        payload["findings"][0]["evidence_ids"] = []

        with self.assertRaises(PromptContractError):
            validate_analyst(payload)

    def test_bad_evidence_ids_are_rejected(self):
        for bad in ([True], [0], [-1], ["1"], 1):
            payload = load("analyst_ok.json")
            payload["findings"][0]["evidence_ids"] = bad

            with self.subTest(bad=bad):
                with self.assertRaises(PromptContractError):
                    validate_analyst(payload)

    def test_too_many_gaps_are_rejected(self):
        payload = load("analyst_gap.json")
        payload["gaps"] = payload["gaps"] * 6

        with self.assertRaises(PromptContractError):
            validate_analyst(payload)


class TestWriterContract(unittest.TestCase):
    def test_valid_draft(self):
        draft = validate_writer(load("writer.json"))

        self.assertEqual(len(draft.sections), 3)
        self.assertGreater(len(draft.summary), 20)
        self.assertEqual(set(draft.all_evidence_ids()), {1, 2})
        self.assertIn("生产", draft.limitations)

    def test_single_section_is_rejected(self):
        payload = load("writer.json")
        payload["sections"] = payload["sections"][:1]

        with self.assertRaises(PromptContractError):
            validate_writer(payload)

    def test_paragraph_without_evidence_is_rejected(self):
        payload = load("writer.json")
        payload["sections"][0]["paragraphs"][0]["evidence_ids"] = []

        with self.assertRaises(PromptContractError):
            validate_writer(payload)

    def test_missing_heading_is_rejected(self):
        payload = load("writer.json")
        del payload["sections"][0]["heading"]

        with self.assertRaises(PromptContractError):
            validate_writer(payload)


class TestCriticContract(unittest.TestCase):
    def test_pass_payload(self):
        result = validate_critic(load("critic_pass.json"))

        self.assertTrue(result.passed)
        self.assertEqual(result.score, 9)

    def test_revise_payload(self):
        result = validate_critic(load("critic_revise.json"))

        self.assertFalse(result.passed)
        self.assertEqual(len(result.issues), 2)

    def test_contradictory_verdict_is_not_pass(self):
        """模型自称 pass 但分数不够时，按不通过处理（不许被一句话骗过）。"""

        result = validate_critic({"score": PASS_SCORE - 1, "verdict": "pass"})

        self.assertFalse(result.passed)

    def test_invalid_verdict_is_rejected(self):
        with self.assertRaises(PromptContractError):
            validate_critic({"score": 9, "verdict": "ok"})

    def test_score_out_of_range_is_rejected(self):
        for bad in (-1, 11, True, "9"):
            with self.subTest(score=bad):
                with self.assertRaises(PromptContractError):
                    validate_critic({"score": bad, "verdict": "pass"})


class TestPromptBuilding(unittest.TestCase):
    def test_planner_prompt_carries_question_and_json_shape(self):
        messages = planner_messages("PagedAttention 显存管理")

        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("JSON", messages[0]["content"])
        self.assertIn("PagedAttention", messages[1]["content"])
        self.assertIn("sub_queries", messages[1]["content"])
        self.assertIn("不执行", messages[0]["content"])

    def test_critic_prompt_includes_rubric(self):
        messages = critic_messages("Q", draft={"a": 1}, evidence=[])

        self.assertIn("rubric", messages[1]["content"])
        self.assertIn("8 分及以上", messages[1]["content"])


class TestEvidenceDigest(unittest.TestCase):
    def test_excerpts_are_truncated_and_ids_kept(self):
        records = [
            {"id": 7, "title": "T", "url": "https://e.org/x"},
            {"id": 9, "title": "", "url": "https://e.org/y"},
        ]
        texts = {7: "正文" * 1000, 9: "短正文"}

        digest = build_evidence_digest(records, texts, per_item_chars=50)

        self.assertEqual([item["id"] for item in digest], [7, 9])
        self.assertLessEqual(len(digest[0]["excerpt"]), 50)
        self.assertEqual(digest[1]["excerpt"], "短正文")
        self.assertEqual(digest[0]["url"], "https://e.org/x")

    def test_missing_text_becomes_empty_excerpt(self):
        digest = build_evidence_digest(
            [{"id": 1, "title": "T", "url": "https://e.org"}], {}
        )

        self.assertEqual(digest[0]["excerpt"], "")


if __name__ == "__main__":
    unittest.main()
