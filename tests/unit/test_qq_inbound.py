"""QQ 入站与 OneBot 客户端测试：全 mock，不联网、不连 NapCat。"""

import hashlib
import hmac
import io
import json
import unittest
import urllib.error

from unittest.mock import patch

from src.qq.inbound import QQInbound
from src.qq.onebot import OneBotClient, OneBotError


SECRET = "s3cret"
OWNER = 2790017495


class FakeClient:
    def __init__(self):
        self.sent = []
        self.fail = False

    def send_private_msg(self, user_id, text):
        if self.fail:
            raise OneBotError("boom")

        self.sent.append((user_id, text))

        return {"message_id": 1}


def event(**overrides):
    base = {
        "post_type": "message",
        "message_type": "private",
        "message_id": 1001,
        "user_id": OWNER,
        "sender": {"user_id": OWNER},
        "message": "hello",
    }
    base.update(overrides)

    return base


class SignatureCase(unittest.TestCase):
    def handler(self):
        return QQInbound(owner_id=OWNER, secret=SECRET, client=FakeClient())

    def test_valid_signature(self):
        body = b'{"a":1}'
        signature = "sha1=" + hmac.new(SECRET.encode(), body, hashlib.sha1).hexdigest()

        self.assertTrue(self.handler().verify(body, signature))

    def test_bad_signature(self):
        self.assertFalse(self.handler().verify(b"{}", "sha1=deadbeef"))

    def test_missing_secret_rejects_all(self):
        handler = QQInbound(owner_id=OWNER, secret="", client=FakeClient())

        self.assertFalse(handler.verify(b"{}", "sha1=x"))

    def test_sha256_signature_supported(self):
        body = b'{"a":1}'
        signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()

        self.assertTrue(self.handler().verify(body, signature))

    def test_bare_sha256_hex_supported(self):
        body = b'{"a":1}'
        signature = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()

        self.assertTrue(self.handler().verify(body, signature))


class WhitelistCase(unittest.TestCase):
    def test_other_sender_is_dropped_without_reply(self):
        client = FakeClient()
        handler = QQInbound(owner_id=OWNER, secret=SECRET, client=client)

        status, payload = handler.handle(
            event(sender={"user_id": 123456}, user_id=123456)
        )

        self.assertEqual(status, 204)
        self.assertEqual(payload, {})
        self.assertEqual(client.sent, [])
        self.assertEqual(handler.audit[-1]["action"], "dropped:not-owner")

    def test_group_message_ignored(self):
        client = FakeClient()
        handler = QQInbound(owner_id=OWNER, secret=SECRET, client=client)

        status, _ = handler.handle(event(message_type="group"))

        self.assertEqual(status, 204)
        self.assertEqual(client.sent, [])

    def test_non_message_event_ignored(self):
        client = FakeClient()
        handler = QQInbound(owner_id=OWNER, secret=SECRET, client=client)

        status, _ = handler.handle({"post_type": "notice"})

        self.assertEqual(status, 204)
        self.assertEqual(client.sent, [])


class HandleCase(unittest.TestCase):
    def test_status_command_replies(self):
        client = FakeClient()
        handler = QQInbound(owner_id=OWNER, secret=SECRET, client=client)

        status, payload = handler.handle(event(message="/status"))

        self.assertEqual(status, 200)
        self.assertIn("在线", payload["reply"])
        self.assertEqual(client.sent[0][0], OWNER)

    def test_unknown_command_replies_hint(self):
        client = FakeClient()
        handler = QQInbound(owner_id=OWNER, secret=SECRET, client=client)

        _, payload = handler.handle(event(message="rm -rf /"))

        self.assertEqual(payload["reply"], "仅支持 /status。")
        self.assertEqual(len(client.sent), 1)

    def test_duplicate_message_processed_once(self):
        client = FakeClient()
        handler = QQInbound(owner_id=OWNER, secret=SECRET, client=client)

        handler.handle(event(message_id=7, message="/status"))
        handler.handle(event(message_id=7, message="/status"))

        self.assertEqual(len(client.sent), 1)

    def test_rate_limit_drops_after_limit(self):
        client = FakeClient()
        handler = QQInbound(
            owner_id=OWNER, secret=SECRET, client=client, max_per_minute=2
        )

        for index in range(4):
            handler.handle(event(message_id=index, message="/status"))

        self.assertEqual(len(client.sent), 2)
        self.assertEqual(handler.audit[-1]["action"], "dropped:rate-limit")

    def test_segments_text_extracted(self):
        client = FakeClient()
        handler = QQInbound(owner_id=OWNER, secret=SECRET, client=client)

        handler.handle(
            event(
                message=[
                    {"type": "text", "data": {"text": "/status"}},
                    {"type": "image", "data": {"file": "x.png"}},
                ]
            )
        )

        self.assertIn("在线", client.sent[0][1])

    def test_reply_failure_is_audited(self):
        client = FakeClient()
        client.fail = True
        handler = QQInbound(owner_id=OWNER, secret=SECRET, client=client)

        status, _ = handler.handle(event(message="/status"))

        self.assertEqual(status, 200)
        self.assertTrue(handler.audit[-1]["action"].startswith("reply-failed"))

    def test_audit_summary_is_truncated(self):
        client = FakeClient()
        handler = QQInbound(owner_id=OWNER, secret=SECRET, client=client)

        handler.handle(event(message="x" * 500))

        self.assertLessEqual(len(handler.audit[-1]["summary"]), 80)


class OneBotClientCase(unittest.TestCase):
    def test_missing_config_raises(self):
        client = OneBotClient(base_url="", token="")

        with self.assertRaises(OneBotError):
            client.send_private_msg(1, "hi")

    def test_sends_with_bearer_token(self):
        captured = {}

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["auth"] = request.headers.get("Authorization")
            captured["body"] = request.data
            captured["timeout"] = timeout

            return Response(json.dumps({"status": "ok", "data": {"message_id": 9}}).encode())

        client = OneBotClient(base_url="http://127.0.0.1:3000", token="tok")

        with patch("src.qq.onebot.urllib.request.urlopen", side_effect=fake_urlopen):
            result = client.send_private_msg(123, "hi")

        self.assertEqual(result, {"message_id": 9})
        self.assertEqual(captured["url"], "http://127.0.0.1:3000/send_private_msg")
        self.assertEqual(captured["auth"], "Bearer tok")
        self.assertEqual(captured["timeout"], 10.0)

    def test_http_error_becomes_onebot_error(self):
        client = OneBotClient(base_url="http://127.0.0.1:3000", token="tok")

        with patch(
            "src.qq.onebot.urllib.request.urlopen",
            side_effect=urllib.error.HTTPError("u", 401, "no", {}, io.BytesIO(b"")),
        ):
            with self.assertRaises(OneBotError) as caught:
                client.send_private_msg(1, "hi")

        self.assertIn("401", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
