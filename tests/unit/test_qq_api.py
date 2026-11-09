"""`/api/qq/onebot` 端点测试：入队 + 固定 204 空正文。"""

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
        self.fake_worker = SimpleNamespace(outbox=self.outbox)

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
        response = self.post(self.event(), sign=False)

        self.assertEqual(response.status_code, 401)

    def test_handled_event_enqueues_and_returns_204(self):
        with patch("src.qq.api.configured_worker", return_value=self.fake_worker):
            response = self.post(self.event(message="/ask 1+1"))

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        self.outbox.enqueue.assert_called_once()
        kwargs = self.outbox.enqueue.call_args.kwargs
        self.assertEqual(kwargs["command"], "ask")
        self.assertEqual(kwargs["question"], "1+1")

    def test_duplicate_enqueue_is_recorded(self):
        self.outbox.enqueue.return_value = False

        with patch("src.qq.api.configured_worker", return_value=self.fake_worker):
            response = self.post(self.event())

        self.assertEqual(response.status_code, 204)

    def test_disabled_returns_204(self):
        with patch.dict("os.environ", {"PA_QQ_ENABLED": "0"}, clear=False):
            from src.qq import api

            api.reset_inbound()
            response = self.post(self.event())

        self.assertEqual(response.status_code, 204)

    def test_non_owner_returns_204_without_enqueue(self):
        event = self.event()
        event["sender"] = {"user_id": 123456}
        event["user_id"] = 123456

        with patch("src.qq.api.configured_worker", return_value=self.fake_worker):
            response = self.post(event)

        self.assertEqual(response.status_code, 204)
        self.outbox.enqueue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
