
import unittest
from unittest.mock import patch

import requests

from src.monitors.rss import fetch_rss
from src.monitors.rss_monitor import run_rss_monitor
from src.monitors.url_policy import UrlPolicyError


MONITOR_ID = "test_rss_errors"
RSS_URL = "https://blog.python.org/rss.xml"


class TestRSSErrors(unittest.TestCase):

    def test_no_new_items_is_success(self):
        """RSS 正常访问，没有新文章，应返回 0。"""

        articles = [
            {
                "id": "article-1",
                "title": "Python News",
                "link": "https://example.com/article-1"
            }
        ]

        with (
            patch(
                "src.monitors.rss_monitor.fetch_rss",
                return_value=articles
            ) as mock_fetch,
            patch(
                "src.monitors.rss_monitor.detect_new_items",
                return_value=[]
            ) as mock_detect
        ):
            result = run_rss_monitor(
                MONITOR_ID,
                RSS_URL
            )

        self.assertEqual(result, 0)
        mock_fetch.assert_called_once_with(RSS_URL)
        mock_detect.assert_called_once_with(
            MONITOR_ID,
            articles
        )

    def test_new_items_count(self):
        """成功读取两篇新文章，应该返回 2。"""

        articles = [
            {"id": "article-1", "title": "News 1"},
            {"id": "article-2", "title": "News 2"}
        ]

        with (
            patch(
                "src.monitors.rss_monitor.fetch_rss",
                return_value=articles
            ),
            patch(
                "src.monitors.rss_monitor.detect_new_items",
                return_value=articles
            )
        ):
            result = run_rss_monitor(
                MONITOR_ID,
                RSS_URL
            )

        self.assertEqual(result, 2)

    def test_http_errors_propagate(self):
        """403、429、503 不应被误认为成功。"""

        for status_code in (403, 429, 503):
            with self.subTest(status_code=status_code):

                response = requests.Response()
                response.status_code = status_code
                response.url = RSS_URL

                error = requests.HTTPError(
                    f"HTTP {status_code}",
                    response=response
                )

                with (
                    patch(
                        "src.monitors.rss_monitor.fetch_rss",
                        side_effect=error
                    ),
                    patch(
                        "src.monitors.rss_monitor.detect_new_items"
                    ) as mock_detect
                ):
                    with self.assertRaises(
                        requests.HTTPError
                    ) as context:
                        run_rss_monitor(
                            MONITOR_ID,
                            RSS_URL
                        )

                self.assertEqual(
                    context.exception.response.status_code,
                    status_code
                )

                mock_detect.assert_not_called()

    def test_timeout_propagates(self):
        """请求超时必须抛出异常。"""

        with patch(
            "src.monitors.rss_monitor.fetch_rss",
            side_effect=requests.Timeout(
                "Simulated RSS timeout"
            )
        ):
            with self.assertRaises(requests.Timeout):
                run_rss_monitor(
                    MONITOR_ID,
                    RSS_URL
                )

    def test_connection_error_propagates(self):
        """断网或 DNS 失败不能返回 0。"""

        with patch(
            "src.monitors.rss_monitor.fetch_rss",
            side_effect=requests.ConnectionError(
                "Simulated connection failure"
            )
        ):
            with self.assertRaises(
                requests.ConnectionError
            ):
                run_rss_monitor(
                    MONITOR_ID,
                    RSS_URL
                )

    def test_http_error_without_response(self):
        """没有 HTTP Response 的异常也应向上传递。"""

        with patch(
            "src.monitors.rss_monitor.fetch_rss",
            side_effect=requests.HTTPError(
                "Missing HTTP response"
            )
        ):
            with self.assertRaises(requests.HTTPError):
                run_rss_monitor(
                    MONITOR_ID,
                    RSS_URL
                )


def public_resolver(hostname, port=None):
    """假 DNS：任何域名都解析到公网地址。"""

    return [(2, 1, 6, "", ("1.1.1.1", 0))]


class FakeResponse:
    """requests.Response 的最小替身。"""

    def __init__(
        self,
        status_code=200,
        headers=None,
        chunks=None,
    ):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks or []
        self.closed = False
        self.url = ""

    @property
    def is_redirect(self):
        return self.status_code in (301, 302, 303, 307, 308)

    def raise_for_status(self):
        if self.status_code >= 400:
            response = requests.Response()
            response.status_code = self.status_code
            response.url = self.url

            raise requests.HTTPError(
                f"HTTP {self.status_code}",
                response=response,
            )

    def iter_content(self, chunk_size=0):
        for chunk in self._chunks:
            yield chunk

    def close(self):
        self.closed = True


class FakeSession:
    """requests.Session 的最小替身：记录请求并返回预置响应。"""

    def __init__(self, response=None):
        self._response = response
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append(url)

        if isinstance(self._response, Exception):
            raise self._response

        return self._response

    def close(self):
        self.closed = True


class TestFetchRssPolicy(unittest.TestCase):
    """抓取路径上的 SSRF 防护。

    这些断言是"重定向不能绕过防护"的直接证据：
    被拦下的那一跳必须**在发出请求之前**就失败。
    """

    def test_plain_http_is_rejected_before_request(self):
        with patch(
            "src.monitors.rss._pinned_session",
        ) as session_factory:
            with self.assertRaises(UrlPolicyError):
                fetch_rss(
                    "http://blog.python.org/rss.xml",
                    resolver=public_resolver,
                )

        # 策略在建立任何连接之前就拒绝了
        session_factory.assert_not_called()

    def test_redirect_to_internal_address_is_blocked(self):
        response = FakeResponse(
            status_code=302,
            headers={
                "Location": "https://127.0.0.1/secret"
            },
        )

        session = FakeSession(response)

        with patch(
            "src.monitors.rss._pinned_session",
            return_value=session,
        ) as session_factory:
            with self.assertRaises(UrlPolicyError):
                fetch_rss(
                    RSS_URL,
                    resolver=public_resolver,
                )

        # 只请求了起点；内网那一跳在建立连接之前就被拦下
        self.assertEqual(session.calls, [RSS_URL])
        self.assertTrue(response.closed)
        self.assertTrue(session.closed)

        # 只为一跳建立会话，且连接被固定到"已验证的地址"
        self.assertEqual(session_factory.call_count, 1)

        self.assertEqual(
            session_factory.call_args.kwargs,
            {
                "host": "blog.python.org",
                "connect_host": "1.1.1.1",
                "port": 443,
            },
        )

    def test_redirect_to_metadata_service_is_blocked(self):
        response = FakeResponse(
            status_code=301,
            headers={
                "Location": (
                    "https://169.254.169.254"
                    "/latest/meta-data/"
                )
            },
        )

        with patch(
            "src.monitors.rss._pinned_session",
            return_value=FakeSession(response),
        ):
            with self.assertRaises(UrlPolicyError):
                fetch_rss(
                    RSS_URL,
                    resolver=public_resolver,
                )

    def test_too_many_redirects(self):
        response = FakeResponse(
            status_code=302,
            headers={"Location": RSS_URL},
        )

        with patch(
            "src.monitors.rss._pinned_session",
            return_value=FakeSession(response),
        ):
            with self.assertRaises(UrlPolicyError):
                fetch_rss(
                    RSS_URL,
                    resolver=public_resolver,
                )

    def test_redirect_without_location_is_rejected(self):
        response = FakeResponse(status_code=302)

        with patch(
            "src.monitors.rss._pinned_session",
            return_value=FakeSession(response),
        ):
            with self.assertRaises(ValueError):
                fetch_rss(
                    RSS_URL,
                    resolver=public_resolver,
                )

    def test_oversized_body_is_rejected(self):
        response = FakeResponse(
            chunks=[b"x" * 64, b"y" * 64],
        )

        with (
            patch(
                "src.monitors.rss._pinned_session",
                return_value=FakeSession(response),
            ),
            patch(
                "src.monitors.rss.MAX_RESPONSE_BYTES",
                100,
            ),
        ):
            with self.assertRaises(UrlPolicyError):
                fetch_rss(
                    RSS_URL,
                    resolver=public_resolver,
                )

    def test_declared_oversized_body_is_rejected(self):
        response = FakeResponse(
            headers={"Content-Length": "999999999"},
            chunks=[b"<rss></rss>"],
        )

        with patch(
            "src.monitors.rss._pinned_session",
            return_value=FakeSession(response),
        ):
            with self.assertRaises(UrlPolicyError):
                fetch_rss(
                    RSS_URL,
                    resolver=public_resolver,
                )

    def test_http_error_keeps_response_for_retry_policy(self):
        response = FakeResponse(status_code=429)

        with patch(
            "src.monitors.rss._pinned_session",
            return_value=FakeSession(response),
        ):
            with self.assertRaises(
                requests.HTTPError
            ) as context:
                fetch_rss(
                    RSS_URL,
                    resolver=public_resolver,
                )

        # rss_monitor 依赖 exc.response.status_code 决定重试策略
        self.assertEqual(
            context.exception.response.status_code,
            429,
        )


if __name__ == "__main__":
    unittest.main()