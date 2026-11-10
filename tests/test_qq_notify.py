"""QQ 通知渠道：发送模块与渠道判定测试（全 mock）。"""

import os
import unittest

from unittest.mock import patch

from src.core import notification_delivery as delivery
from src.core import qq_notify
from src.qq.onebot import OneBotError


class FakeClient:
    def __init__(self):
        self.calls = []

    def send_private_msg(self, user_id, text):
        self.calls.append((user_id, text))

        return {"message_id": 1}


class QqNotifyCase(unittest.TestCase):
    def env(self, **extra):
        base = {
            "PA_QQ_OWNER": "10001",
            "PA_ONEBOT_URL": "http://127.0.0.1:3001",
            "PA_ONEBOT_TOKEN": "token",
        }
        base.update(extra)

        return patch.dict(os.environ, base, clear=False)

    def test_empty_message_rejected(self):
        with self.env(), self.assertRaises(ValueError):
            qq_notify.send_qq_notification("  ", client=FakeClient())

    def test_truncate_1000_chars(self):
        client = FakeClient()

        with self.env():
            qq_notify.send_qq_notification("x" * 1001, client=client)

        self.assertEqual(len(client.calls[0][1]), 1000)

    def test_invalid_owner(self):
        for value in ("0", "-1", "abc", ""):
            with self.env(PA_QQ_OWNER=value), self.assertRaises(ValueError):
                qq_notify.send_qq_notification("hello", client=FakeClient())

    def test_missing_token(self):
        with patch.dict(
            os.environ,
            {"PA_QQ_OWNER": "10001", "PA_ONEBOT_URL": "http://127.0.0.1:3001"},
            clear=True,
        ), self.assertRaises(ValueError):
            qq_notify.send_qq_notification("hello", client=None)

    def test_remote_url_rejected(self):
        for url in ("http://example.com:3001", "https://10.0.0.1:3001", "ftp://127.0.0.1:3001"):
            with self.env(PA_ONEBOT_URL=url), self.assertRaises(ValueError):
                qq_notify.send_qq_notification("hello", client=None)

    def test_userinfo_url_rejected(self):
        with self.env(PA_ONEBOT_URL="http://user:pass@127.0.0.1:3001"), self.assertRaises(ValueError):
            qq_notify.send_qq_notification("hello", client=None)

    def test_owner_send(self):
        client = FakeClient()

        with self.env():
            qq_notify.send_qq_notification("hello", client=client)

        self.assertEqual(client.calls, [(10001, "hello")])

    def test_network_error_propagates(self):
        client = FakeClient()
        client.send_private_msg = lambda *args: (_ for _ in ()).throw(OneBotError("down"))

        with self.env(), self.assertRaises(OneBotError):
            qq_notify.send_qq_notification("hello", client=client)


class ChannelCase(unittest.TestCase):
    def test_channel_matrix(self):
        with patch.object(delivery, "role_of_user", return_value="owner"), patch.dict(
            os.environ, {"PA_QQ_NOTIFY": "1"}
        ):
            self.assertEqual(delivery.channel_for_owner("auth-uuid"), delivery.QQ)

        with patch.object(delivery, "role_of_user", return_value="owner"), patch.dict(
            os.environ, {"PA_QQ_NOTIFY": "0"}
        ):
            self.assertEqual(delivery.channel_for_owner("auth-uuid"), delivery.NTFY)

        with patch.object(delivery, "role_of_user", return_value="admin"), patch.dict(
            os.environ, {"PA_QQ_NOTIFY": "1"}
        ):
            self.assertEqual(delivery.channel_for_owner("auth-uuid"), delivery.IN_APP)

        with patch.object(delivery, "role_of_user", return_value=None):
            self.assertEqual(delivery.channel_for_owner("auth-uuid"), delivery.IN_APP)

        self.assertEqual(delivery.channel_for_owner(None), delivery.NTFY)


if __name__ == "__main__":
    unittest.main()
