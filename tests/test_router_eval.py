"""路由评估集的冻结校验与质量门槛。"""

from __future__ import annotations

import hashlib
import json
import unittest

from pathlib import Path

from src.routing import Router
from src.routing.policy import approval_gate

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "tests" / "data" / "router-eval.jsonl"
MANIFEST = ROOT / "tests" / "data" / "router-eval.manifest.json"


def load_rows() -> list[dict]:
    return [
        json.loads(line)
        for line in DATA.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class RouterEvalTest(unittest.TestCase):
    def setUp(self):
        self.rows = load_rows()
        self.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.router = Router()

    def test_manifest_counts_match_data(self):
        counts = {"train": 0, "dev": 0, "test": 0}

        for row in self.rows:
            counts[row["split"]] += 1

        self.assertEqual(counts, self.manifest["counts"])
        self.assertEqual(len(self.rows), self.manifest["total"])

    def test_test_split_hash_is_frozen(self):
        texts = sorted(
            row["text"] for row in self.rows if row["split"] == "test"
        )
        digest = hashlib.sha256("\n".join(texts).encode("utf-8")).hexdigest()

        self.assertEqual(digest, self.manifest["test_sha256"])

    def test_test_split_accuracy(self):
        rows = [row for row in self.rows if row["split"] == "test"]
        correct = 0
        high_risk_missed = 0

        for row in rows:
            decision = self.router.decide(row["text"])
            predicted = (
                decision.intent
                if decision.decision == "route"
                else "__clarify__"
            )

            if predicted == row["intent"]:
                correct += 1

            if row["risk"] == "high" and approval_gate(decision).allowed:
                high_risk_missed += 1

        accuracy = correct / len(rows)

        self.assertGreaterEqual(
            accuracy,
            0.90,
            f"test accuracy={accuracy:.3f} ({correct}/{len(rows)})",
        )
        self.assertEqual(high_risk_missed, 0)


if __name__ == "__main__":
    unittest.main()
