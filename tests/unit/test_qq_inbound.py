"""QQ 入站与 OneBot 客户端测试：全 mock，不联网、不连 NapCat。"""

import hashlib
import hmac
import io
import json
import unittest
import urllib.error

from pathlib import Path

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

        self.assertEqual(payload["reply"], "仅支持 /status 与 /ask。")
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
        self.assertEqual(captured["url"], "http://127.0.0.1:3000/send_msg")
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




class FakeProvider:
    configured = True
    name = "ark"
    model = "doubao-test"

    def __init__(self, answer="答案", error=None):
        self.answer = answer
        self.error = error

    def submit(self, prompt, *, artifact_name, out_dir):
        if self.error:
            raise RuntimeError(self.error)

        (Path(out_dir) / artifact_name).write_text(self.answer, encoding="utf-8")

        return {"task_id": "t"}


class AskCase(unittest.TestCase):
    def handler(self, provider):
        return QQInbound(owner_id=OWNER, secret=SECRET, client=FakeClient())

    def run_ask(self, provider, text, provider_name="ark"):
        handler = self.handler(provider)

        with patch(
            "src.collab.providers.build_provider", return_value=provider
        ), patch(
            "src.collab.providers.provider_name", return_value=provider_name
        ):
            status, payload = handler.handle(event(message=text))

        return handler, payload

    def test_ask_returns_answer(self):
        _, payload = self.run_ask(FakeProvider("4"), "/ask 1+1")

        self.assertEqual(payload["reply"], "4")

    def test_ask_empty_question(self):
        _, payload = self.run_ask(FakeProvider(), "/ask")

        self.assertIn("用法", payload["reply"])

    def test_ask_too_long(self):
        _, payload = self.run_ask(FakeProvider(), "/ask " + "x" * 600)

        self.assertIn("太长", payload["reply"])

    def test_ask_provider_failure(self):
        _, payload = self.run_ask(FakeProvider(error="HTTP 500"), "/ask hi")

        self.assertIn("调用失败", payload["reply"])
        self.assertIn("HTTP 500", payload["reply"])

    def test_ask_rejects_agent_mode(self):
        _, payload = self.run_ask(FakeProvider(), "/ask hi", provider_name="qwen-agent")

        self.assertIn("agent", payload["reply"])

    def test_ask_truncates_long_answer(self):
        _, payload = self.run_ask(FakeProvider("y" * 2000), "/ask hi")

        self.assertLessEqual(len(payload["reply"]), 810)
        self.assertIn("已截断", payload["reply"])

    def test_ask_audited_without_full_text(self):
        handler, _ = self.run_ask(FakeProvider("答案"), "/ask 秘密问题")

        record = next(item for item in handler.audit if item.get("action") == "ask")
        self.assertEqual(record["action"], "ask")
        self.assertEqual(record["question_chars"], len("秘密问题"))
        self.assertNotIn("秘密问题", str(record))


if __name__ == "__main__":
    unittest.main()
