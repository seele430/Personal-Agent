"""`send_notification` 单测：全 mock，不联网。"""

import unittest

from unittest.mock import patch

import src.notification as notification


class NotificationCase(unittest.TestCase):
    def setUp(self):
        self._env = patch.dict("os.environ", {}, clear=False)
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_missing_topic_raises(self):
        with patch.dict("os.environ", {"NTFY_TOPIC": ""}, clear=False):
            with self.assertRaises(ValueError) as caught:
                notification.send_notification("hello")

        self.assertIn("NTFY_TOPIC", str(caught.exception))

    def test_empty_message_raises(self):
        with patch.dict("os.environ", {"NTFY_TOPIC": "t" * 10}, clear=False):
            with self.assertRaises(ValueError):
                notification.send_notification("   ")

    def test_posts_utf8_body_to_topic(self):
        with patch.dict(
            "os.environ",
            {"NTFY_TOPIC": "topic-abc", "NTFY_SERVER": "https://example.invalid/"},
            clear=False,
        ), patch("src.notification.requests.post") as post:
            post.return_value.raise_for_status.return_value = None

            result = notification.send_notification("提醒：喝水")

        self.assertTrue(result)
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://example.invalid/topic-abc")
        self.assertEqual(kwargs["data"], "提醒：喝水".encode("utf-8"))
        self.assertEqual(kwargs["timeout"], 15)
        self.assertIn("charset=utf-8", kwargs["headers"]["Content-Type"])

    def test_http_error_propagates(self):
        with patch.dict("os.environ", {"NTFY_TOPIC": "topic-abc"}, clear=False), \
             patch("src.notification.requests.post") as post:
            post.return_value.raise_for_status.side_effect = RuntimeError("500")

            with self.assertRaises(RuntimeError):
                notification.send_notification("hello")


if __name__ == "__main__":
    unittest.main()
