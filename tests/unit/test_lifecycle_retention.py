"""生命周期测试：默认预演、keep_last 保护、确认才删。"""

import sqlite3
import tempfile
import unittest

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.lifecycle.retention import (
    RetentionError,
    RetentionPolicy,
    apply,
    plan,
    plan_table,
)


NOW = datetime(2026, 11, 7, tzinfo=timezone.utc)


def old(days_ago):
    return (NOW - timedelta(days=days_ago)).isoformat()


class RetentionCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.db = Path(self._temporary.name) / "traces.db"
        self.make_spans()

    def make_spans(self):
        connection = sqlite3.connect(self.db)

        with connection:
            connection.execute(
                "CREATE TABLE spans (span_id TEXT, created_at TEXT)"
            )

            rows = [
                ("old-1", old(100)),
                ("old-2", old(90)),
                ("old-3", old(80)),
                ("new-1", old(5)),
                ("new-2", old(1)),
            ]

            connection.executemany(
                "INSERT INTO spans VALUES (?,?)", rows
            )

        connection.close()

    def count(self):
        connection = sqlite3.connect(self.db)

        try:
            return connection.execute("SELECT COUNT(*) FROM spans").fetchone()[0]

        finally:
            connection.close()


class TestPolicy(RetentionCase):
    def test_defaults(self):
        policy = RetentionPolicy()

        self.assertEqual(policy.days, 30)
        self.assertEqual(policy.keep_last, 50)

    def test_invalid_days(self):
        for bad in (0, -1, True, "30"):
            with self.subTest(value=bad):
                with self.assertRaises(RetentionError):
                    RetentionPolicy(days=bad)

    def test_invalid_keep_last(self):
        with self.assertRaises(RetentionError):
            RetentionPolicy(keep_last=-1)


class TestPlan(RetentionCase):
    def test_candidates_are_old_rows(self):
        target = plan_table(
            self.db, "spans", policy=RetentionPolicy(days=30, keep_last=0), now=NOW
        )

        self.assertEqual(target.count, 3)
        self.assertEqual(self.count(), 5)

    def test_keep_last_protects_recent(self):
        target = plan_table(
            self.db, "spans", policy=RetentionPolicy(days=30, keep_last=3), now=NOW
        )

        # 3 条超期，keep_last=3 把它们全保护住 → 候选 = 0
        self.assertEqual(target.count, 0)
        self.assertEqual(target.kept_recent, 3)

    def test_dry_run_does_not_delete(self):
        plan_table(self.db, "spans", policy=RetentionPolicy(days=30, keep_last=0), now=NOW)

        self.assertEqual(self.count(), 5)

    def test_unknown_table_rejected(self):
        with self.assertRaises(RetentionError):
            plan_table(self.db, "users", policy=RetentionPolicy(), now=NOW)

    def test_missing_db_rejected(self):
        with self.assertRaises(RetentionError):
            plan_table(
                Path(self._temporary.name) / "nope.db", "spans",
                policy=RetentionPolicy(), now=NOW,
            )

    def test_serializes(self):
        payload = plan_table(
            self.db, "spans", policy=RetentionPolicy(days=30, keep_last=0), now=NOW
        ).to_dict()

        self.assertEqual(payload["table"], "spans")
        self.assertEqual(payload["count"], 3)
        self.assertIn("cutoff", payload)


class TestApply(RetentionCase):
    def test_requires_confirm(self):
        purge_plan = plan(
            spans_db=self.db, policy=RetentionPolicy(days=30, keep_last=0), now=NOW
        )

        with self.assertRaises(RetentionError):
            apply(purge_plan, spans_db=self.db)

        self.assertEqual(self.count(), 5)

    def test_apply_deletes_candidates(self):
        purge_plan = plan(
            spans_db=self.db, policy=RetentionPolicy(days=30, keep_last=0), now=NOW
        )
        result = apply(purge_plan, spans_db=self.db, confirm=True)

        self.assertEqual(result.deleted, 3)
        self.assertEqual(self.count(), 2)

    def test_apply_is_idempotent(self):
        purge_plan = plan(
            spans_db=self.db, policy=RetentionPolicy(days=30, keep_last=0), now=NOW
        )
        apply(purge_plan, spans_db=self.db, confirm=True)
        again = apply(purge_plan, spans_db=self.db, confirm=True)

        # 候选已删，第二次不会多删
        self.assertEqual(self.count(), 2)
        self.assertEqual(again.deleted, 0)

    def test_plan_for_missing_store_is_empty(self):
        empty = plan(policy=RetentionPolicy(), now=NOW)

        self.assertEqual(empty.total, 0)


if __name__ == "__main__":
    unittest.main()
