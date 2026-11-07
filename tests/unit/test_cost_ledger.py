"""成本账本测试：失败也算钱、"每个被接受产物"的分子分母。"""

import unittest

from src.cost.ledger import (
    CostLedger,
    LedgerError,
    Price,
    summary_from_records,
)


PRICE = Price(input_per_million=2.0, output_per_million=8.0)


class TestRecord(unittest.TestCase):
    def test_entry_math(self):
        ledger = CostLedger(price=PRICE)

        entry = ledger.record("r1", kind="report", tokens_in=1000, tokens_out=500)

        self.assertEqual(entry.total_tokens, 1500)
        self.assertEqual(len(ledger.entries()), 1)

    def test_validation(self):
        ledger = CostLedger()

        for kwargs in (
            {"tokens_in": -1},
            {"tokens_out": -5},
            {"tokens_in": True},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(LedgerError):
                    ledger.record("r1", kind="chat", **kwargs)

        with self.assertRaises(LedgerError):
            ledger.record("", kind="chat")

        with self.assertRaises(LedgerError):
            ledger.record("r1", kind="  ")

    def test_price_validation(self):
        with self.assertRaises(LedgerError):
            Price(input_per_million=-1, output_per_million=1)

        with self.assertRaises(LedgerError):
            Price(input_per_million=True, output_per_million=1)


class TestSummary(unittest.TestCase):
    def test_no_price_reports_usage_only(self):
        ledger = CostLedger()
        ledger.record("r1", kind="report", tokens_in=100, tokens_out=50)

        summary = ledger.summary()

        self.assertIsNone(summary["cost"])
        self.assertIsNone(summary["cost_per_accepted_artifact"])
        self.assertFalse(summary["price_configured"])
        self.assertEqual(summary["total_tokens"], 150)

    def test_cost_math(self):
        ledger = CostLedger(price=PRICE)
        ledger.record("r1", kind="report", tokens_in=1_000_000, tokens_out=1_000_000, accepted=True)

        summary = ledger.summary()

        self.assertAlmostEqual(summary["cost"], 10.0)
        self.assertAlmostEqual(summary["cost_per_accepted_artifact"], 10.0)

    def test_failed_runs_count_toward_cost(self):
        ledger = CostLedger(price=PRICE)
        ledger.record("fail", kind="report", tokens_in=1_000_000, tokens_out=0, accepted=False)
        ledger.record("ok", kind="report", tokens_in=1_000_000, tokens_out=0, accepted=True)

        summary = ledger.summary()

        self.assertEqual(summary["runs"], 2)
        self.assertEqual(summary["accepted_runs"], 1)
        # 分子含失败运行：2 元 / 1 个被接受产物
        self.assertAlmostEqual(summary["cost_per_accepted_artifact"], 4.0)

    def test_zero_accepted(self):
        ledger = CostLedger(price=PRICE)
        ledger.record("r1", kind="report", tokens_in=10, tokens_out=10)

        summary = ledger.summary()

        self.assertIsNone(summary["cost_per_accepted_artifact"])
        self.assertIsNone(summary["tokens_per_accepted_artifact"])

    def test_by_kind_buckets(self):
        ledger = CostLedger()
        ledger.record("a", kind="report", tokens_in=10, accepted=True)
        ledger.record("b", kind="report", tokens_in=20)
        ledger.record("c", kind="snake", tokens_in=5, accepted=True)

        summary = ledger.summary()

        self.assertEqual(summary["by_kind"]["report"]["runs"], 2)
        self.assertEqual(summary["by_kind"]["report"]["tokens"], 30)
        self.assertEqual(summary["by_kind"]["report"]["accepted"], 1)
        self.assertEqual(summary["by_kind"]["snake"]["runs"], 1)


class TestFromRecords(unittest.TestCase):
    def test_bulk_import(self):
        records = [
            {"run_id": "r1", "kind": "report", "tokens_in": 100, "tokens_out": 50, "accepted": True},
            {"id": "r2", "kind": "snake", "tokens_in": 0, "accepted": True},
        ]

        summary = summary_from_records(records, price=PRICE)

        self.assertEqual(summary["runs"], 2)
        self.assertEqual(summary["accepted_runs"], 2)
        self.assertIsNotNone(summary["cost_per_accepted_artifact"])

    def test_bad_records(self):
        with self.assertRaises(LedgerError):
            summary_from_records("nope")

        with self.assertRaises(LedgerError):
            summary_from_records(["nope"])

    def test_missing_fields_default_to_zero(self):
        summary = summary_from_records([{"run_id": "r1"}])

        self.assertEqual(summary["total_tokens"], 0)
        self.assertEqual(summary["runs"], 1)


if __name__ == "__main__":
    unittest.main()
