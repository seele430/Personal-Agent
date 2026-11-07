"""记忆评测 CLI 测试（免费档 + 付费档的"未确认不调用"）。"""

import json
import tempfile
import unittest

from pathlib import Path

import scripts.run_memory_eval as runner


class MemoryEvalCliTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / "out"
        self.db = Path(self.temp.name) / "knowledge.db"

    def run_cli(self, *extra):
        return runner.main(
            ["--out", str(self.out), "--db", str(self.db), *extra]
        )

    def test_free_mode_passes_and_writes_reports(self):
        code = self.run_cli()

        self.assertEqual(code, 0)

        files = sorted(path.name for path in self.out.iterdir())

        self.assertEqual(len(files), 2)
        self.assertTrue(any(name.endswith(".md") for name in files))
        self.assertTrue(any(name.endswith(".json") for name in files))

    def test_report_contains_all_thresholds(self):
        self.assertEqual(self.run_cli(), 0)

        markdown = next(self.out.glob("*.md")).read_text(encoding="utf-8")

        self.assertIn("Recall@5", markdown)
        self.assertIn("冲突采用当前事实", markdown)
        self.assertIn("删除后复活", markdown)
        self.assertIn("版本回滚", markdown)
        self.assertIn("PASS", markdown)

    def test_json_has_counts_and_results(self):
        self.assertEqual(self.run_cli(), 0)

        payload = json.loads(next(self.out.glob("*.json")).read_text(encoding="utf-8"))

        self.assertEqual(payload["mode"], "free")
        self.assertEqual(payload["recall"]["total"], 50)
        self.assertEqual(payload["conflict"]["correct"], 10)
        self.assertEqual(len(payload["results"]), 60)
        self.assertTrue(payload["passed"])

    def test_paid_mode_without_confirm_never_calls_model(self):
        code = self.run_cli("--with-answers")

        self.assertEqual(code, 2)
        # 未确认时连报告都不写：这里主要证明"没花一分钱就退出"
        self.assertFalse(self.out.exists() and any(self.out.iterdir()))

    def test_paid_mode_estimates_cost_in_tokens(self):
        estimate = runner.estimate_cost(60)

        self.assertEqual(estimate["model_calls"], 60)
        self.assertGreater(estimate["total_tokens"], 0)
        self.assertEqual(
            estimate["total_tokens"],
            estimate["prompt_tokens"] + estimate["completion_tokens"],
        )


class PaidPathWithFakeAnswererTest(unittest.TestCase):
    """用替身把付费档的**代码路径**跑通，一分钱不花。

    真跑 60 条要用户确认；但"确认之后会不会崩、判据会不会误杀"
    不该等到花钱那一刻才知道。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name) / "out"
        self.db = Path(self.temp.name) / "knowledge.db"

    def run_paid(self, answerer):
        from unittest.mock import patch

        with patch.object(runner, "build_answerer", return_value=answerer):
            return runner.main(
                [
                    "--out",
                    str(self.out),
                    "--db",
                    str(self.db),
                    "--with-answers",
                    "--confirm",
                ]
            )

    def test_paid_path_passes_with_cited_answers(self):
        def answerer(context, question):
            usage = {"prompt_tokens": 10, "completion_tokens": 5}

            if not context:
                return {
                    "answer": "没有依据",
                    "citations": [],
                    "insufficient": True,
                    "_usage": usage,
                }

            return {
                "answer": "有依据的回答",
                "citations": [context[0]["id"]],
                "insufficient": False,
                "_usage": usage,
            }

        code = self.run_paid(answerer)

        self.assertEqual(code, 0)

        payload = json.loads(
            next(self.out.glob("*.json")).read_text(encoding="utf-8")
        )

        self.assertEqual(payload["mode"], "free+answers")
        self.assertEqual(payload["answers"]["total"], 60)
        self.assertEqual(payload["answers"]["unsupported"], 0)
        self.assertEqual(payload["llm_usage"]["prompt_tokens"], 600)

    def test_paid_path_fails_when_answers_have_no_citation(self):
        def answerer(context, question):
            return {"answer": "我记得是这样", "citations": [], "insufficient": False}

        code = self.run_paid(answerer)

        self.assertEqual(code, 3)

        payload = json.loads(
            next(self.out.glob("*.json")).read_text(encoding="utf-8")
        )

        self.assertEqual(payload["answers"]["unsupported"], 60)
        self.assertTrue(any("无依据回答" in item for item in payload["problems"]))


if __name__ == "__main__":
    unittest.main()
