"""抓取安全边界测试。

不联网：DNS 解析与 HTTP 会话都替换成替身，但**被替换的正是生产代码
调用的那两个入口**，所以"每一跳重新校验"的链路仍然被真实执行。
"""

import unittest

from unittest.mock import patch

import requests

import src.web.fetch as web_fetch

from src.monitors.url_policy import UrlPolicyError
from src.web.fetch import (
    FetchBlocked,
    FetchError,
    FetchHTTPError,
    FetchTimeout,
    FetchTooLarge,
    FetchUnsupportedType,
    fetch_page,
)


PUBLIC = ["93.184.216.34"]


class FakeResponse:
    def __init__(
        self,
        status=200,
        body=b"",
        headers=None,
        location=None,
    ):
        self.status_code = status
        self.headers = dict(headers or {})
        self._body = body
        self.is_redirect = location is not None
        self.closed = False

        if location is not None:
            self.headers["Location"] = location

    def iter_content(self, chunk_size=65536):
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start:start + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.closed = False
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})

        item = self._responses.pop(0)

        if isinstance(item, Exception):
            raise item

        return item

    def close(self):
        self.closed = True


class FetchTestCase(unittest.TestCase):
    def patch_network(self, responses, resolver=None):
        """把 DNS 与连接都换成替身，返回 (session, 取消 patch 的上下文)。"""

        session = FakeSession(responses)

        def default_resolver(url, resolver=None):
            return ("example.org", PUBLIC)

        stack = []

        def patcher():
            stack.append(
                patch.object(
                    web_fetch,
                    "resolve_for_fetch",
                    resolver or default_resolver,
                )
            )
            stack.append(
                patch.object(
                    web_fetch,
                    "_pinned_session",
                    lambda **kwargs: session,
                )
            )

            for item in stack:
                item.start()

            return session

        self.addCleanup(lambda: [item.stop() for item in stack])

        return patcher()


class TestPolicyRejections(FetchTestCase):
    """策略层拒绝：不需要任何替身，也不会产生连接。"""

    def test_plain_http_is_blocked(self):
        with self.assertRaises(FetchBlocked) as caught:
            fetch_page("http://example.org/article")

        self.assertIn("HTTPS", str(caught.exception))

    def test_loopback_literal_is_blocked(self):
        with self.assertRaises(FetchBlocked):
            fetch_page("https://127.0.0.1/admin")

    def test_private_literal_is_blocked(self):
        with self.assertRaises(FetchBlocked):
            fetch_page("https://192.168.1.10/internal")

    def test_each_redirect_hop_is_revalidated(self):
        """公网重定向到内网必须被拦在**第二跳**的校验上。"""

        def resolver(url, resolver=None):
            if "127.0.0.1" in url:
                raise UrlPolicyError("不允许访问非公网地址")

            return ("example.org", PUBLIC)

        session = self.patch_network(
            [FakeResponse(302, location="https://127.0.0.1/secret")],
            resolver=resolver,
        )

        with self.assertRaises(FetchBlocked):
            fetch_page("https://example.org/start")

        # 只发出了第一跳的请求，第二跳在校验处就被拦下
        self.assertEqual(len(session.calls), 1)


class TestResponseHandling(FetchTestCase):
    def test_successful_page_returns_metadata(self):
        body = b"<html><body>" + b"text " * 100 + b"</body></html>"
        session = self.patch_network(
            [
                FakeResponse(302, location="/final"),
                FakeResponse(
                    200,
                    body=body,
                    headers={
                        "Content-Type": "text/html; charset=utf-8",
                        "ETag": '"abc"',
                    },
                ),
            ]
        )

        page = fetch_page("https://example.org/start")

        self.assertEqual(page.status, 200)
        self.assertEqual(page.url, "https://example.org/final")
        self.assertEqual(page.redirects, 1)
        self.assertEqual(page.media_type, "text/html")
        self.assertEqual(page.body, body)
        self.assertEqual(page.content_length, len(body))
        self.assertEqual(page.etag, '"abc"')
        self.assertEqual(len(session.calls), 2)
        self.assertTrue(session.closed)

    def test_http_error_keeps_status_code(self):
        self.patch_network(
            [FakeResponse(404, headers={"Content-Type": "text/html"})]
        )

        with self.assertRaises(FetchHTTPError) as caught:
            fetch_page("https://example.org/missing")

        self.assertEqual(caught.exception.status, 404)

    def test_unsupported_media_type_is_rejected(self):
        self.patch_network(
            [
                FakeResponse(
                    200,
                    body=b"%PDF-1.7",
                    headers={"Content-Type": "application/pdf"},
                )
            ]
        )

        with self.assertRaises(FetchUnsupportedType):
            fetch_page("https://example.org/paper.pdf")

    def test_declared_length_over_limit_is_rejected(self):
        self.patch_network(
            [
                FakeResponse(
                    200,
                    body=b"x" * 10,
                    headers={
                        "Content-Type": "text/html",
                        "Content-Length": "9999999",
                    },
                )
            ]
        )

        with self.assertRaises(FetchTooLarge):
            fetch_page("https://example.org/big", max_bytes=1024)

    def test_streaming_body_over_limit_is_rejected(self):
        self.patch_network(
            [
                FakeResponse(
                    200,
                    body=b"x" * 5000,
                    headers={"Content-Type": "text/html"},
                )
            ]
        )

        with self.assertRaises(FetchTooLarge):
            fetch_page("https://example.org/big", max_bytes=1024)

    def test_timeout_is_its_own_error(self):
        self.patch_network([requests.Timeout("slow")])

        with self.assertRaises(FetchTimeout):
            fetch_page("https://example.org/slow")

    def test_connection_error_is_wrapped(self):
        self.patch_network([requests.ConnectionError("reset")])

        with self.assertRaises(FetchError) as caught:
            fetch_page("https://example.org/down")

        self.assertNotIsInstance(caught.exception, FetchTimeout)

    def test_redirect_without_location_is_an_error(self):
        response = FakeResponse(302)
        response.is_redirect = True

        self.patch_network([response])

        with self.assertRaises(FetchHTTPError):
            fetch_page("https://example.org/weird")

    def test_too_many_redirects_is_blocked(self):
        responses = [
            FakeResponse(302, location=f"/hop{index}")
            for index in range(10)
        ]
        self.patch_network(responses)

        with self.assertRaises(FetchBlocked):
            fetch_page("https://example.org/start")

    def test_invalid_max_bytes_is_rejected(self):
        for bad in (0, -1, True, "1024"):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    fetch_page("https://example.org/x", max_bytes=bad)


if __name__ == "__main__":
    unittest.main()
