"""QQ 出站队列与 worker 测试：SQLite + 全 mock 发送。"""

import tempfile
import unittest

from pathlib import Path

from src.qq.inbound import QQInbound
from src.qq.onebot import OneBotError
from src.qq.outbox import QQOutbox
from src.qq.worker import QQWorker


class FakeClient:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def send_private_msg(self, user_id, text):
        if self.fail:
            raise OneBotError("connect failed")

        self.sent.append((user_id, text))

        return {"message_id": 42}


class OutboxCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.outbox = QQOutbox(Path(self._temporary.name) / "outbox.db")

    def test_enqueue_then_duplicate(self):
        self.assertTrue(self.outbox.enqueue(message_id="1", sender=1, command="status"))
        self.assertFalse(self.outbox.enqueue(message_id="1", sender=1, command="status"))
        self.assertEqual(self.outbox.counts()["pending"], 1)

    def test_claim_moves_to_sending(self):
        self.outbox.enqueue(message_id="1", sender=1, command="status")
        job = self.outbox.claim_next()

        self.assertEqual(job["state"], "sending")
        self.assertIsNone(self.outbox.claim_next())

    def test_mark_sent(self):
        self.outbox.enqueue(message_id="1", sender=1, command="status")
        self.outbox.claim_next()
        self.outbox.mark_sent("1", reply="ok", result_message_id="99")

        row = self.outbox.get("1")
        self.assertEqual(row["state"], "sent")
        self.assertEqual(row["reply"], "ok")

    def test_mark_failed_and_unknown(self):
        self.outbox.enqueue(message_id="1", sender=1, command="status")
        self.outbox.claim_next()
        self.outbox.mark_failed("1", error="boom")
        self.assertEqual(self.outbox.get("1")["state"], "failed")

        self.outbox.enqueue(message_id="2", sender=1, command="status")
        self.outbox.claim_next()
        self.outbox.mark_unknown("2", error="timeout")
        self.assertEqual(self.outbox.get("2")["state"], "unknown")

    def test_recover_inflight_marks_unknown(self):
        self.outbox.enqueue(message_id="1", sender=1, command="status")
        self.outbox.claim_next()

        self.assertEqual(self.outbox.recover_inflight(), 1)
        self.assertEqual(self.outbox.get("1")["state"], "unknown")


class WorkerCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.outbox = QQOutbox(Path(self._temporary.name) / "outbox.db")
        self.handler = QQInbound(
            owner_id=1, secret="s", client=FakeClient(), enabled=True
        )

    def worker(self, client):
        return QQWorker(outbox=self.outbox, client=client, handler=self.handler)

    def test_run_once_sends_and_marks_sent(self):
        client = FakeClient()
        self.outbox.enqueue(message_id="1", sender=2790017495, command="status")

        self.assertTrue(self.worker(client).run_once())

        self.assertEqual(client.sent, [(2790017495, "Personal Agent 在线；QQ 入站已接通。")])
        self.assertEqual(self.outbox.get("1")["state"], "sent")

    def test_send_failure_marks_failed(self):
        self.outbox.enqueue(message_id="1", sender=2790017495, command="status")

        self.worker(FakeClient(fail=True)).run_once()

        self.assertEqual(self.outbox.get("1")["state"], "failed")

    def test_unknown_command_replies_hint(self):
        client = FakeClient()
        self.outbox.enqueue(message_id="1", sender=2790017495, command="unknown")

        self.worker(client).run_once()

        self.assertIn("仅支持", client.sent[0][1])

    def test_empty_queue_returns_false(self):
        self.assertFalse(self.worker(FakeClient()).run_once())


if __name__ == "__main__":
    unittest.main()
