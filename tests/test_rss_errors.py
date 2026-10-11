
import unittest
from unittest.mock import patch

import requests

from src.monitors.rss_monitor import run_rss_monitor


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


if __name__ == "__main__":
    unittest.main()