"""出站队列测试：状态机、原子认领、确认消费、取消、迁移。"""

import sqlite3
import tempfile
import unittest

from pathlib import Path

from src.qq.outbox import QQOutbox


class OutboxCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.path = Path(self._temporary.name) / "outbox.db"
        self.outbox = QQOutbox(self.path)

    def enqueue(self, message_id="1", sender=7, text="/status", queue="task"):
        return self.outbox.enqueue(
            message_id=message_id, sender=sender, raw_text=text, queue=queue
        )

    def to_pending(self, message_id="1", intent="status", state="pending_task"):
        self.outbox.set_routing(
            message_id,
            intent=intent,
            params={},
            params_hash="h",
            decision_source="rule",
            state=state,
        )

    # ---------- 入队/幂等 ----------

    def test_enqueue_and_duplicate(self):
        self.assertTrue(self.enqueue())
        self.assertFalse(self.enqueue())
        self.assertEqual(self.outbox.counts()["pending_task"], 1)

    def test_new_rows_have_explicit_zero_flags(self):
        self.enqueue()

        row = self.outbox.get("1")
        self.assertEqual(row["confirm_used"], 0)
        self.assertEqual(row["cancelled"], 0)

    def test_queue_selection(self):
        self.enqueue(queue="control")

        self.assertEqual(self.outbox.get("1")["queue"], "control")

    # ---------- 认领 ----------

    def test_claim_task(self):
        self.enqueue()
        self.to_pending()

        job = self.outbox.claim("task")

        self.assertEqual(job["state"], "running")
        self.assertIsNone(self.outbox.claim("task"))

    def test_claim_control(self):
        self.enqueue(queue="control")
        self.to_pending(state="pending_control")

        job = self.outbox.claim("control")

        self.assertEqual(job["state"], "running_control")

    # ---------- 发送认领 ----------

    def test_claim_send_requires_running_and_not_cancelled(self):
        self.enqueue()
        self.to_pending()
        self.outbox.claim("task")

        self.assertTrue(self.outbox.claim_send("1"))

    def test_claim_send_fails_when_cancelled(self):
        self.enqueue()
        self.to_pending(intent="ask")
        self.outbox.claim("task")
        self.outbox.cancel(sender=7, target="1")

        self.assertFalse(self.outbox.claim_send("1"))

    # ---------- 确认 ----------

    def test_confirm_consumes_once(self):
        self.enqueue()
        self.to_pending(intent="read_file")
        self.outbox.set_awaiting_confirm("1", confirm_id="K7M2P9QX", expires_at=9e9)

        first = self.outbox.consume_confirm(sender=7, confirm_id="K7M2P9QX")
        second = self.outbox.consume_confirm(sender=7, confirm_id="K7M2P9QX")

        self.assertEqual(first["state"], "pending_task")
        self.assertIsNone(second)
        self.assertEqual(self.outbox.get("1")["confirm_used"], 1)

    def test_confirm_expired(self):
        self.enqueue()
        self.to_pending(intent="read_file")
        self.outbox.set_awaiting_confirm("1", confirm_id="ABCD2345", expires_at=1.0)

        self.assertIsNone(self.outbox.consume_confirm(sender=7, confirm_id="ABCD2345"))

    def test_confirm_wrong_sender(self):
        self.enqueue()
        self.to_pending(intent="read_file")
        self.outbox.set_awaiting_confirm("1", confirm_id="ABCD2345", expires_at=9e9)

        self.assertIsNone(self.outbox.consume_confirm(sender=8, confirm_id="ABCD2345"))

    # ---------- 取消 ----------

    def test_cancel_pending(self):
        self.enqueue()
        self.to_pending(intent="ask")

        outcome, _ = self.outbox.cancel(sender=7, target="1")

        self.assertEqual(outcome, "cancelled")
        self.assertEqual(self.outbox.get("1")["state"], "cancelled")

    def test_cancel_running_marks_flag_only(self):
        self.enqueue()
        self.to_pending(intent="ask")
        self.outbox.claim("task")

        outcome, _ = self.outbox.cancel(sender=7, target="1")

        self.assertEqual(outcome, "running")
        row = self.outbox.get("1")
        self.assertEqual(row["state"], "running")
        self.assertEqual(row["cancelled"], 1)

    def test_cancel_sending_reports_sending(self):
        self.enqueue()
        self.to_pending()
        self.outbox.claim("task")
        self.outbox.claim_send("1")

        outcome, _ = self.outbox.cancel(sender=7, target="1")

        self.assertEqual(outcome, "sending")

    def test_cancel_missing(self):
        self.assertEqual(self.outbox.cancel(sender=7, target="999")[0], "not_found")

    # ---------- 恢复/迁移 ----------

    def test_recover_inflight(self):
        self.enqueue()
        self.to_pending(intent="ask")
        self.outbox.claim("task")

        self.assertEqual(self.outbox.recover_inflight(), 1)
        self.assertEqual(self.outbox.get("1")["state"], "unknown")

    def test_migration_backfills_queue_and_keeps_legacy_null(self):
        legacy = Path(self._temporary.name) / "legacy.db"
        connection = sqlite3.connect(legacy)
        connection.executescript(
            """
            CREATE TABLE qq_outbox (
                message_id TEXT PRIMARY KEY, sender INTEGER NOT NULL, command TEXT NOT NULL,
                question TEXT, state TEXT NOT NULL, reply TEXT,
                attempts INTEGER NOT NULL DEFAULT 0, error TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            INSERT INTO qq_outbox VALUES ('old-cancel', 7, 'cancel', '', 'pending', NULL, 0, NULL, 1, 1);
            INSERT INTO qq_outbox VALUES ('old-ask', 7, 'ask', '', 'pending', NULL, 0, NULL, 2, 2);
            """
        )
        connection.commit()
        connection.close()

        migrated = QQOutbox(legacy)

        self.assertEqual(migrated.get("old-cancel")["queue"], "control")
        self.assertEqual(migrated.get("old-ask")["queue"], "task")
        self.assertIsNone(migrated.get("old-cancel")["router_version"])



    def test_two_workers_cannot_claim_same_task(self):
        self.enqueue()
        self.to_pending()

        self.assertIsNotNone(self.outbox.claim("task"))
        self.assertIsNone(self.outbox.claim("task"))

    def test_claim_send_twice_only_first_wins(self):
        self.enqueue()
        self.to_pending()
        self.outbox.claim("task")

        self.assertTrue(self.outbox.claim_send("1"))
        self.assertFalse(self.outbox.claim_send("1"))

    def test_cancel_awaiting_confirm_then_confirm_fails(self):
        self.enqueue()
        self.to_pending(intent="read_file")
        self.outbox.set_awaiting_confirm("1", confirm_id="K7M2P9QX", expires_at=9e9)

        self.outbox.cancel(sender=7, target="1")

        self.assertIsNone(self.outbox.consume_confirm(sender=7, confirm_id="K7M2P9QX"))

    def test_legacy_rows_do_not_look_like_new_routes(self):
        self.enqueue()
        self.to_pending()

        self.assertEqual(self.outbox.get("1")["router_version"], "2.0.0-mvp")


if __name__ == "__main__":
    unittest.main()
