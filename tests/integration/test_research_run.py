"""端到端脚本测试（离线模式）。

验证：链路能跑通、引用全部命中、两次运行输出逐字节一致、
两条回边在证据 JSON 里看得见。
"""

import json
import tempfile
import unittest

from pathlib import Path

import scripts.research_run as runner

from src.research.report import check_report
from src.web.evidence import EvidenceStore


QUERY = "PagedAttention 如何管理显存"


class ResearchRunOfflineTest(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)

        root = Path(self._temporary.name)
        self.out = root / "out"
        self.evidence_db = root / "evidence.db"
        self.research_db = root / "research.db"

    def run_script(self):
        return runner.main(
            [
                "--query",
                QUERY,
                "--offline",
                "--out",
                str(self.out),
                "--evidence-db",
                str(self.evidence_db),
                "--research-db",
                str(self.research_db),
            ]
        )

    def artifacts(self):
        files = sorted(path.name for path in self.out.iterdir())

        return files

    def test_offline_run_passes(self):
        code = self.run_script()

        self.assertEqual(code, 0)
        self.assertEqual(len(self.artifacts()), 2)
        self.assertTrue(any(name.endswith(".md") for name in self.artifacts()))
        self.assertTrue(any(name.endswith(".json") for name in self.artifacts()))

    def test_report_json_records_the_whole_run(self):
        self.assertEqual(self.run_script(), 0)

        payload = json.loads(
            next(self.out.glob("*.json")).read_text(encoding="utf-8")
        )

        self.assertEqual(payload["mode"], "offline")
        self.assertEqual(payload["state"], "done")
        self.assertEqual(payload["research_rounds"], 2)
        self.assertEqual(payload["revision_count"], 1)
        self.assertEqual(payload["llm_calls"], 7)
        self.assertTrue(payload["report"]["passed"])
        self.assertEqual(payload["report"]["missing_ids"], [])
        self.assertGreaterEqual(payload["report"]["sources"], 2)

        transitions = [event["to"] for event in payload["events"]]

        self.assertEqual(transitions.count("researching"), 2)
        self.assertIn("revising", transitions)
        self.assertEqual(transitions[-1], "done")

    def test_every_citation_hits_the_evidence_store(self):
        self.assertEqual(self.run_script(), 0)

        markdown = next(self.out.glob("*.md")).read_text(encoding="utf-8")
        store = EvidenceStore(self.evidence_db)

        self.assertEqual(check_report(markdown, store), ())
        self.assertIn("## 参考来源", markdown)
        self.assertIn("sha256:", markdown)

    def test_rerun_is_byte_identical(self):
        self.assertEqual(self.run_script(), 0)
        first = next(self.out.glob("*.md")).read_text(encoding="utf-8")

        self.assertEqual(self.run_script(), 0)
        second = next(self.out.glob("*.md")).read_text(encoding="utf-8")

        self.assertEqual(first, second)

    def test_offline_mode_is_the_default(self):
        parser = runner.build_parser()
        args = parser.parse_args(["--query", "x"])

        self.assertFalse(args.live)


if __name__ == "__main__":
    unittest.main()
