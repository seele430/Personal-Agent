"""`/api/qq/onebot` 端点测试：入队 + 队列选择 + 204 空正文。"""

import hashlib
import hmac
import json
import unittest

from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient


SECRET = "s3cret"
OWNER = 2790017495


class EndpointCase(unittest.TestCase):
    def setUp(self):
        from src.main import app
        from src.qq import api

        api.reset_inbound()

        self.client = TestClient(app)
        self.env = patch.dict(
            "os.environ",
            {
                "PA_QQ_ENABLED": "1",
                "PA_QQ_OWNER": str(OWNER),
                "PA_QQ_SECRET": SECRET,
                "PA_ONEBOT_URL": "http://127.0.0.1:1",
                "PA_ONEBOT_TOKEN": "tok",
                "PA_QQ_AUDIT": "0",
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(api.reset_inbound)

        self.outbox = Mock()
        self.outbox.enqueue.return_value = True
        self.workers = (self.outbox, Mock(), Mock())

    def post(self, event, *, sign=True):
        body = json.dumps(event, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}

        if sign:
            headers["X-Signature"] = "sha1=" + hmac.new(
                SECRET.encode(), body, hashlib.sha1
            ).hexdigest()

        return self.client.post("/api/qq/onebot", content=body, headers=headers)

    def event(self, message="/status", message_id=1):
        return {
            "post_type": "message",
            "message_type": "private",
            "message_id": message_id,
            "sender": {"user_id": OWNER},
            "user_id": OWNER,
            "message": message,
        }

    def test_bad_signature_is_401(self):
        self.assertEqual(self.post(self.event(), sign=False).status_code, 401)

    def test_task_message_enqueued(self):
        with patch("src.qq.api.configured_workers", return_value=self.workers):
            response = self.post(self.event(message="1+1 等于几"))

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        kwargs = self.outbox.enqueue.call_args.kwargs
        self.assertEqual(kwargs["queue"], "task")
        self.assertEqual(kwargs["raw_text"], "1+1 等于几")

    def test_session_command_not_written_to_messages(self):
        """/new、/use 是命令不是对话，落进消息表会被劈成两条孤立记录。"""

        with patch("src.qq.api.configured_workers", return_value=self.workers), \
                patch("src.sessions.SessionStore") as store_class:
            self.post(self.event(message="/new"))

        store_class.return_value.add_message.assert_not_called()

    def test_conversation_message_still_recorded(self):
        with patch("src.qq.api.configured_workers", return_value=self.workers), \
                patch("src.sessions.SessionStore") as store_class:
            self.post(self.event(message="/ask 你好"))

        store_class.return_value.add_message.assert_called_once()

    def test_confirm_goes_to_control_queue(self):
        with patch("src.qq.api.configured_workers", return_value=self.workers):
            self.post(self.event(message="/confirm K7M2P9QX"))

        self.assertEqual(self.outbox.enqueue.call_args.kwargs["queue"], "control")

    def test_cancel_goes_to_control_queue(self):
        with patch("src.qq.api.configured_workers", return_value=self.workers):
            self.post(self.event(message="/cancel 1"))

        self.assertEqual(self.outbox.enqueue.call_args.kwargs["queue"], "control")

    def test_non_owner_ignored(self):
        event = self.event()
        event["sender"] = {"user_id": 123456}
        event["user_id"] = 123456

        with patch("src.qq.api.configured_workers", return_value=self.workers):
            response = self.post(event)

        self.assertEqual(response.status_code, 204)
        self.outbox.enqueue.assert_not_called()

    def test_group_ignored(self):
        event = self.event()
        event["message_type"] = "group"

        response = self.post(event)

        self.assertEqual(response.status_code, 204)

    def test_disabled_returns_204(self):
        with patch.dict("os.environ", {"PA_QQ_ENABLED": "0"}, clear=False):
            from src.qq import api

            api.reset_inbound()
            response = self.post(self.event())

        self.assertEqual(response.status_code, 204)

    def test_duplicate_enqueue_ignored(self):
        self.outbox.enqueue.return_value = False

        with patch("src.qq.api.configured_workers", return_value=self.workers):
            response = self.post(self.event())

        self.assertEqual(response.status_code, 204)


if __name__ == "__main__":
    unittest.main()
