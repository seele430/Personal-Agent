"""60 条记忆评测测试：夹具校验、免费指标、付费判定（用替身）。"""

import json
import tempfile
import unittest

from pathlib import Path

from src.memory.evaluation import (
    DEFAULT_DATASET,
    EvalError,
    MemoryEvaluator,
    load_dataset,
)
from src.memory.store import MemoryStore


class EvalTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = load_dataset()

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.store = MemoryStore(Path(self._temporary.name) / "knowledge.db")
        self.evaluator = MemoryEvaluator(self.store, self.dataset)

    def run_eval(self):
        self.evaluator.seed()

        return self.evaluator.run()


class TestDataset(EvalTestCase):
    def test_shape_and_ratios(self):
        self.assertEqual(len(self.dataset["samples"]), 60)
        self.assertEqual(len(self.dataset["items"]), 65)

        counts = {}

        for sample in self.dataset["samples"]:
            counts[sample["category"]] = counts.get(sample["category"], 0) + 1

        self.assertEqual(
            counts,
            {
                "preference": 20,
                "project_fact": 20,
                "conflict": 10,
                "deletion": 5,
                "unanswerable": 5,
            },
        )

    def test_wrong_sample_count_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            broken = dict(self.dataset)
            broken["samples"] = broken["samples"][:59]
            path = Path(folder) / "broken.json"
            path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")

            with self.assertRaises(EvalError):
                load_dataset(path)

    def test_missing_file_is_rejected(self):
        with self.assertRaises(EvalError):
            load_dataset(Path("definitely-not-here.json"))

    def test_unknown_category_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            broken = dict(self.dataset)
            broken["samples"] = [dict(item) for item in broken["samples"]]
            broken["samples"][0]["category"] = "vibes"
            path = Path(folder) / "broken.json"
            path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")

            with self.assertRaises(EvalError):
                load_dataset(path)


class TestFreeMetrics(EvalTestCase):
    def test_seed_creates_all_items(self):
        ids = self.evaluator.seed()

        self.assertEqual(len(ids), 65)
        self.assertEqual(self.store.count_items(self.dataset["owner_id"]), 65)

    def test_recall_conflict_and_cleanup(self):
        report = self.run_eval()

        self.assertEqual(report.recall_total, 50)
        self.assertGreaterEqual(report.recall_hit, 45)
        self.assertEqual(report.conflict_total, 10)
        self.assertEqual(report.conflict_correct, 10)
        self.assertEqual(report.resurrection, 0)
        self.assertEqual(report.false_hits, 0)
        self.assertTrue(report.version_rollback_ok)
        self.assertTrue(report.passed, report.problems)

    def test_deletion_round_ends_with_version_rollback(self):
        self.evaluator.seed()
        report = self.evaluator.run()

        self.assertEqual(report.deletion_total, 5)
        self.assertEqual(report.resurrection, 0)

        # run() 最后一步是做版本回滚（C6），所以被删的 5 条又回来了。
        # 这不是"删除失败"：删除效果由 resurrection 指标和 store 的单测覆盖。
        self.assertTrue(report.version_rollback_ok)
        self.assertEqual(
            self.store.count_items(self.dataset["owner_id"]), 65
        )

    def test_report_serializes_to_json(self):
        payload = self.run_eval().to_dict()

        self.assertIn("recall", payload)
        self.assertEqual(payload["recall"]["total"], 50)
        self.assertEqual(payload["conflict"]["target"], 10)
        self.assertTrue(payload["passed"])
        self.assertEqual(len(payload["results"]), 60)

    def test_version_rollback_restores_deleted_items(self):
        self.evaluator.seed()
        report = self.evaluator.run()

        self.assertTrue(report.version_rollback_ok)

        # 回滚发生在 run() 末尾，所以删除的样本条目应已回来
        self.assertEqual(
            self.store.count_items(self.dataset["owner_id"]), 65
        )


class TestAnswerCheck(EvalTestCase):
    def setUp(self):
        super().setUp()
        self.evaluator.seed()

    def test_supported_answer(self):
        def answerer(context, question):
            return {
                "answer": "有依据的回答",
                "citations": [context[0]["id"]] if context else [],
                "insufficient": False,
            }

        answers = self.evaluator.run_answer_check(answerer)

        self.assertEqual(len(answers), 60)
        self.assertTrue(any(item.supported for item in answers))

    def test_answer_without_citation_is_unsupported(self):
        def answerer(context, question):
            return {"answer": "我记得是这样", "citations": [], "insufficient": False}

        answers = self.evaluator.run_answer_check(answerer)

        self.assertTrue(any("无引用" in item.detail for item in answers))

    def test_citation_outside_context_is_unsupported(self):
        def answerer(context, question):
            return {
                "answer": "引用了不存在的记忆",
                "citations": [999999],
                "insufficient": False,
            }

        answers = self.evaluator.run_answer_check(answerer)

        self.assertTrue(
            any("不存在的记忆" in item.detail for item in answers)
        )

    def test_insufficient_is_acceptable(self):
        def answerer(context, question):
            return {"answer": "没有依据", "citations": [], "insufficient": True}

        answers = self.evaluator.run_answer_check(answerer)

        self.assertTrue(all(item.supported for item in answers))

    def test_answerer_exception_is_recorded_not_raised(self):
        def answerer(context, question):
            raise RuntimeError("模型挂了")

        answers = self.evaluator.run_answer_check(answerer)

        self.assertEqual(len(answers), 60)
        self.assertTrue(all("作答失败" in item.detail for item in answers))

    def test_attach_answers_adds_problem_when_too_many_unsupported(self):
        report = self.run_eval()

        def answerer(context, question):
            return {"answer": "无引用", "citations": [], "insufficient": False}

        answers = self.evaluator.run_answer_check(answerer)
        merged = self.evaluator.attach_answers(report, answers)

        self.assertEqual(merged.unsupported, 60)
        self.assertFalse(merged.passed)
        self.assertTrue(any("无依据回答" in item for item in merged.problems))

    def test_string_citations_are_accepted(self):
        """真实模型会写 "citations": ["2"] —— 数字字符串是合法引用。"""

        def answerer(context, question):
            if not context:
                return {"answer": "无", "citations": [], "insufficient": True}

            return {
                "answer": "有依据",
                "citations": [str(context[0]["id"])],
                "insufficient": False,
            }

        answers = self.evaluator.run_answer_check(answerer)

        self.assertTrue(all(item.supported for item in answers))

    def test_non_numeric_and_out_of_range_citations_are_rejected(self):
        def answerer(context, question):
            return {
                "answer": "瞎引",
                "citations": ["abc", True, 0, 999999, "999999"],
                "insufficient": False,
            }

        answers = self.evaluator.run_answer_check(answerer)

        self.assertTrue(all(not item.supported for item in answers))

    def test_citation_coercion_deduplicates(self):
        from src.memory.evaluation import _coerce_citation_ids

        self.assertEqual(_coerce_citation_ids(["2", 2, "3"]), (2, 3))
        self.assertEqual(_coerce_citation_ids("2"), ())
        self.assertEqual(_coerce_citation_ids(["-1", "0", "x"]), ())


if __name__ == "__main__":
    unittest.main()
