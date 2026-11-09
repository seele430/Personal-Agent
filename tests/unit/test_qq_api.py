"""`/api/qq/onebot` 端点测试：固定 204 空正文，不触发 OneBot 快速回复。"""

import hashlib
import hmac
import json
import unittest

from unittest.mock import patch

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
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(api.reset_inbound)

    def post(self, event, *, sign=True):
        body = json.dumps(event, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}

        if sign:
            headers["X-Signature"] = "sha1=" + hmac.new(
                SECRET.encode(), body, hashlib.sha1
            ).hexdigest()

        return self.client.post("/api/qq/onebot", content=body, headers=headers)

    def event(self):
        return {
            "post_type": "message",
            "message_type": "private",
            "message_id": 1,
            "sender": {"user_id": OWNER},
            "user_id": OWNER,
            "message": "/status",
        }

    def test_bad_signature_is_401(self):
        response = self.post(self.event(), sign=False)

        self.assertEqual(response.status_code, 401)

    def test_handled_event_returns_204_empty(self):
        with patch(
            "src.qq.inbound.QQInbound.handle", return_value=(200, {"reply": "x"})
        ):
            response = self.post(self.event())

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")

    def test_disabled_returns_204(self):
        with patch.dict("os.environ", {"PA_QQ_ENABLED": "0"}, clear=False):
            from src.qq import api

            api.reset_inbound()
            response = self.post(self.event())

        self.assertEqual(response.status_code, 204)

    def test_non_owner_event_returns_204(self):
        with patch(
            "src.qq.inbound.QQInbound.handle", return_value=(204, {})
        ):
            response = self.post(self.event())

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")


if __name__ == "__main__":
    unittest.main()
