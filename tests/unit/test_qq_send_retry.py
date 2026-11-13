"""QQ 发送失败：重试一次 + 保留「未送达」的回答。全 mock，不联网。"""

import os
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from src.qq.inbound import QQInbound
from src.qq.onebot import OneBotError
from src.qq.outbox import QQOutbox
from src.qq.worker import TaskWorker
from src.sessions import SessionStore


class ScriptedClient:
    """按文本设定剩余失败次数，失败次数用完后正常送达。"""

    def __init__(self, fail_on=None):
        self.fail_on = dict(fail_on or {})
        self.calls: list[str] = []
        self.sent: list[str] = []

    def send_private_msg(self, user_id, text):
        self.calls.append(text)
        remaining = int(self.fail_on.get(text) or 0)

        if remaining:
            self.fail_on[text] = remaining - 1

            raise OneBotError("Remote end closed connection without response")

        self.sent.append(text)

        return {"message_id": 1}


class SendRetryCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self._environment = patch.dict(
            os.environ,
            {"PERSONAL_AGENT_DATA_DIR": str(self.root), "PA_QQ_AUDIT": "0"},
            clear=False,
        )
        self._environment.start()
        self.addCleanup(self._environment.stop)
        self._delay = patch("src.qq.worker.SEND_RETRY_DELAY", 0)
        self._delay.start()
        self.addCleanup(self._delay.stop)

        self.outbox = QQOutbox(self.root / "outbox.db")
        self.sessions = SessionStore(self.root / "sessions.db")
        self.session = self.sessions.create("qq")
        self.sessions.set_active(7, self.session["id"])

    def worker(self, client):
        handler = QQInbound(owner_id=7, secret="s", client=client)
        self.handler = handler

        return TaskWorker(outbox=self.outbox, client=client, handler=handler)

    def running_job(self, message_id="m-1"):
        self.outbox.enqueue(
            message_id=message_id, sender=7, raw_text="/ask 问题", queue="task"
        )
        self.outbox.set_routing(
            message_id,
            intent="ask",
            params={"question": "问题"},
            params_hash="h",
            decision_source="command",
            state="running",
        )

        return {"message_id": message_id, "sender": 7, "command": "ask"}

    def actions(self):
        return [record["action"] for record in self.handler.audit]

    def assistant_rows(self):
        return [
            row for row in self.sessions.messages(self.session["id"])
            if row["role"] == "assistant"
        ]

    # ---------- 单条 ----------

    def test_retry_once_then_sent(self):
        client = ScriptedClient(fail_on={"答案": 1})
        job = self.running_job()

        self.assertTrue(self.worker(client).reply(job, "答案"))

        row = self.outbox.get("m-1")
        self.assertEqual(row["state"], "sent")
        self.assertEqual(row["reply"], "答案")
        self.assertEqual(client.sent, ["答案"])
        self.assertEqual(client.calls, ["答案", "答案"])
        self.assertIn("send-retry", self.actions())
        self.assertEqual([row["content"] for row in self.assistant_rows()], ["答案"])

    def test_two_failures_keep_reply_and_mark_undelivered(self):
        client = ScriptedClient(fail_on={"答案": 2})
        job = self.running_job()

        self.assertFalse(self.worker(client).reply(job, "答案"))

        row = self.outbox.get("m-1")
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["reply"], "答案")
        self.assertEqual(client.calls, ["答案", "答案"])
        self.assertIn("send-failed", self.actions())

        contents = [item["content"] for item in self.assistant_rows()]
        self.assertEqual(contents, ["（未送达）答案"])

    def test_retry_can_be_disabled(self):
        client = ScriptedClient(fail_on={"答案": 1})
        job = self.running_job()

        with patch.dict(os.environ, {"PA_QQ_SEND_RETRY": "0"}, clear=False):
            self.assertFalse(self.worker(client).reply(job, "答案"))

        self.assertEqual(client.calls, ["答案"])
        self.assertEqual(self.outbox.get("m-1")["state"], "failed")

    # ---------- 分片 ----------

    def test_chunk_retry_does_not_resend_delivered_chunk(self):
        client = ScriptedClient(fail_on={"第二片": 1})
        job = self.running_job()

        self.assertTrue(self.worker(client).reply_many(job, ["第一片", "第二片"]))

        self.assertEqual(client.calls, ["第一片", "第二片", "第二片"])
        self.assertEqual(client.sent, ["第一片", "第二片"])
        self.assertEqual(self.outbox.get("m-1")["state"], "sent")

    def test_chunk_failure_marks_partial(self):
        client = ScriptedClient(fail_on={"第二片": 2})
        job = self.running_job()

        self.assertFalse(self.worker(client).reply_many(job, ["第一片", "第二片"]))

        row = self.outbox.get("m-1")
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["reply"], "第一片\n第二片")

        contents = [item["content"] for item in self.assistant_rows()]
        self.assertEqual(contents, ["（部分未送达）第一片\n第二片"])

    # ---------- 取消 ----------

    def test_cancelled_row_does_not_retry(self):
        """发送途中被置 cancelled=1：不再重试、也不写「未送达」。

        `cancel()` 对 sending 状态只回状态不置位（发送认领即线性化点），
        所以这里直接改库模拟并发写入者——守的是防御分支。
        """

        job = self.running_job()
        outbox = self.outbox

        class CancellingClient(ScriptedClient):
            def send_private_msg(self, user_id, text):
                self.calls.append(text)

                with outbox._session() as connection:
                    connection.execute(
                        "UPDATE qq_outbox SET cancelled = 1 WHERE message_id = ?",
                        ("m-1",),
                    )

                raise OneBotError("boom")

        client = CancellingClient()

        self.assertFalse(self.worker(client).reply(job, "答案"))
        self.assertEqual(client.calls, ["答案"])
        self.assertEqual(self.outbox.get("m-1")["state"], "cancelled")
        self.assertIn("send-cancelled-during-retry", self.actions())
        self.assertEqual(self.assistant_rows(), [])


if __name__ == "__main__":
    unittest.main()
