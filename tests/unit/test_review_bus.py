"""M1 review bus tests。"""

import json
import tempfile
import unittest

from pathlib import Path

from src.collab import review_bus


class ReviewBusCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        (self.root / "kimi" / "outbox").mkdir(parents=True)
        (self.root / "state").mkdir(parents=True)
        self.set_state(round=1, status="waiting_kimi")

    def set_state(self, **overrides):
        state = {
            "round": 1,
            "status": "waiting_kimi",
            "history": [],
            **overrides,
        }
        (self.root / "state" / "review.json").write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )

    def result(self, payload):
        path = self.root / "kimi" / "outbox" / "round-001-result.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def valid(self, verdict="pass", findings=None):
        return {
            "round": 1,
            "verdict": verdict,
            "findings": findings or [],
        }

    def test_pass_advances_and_archives(self):
        self.result(self.valid())
        result = review_bus.process_once(self.root)
        self.assertEqual(result["action"], "processed")
        state = review_bus.load_state(self.root)
        self.assertEqual(state["status"], "pass_waiting_human_signoff")
        self.assertTrue((self.root / "archive" / "round-001" / "round-001-result.json").is_file())
        self.assertEqual(len(state["processed_results"]), 1)

    def test_findings_advance_to_fixing(self):
        finding = {
            "severity": "high", "file": "a.py", "line": 1,
            "issue": "x", "evidence": "y", "suggestion": "z",
        }
        self.result(self.valid("findings", [finding]))
        result = review_bus.process_once(self.root)
        self.assertEqual(result["action"], "processed")
        self.assertEqual(review_bus.load_state(self.root)["status"], "fixing")

    def test_duplicate_ignored(self):
        self.result(self.valid())
        review_bus.process_once(self.root)
        state = review_bus.load_state(self.root)
        state["status"] = "waiting_kimi"
        review_bus.save_state_atomic(self.root, state)
        result = review_bus.process_once(self.root)
        self.assertEqual(result["action"], "duplicate")

    def test_round_mismatch_rejected(self):
        self.result({"round": 2, "verdict": "pass", "findings": []})
        result = review_bus.process_once(self.root)
        self.assertEqual(result["action"], "review_failed")
        self.assertIn("round_mismatch", result["errors"][0])

    def test_invalid_json_rejected(self):
        (self.root / "kimi" / "outbox" / "round-001-result.json").write_text(
            "{not json", encoding="utf-8"
        )
        result = review_bus.process_once(self.root)
        self.assertEqual(result["action"], "review_failed")
        self.assertIn("invalid_json", result["errors"][0])

    def test_pass_with_findings_rejected(self):
        finding = {
            "severity": "high", "file": "a.py", "line": 1,
            "issue": "x", "evidence": "y",
        }
        self.result(self.valid("pass", [finding]))
        result = review_bus.process_once(self.root)
        self.assertEqual(result["action"], "review_failed")
        self.assertIn("pass_with_findings", result["errors"][0])

    def test_missing_evidence_rejected(self):
        finding = {"severity": "high", "file": "a.py", "line": 1, "issue": "x"}
        self.result(self.valid("findings", [finding]))
        result = review_bus.process_once(self.root)
        self.assertEqual(result["action"], "review_failed")
        self.assertIn("missing_evidence", result["errors"][0])

    def test_too_large_rejected(self):
        path = self.root / "kimi" / "outbox" / "round-001-result.json"
        path.write_bytes(b"{" + b" " * (review_bus.MAX_RESULT_BYTES + 1) + b"}")
        result = review_bus.process_once(self.root)
        self.assertEqual(result["action"], "review_failed")
        self.assertIn("result_too_large", result["errors"][0])

    def test_no_result(self):
        result = review_bus.process_once(self.root)
        self.assertEqual(result["action"], "no_result")

    def test_state_survives_atomic_write(self):
        self.result(self.valid("findings", []))
        review_bus.process_once(self.root)
        raw = (self.root / "state" / "review.json").read_text(encoding="utf-8")
        self.assertIsInstance(json.loads(raw), dict)



    def test_signed_off_not_reprocessed(self):
        self.result(self.valid())
        self.set_state(round=1, status="signed_off")
        result = review_bus.process_once(self.root)
        self.assertEqual(result["action"], "not_waiting")
        self.assertEqual(review_bus.load_state(self.root)["status"], "signed_off")


if __name__ == "__main__":
    unittest.main()
