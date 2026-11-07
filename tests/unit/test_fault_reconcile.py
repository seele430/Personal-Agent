"""对账测试：产物缺文件 / 孤儿文件 / Outbox 卡死。"""

import sqlite3
import tempfile
import unittest

from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.artifacts.registry import ArtifactRegistry
from src.faults.reconcile import reconcile_artifacts, reconcile_outbox


class ArtifactCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "artifacts"
        self.root.mkdir()
        self.registry = ArtifactRegistry(Path(self.temp.name) / "a.db", root=self.root)

    def add(self, name, content=b"x"):
        path = self.root / name
        path.write_bytes(content)

        return self.registry.register("u1", name)


class TestArtifactReconcile(ArtifactCase):
    def test_clean_state(self):
        self.add("a.txt")

        report = reconcile_artifacts(self.registry)

        self.assertEqual(report.checked, 1)
        self.assertEqual(report.missing, ())
        self.assertEqual(report.orphan_files, ())

    def test_missing_file_is_reported_not_deleted_by_default(self):
        artifact = self.add("gone.txt")
        (self.root / "gone.txt").unlink()

        report = reconcile_artifacts(self.registry)

        self.assertEqual(report.missing, (artifact.id,))
        self.assertEqual(report.purged, 0)
        self.assertIsNotNone(self.registry.get(artifact.id, owner_id="u1"))

    def test_purge_flag_removes_broken_rows(self):
        artifact = self.add("gone.txt")
        (self.root / "gone.txt").unlink()

        report = reconcile_artifacts(self.registry, purge_missing=True)

        self.assertEqual(report.purged, 1)
        self.assertEqual(self.registry.count(), 0)

        with self.assertRaises(KeyError):
            self.registry.get(artifact.id, owner_id="u1")

    def test_orphan_files_reported_not_deleted(self):
        (self.root / "stray.bin").write_bytes(b"z")

        report = reconcile_artifacts(self.registry)

        self.assertIn("stray.bin", report.orphan_files)
        self.assertTrue((self.root / "stray.bin").exists())

    def test_nested_orphans(self):
        (self.root / "sub").mkdir()
        (self.root / "sub" / "stray.txt").write_bytes(b"z")
        self.add("ok.txt")

        report = reconcile_artifacts(self.registry)

        self.assertIn("sub/stray.txt", report.orphan_files)
        self.assertEqual(report.missing, ())

    def test_report_serializes(self):
        self.add("a.txt")

        payload = reconcile_artifacts(self.registry).to_dict()

        self.assertEqual(payload["checked"], 1)
        self.assertEqual(payload["missing"], [])


class OutboxCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "tasks.db"

        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                "CREATE TABLE notification_outbox ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, monitor_id TEXT, "
                "item_id TEXT, message TEXT, status TEXT, attempts INTEGER, "
                "next_attempt_at TEXT, created_at TEXT, sent_at TEXT, owner_id TEXT)"
            )

    def insert(self, status, next_attempt_at=None):
        with closing(sqlite3.connect(self.db)) as connection, connection:
            cursor = connection.execute(
                "INSERT INTO notification_outbox "
                "(monitor_id, item_id, message, status, attempts, next_attempt_at) "
                "VALUES (?,?,?,?,?,?)",
                ("m", "i", "msg", status, 1, next_attempt_at),
            )

            return int(cursor.lastrowid)

    def status(self, row_id):
        with closing(sqlite3.connect(self.db)) as connection:
            return connection.execute(
                "SELECT status FROM notification_outbox WHERE id = ?", (row_id,)
            ).fetchone()[0]


class TestOutboxReconcile(OutboxCase):
    def test_stuck_sending_is_requeued(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        row_id = self.insert("sending", past)

        report = reconcile_outbox(self.db)

        self.assertEqual(report.stuck, (row_id,))
        self.assertEqual(report.requeued, 1)
        self.assertEqual(self.status(row_id), "pending")

    def test_fresh_sending_is_left_alone(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        row_id = self.insert("sending", future)

        report = reconcile_outbox(self.db)

        self.assertEqual(report.requeued, 0)
        self.assertEqual(self.status(row_id), "sending")

    def test_pending_rows_untouched(self):
        row_id = self.insert("pending", None)

        reconcile_outbox(self.db)

        self.assertEqual(self.status(row_id), "pending")

    def test_missing_db_is_noop(self):
        report = reconcile_outbox(Path(self.temp.name) / "nope.db")

        self.assertEqual(report.requeued, 0)

    def test_invalid_arguments(self):
        for bad in (-1, True, "x"):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    reconcile_outbox(self.db, stuck_after_seconds=bad)

    def test_report_serializes(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.insert("sending", past)

        payload = reconcile_outbox(self.db).to_dict()

        self.assertEqual(payload["requeued"], 1)
        self.assertTrue(payload["stuck"])


if __name__ == "__main__":
    unittest.main()
