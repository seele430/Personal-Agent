"""搜索 Provider 单元测试。

完全离线：传输层由用例注入，既不联网也没有 socket。
"""

import json
import unittest

from pathlib import Path

from src.web.search import (
    API_KEY_ENV,
    BochaSearchProvider,
    FakeSearchProvider,
    SearchAuthError,
    SearchPayloadError,
    SearchRateLimited,
    SearchUnavailable,
    normalize_count,
    normalize_query,
    provider_from_env,
)


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "web"
SEARCH_FIXTURE = FIXTURES / "search_vllm.json"


def transport_returning(status, payload=b"", calls=None):
    """构造一个假传输层，记录调用参数。"""

    def transport(url, headers, body, timeout):
        if calls is not None:
            calls.append(
                {
                    "url": url,
                    "headers": dict(headers),
                    "body": json.loads(body.decode("utf-8")),
                    "timeout": timeout,
                }
            )

        return status, payload

    return transport


def transport_raising(error):
    def transport(url, headers, body, timeout):
        raise error

    return transport


class TestBochaSearchProvider(unittest.TestCase):
    """解析真实响应形状、过滤不可用条目。"""

    def setUp(self):
        self.payload = SEARCH_FIXTURE.read_bytes()

    def test_returns_usable_hits_and_drops_bad_entries(self):
        calls = []
        provider = BochaSearchProvider(
            "test-key", transport=transport_returning(200, self.payload, calls)
        )

        outcome = provider.search("vllm pagedattention", count=5)

        self.assertEqual(outcome.provider, "bocha")
        self.assertEqual(outcome.query, "vllm pagedattention")
        # 4 条原始结果里，两条没有可用 URL，应被丢弃
        self.assertEqual(outcome.count, 2)
        self.assertEqual(
            [hit.url for hit in outcome.hits],
            [
                "https://arxiv.org/abs/2309.06180",
                "https://docs.vllm.ai/en/latest/",
            ],
        )
        self.assertIn("PagedAttention", outcome.hits[0].title)
        self.assertGreaterEqual(outcome.latency_ms, 0.0)

        # 请求体符合博查 API 的形状
        self.assertEqual(calls[0]["body"]["query"], "vllm pagedattention")
        self.assertEqual(calls[0]["body"]["count"], 5)
        self.assertTrue(calls[0]["headers"]["Authorization"].startswith("Bearer "))

    def test_snippet_and_title_are_truncated(self):
        long_title = "标题" * 300
        long_snippet = "摘要" * 400
        payload = json.dumps(
            {
                "code": 200,
                "data": {
                    "webPages": {
                        "value": [
                            {
                                "name": long_title,
                                "url": "https://example.org/a",
                                "summary": long_snippet,
                            }
                        ]
                    }
                },
            }
        ).encode("utf-8")

        provider = BochaSearchProvider(
            "test-key", transport=transport_returning(200, payload)
        )

        hit = provider.search("x", count=1).hits[0]

        self.assertLessEqual(len(hit.title), 200)
        self.assertLessEqual(len(hit.snippet), 300)

    def test_missing_key_fails_before_any_request(self):
        calls = []
        provider = BochaSearchProvider(
            "", transport=transport_returning(200, b"{}", calls)
        )

        with self.assertRaises(SearchAuthError):
            provider.search("x")

        self.assertEqual(calls, [])

    def test_http_status_maps_to_distinct_errors(self):
        cases = (
            (401, SearchAuthError),
            (403, SearchAuthError),
            (429, SearchRateLimited),
            (500, SearchUnavailable),
            (503, SearchUnavailable),
            (418, SearchPayloadError),
        )

        for status, expected in cases:
            with self.subTest(status=status):
                provider = BochaSearchProvider(
                    "k", transport=transport_returning(status, b"")
                )

                with self.assertRaises(expected):
                    provider.search("x")

    def test_transport_error_is_wrapped(self):
        provider = BochaSearchProvider(
            "k", transport=transport_raising(TimeoutError("boom"))
        )

        with self.assertRaises(SearchUnavailable):
            provider.search("x")

    def test_bad_payload_is_explicit(self):
        for payload in (b"not json", b"[1,2,3]"):
            with self.subTest(payload=payload):
                provider = BochaSearchProvider(
                    "k", transport=transport_returning(200, payload)
                )

                with self.assertRaises(SearchPayloadError):
                    provider.search("x")

    def test_empty_result_list_is_not_an_error(self):
        provider = BochaSearchProvider(
            "k",
            transport=transport_returning(
                200, json.dumps({"code": 200, "data": {}}).encode("utf-8")
            ),
        )

        self.assertEqual(provider.search("x").count, 0)


class TestInputValidation(unittest.TestCase):
    """查询词与条数必须在进门处收敛。"""

    def test_query_normalization(self):
        self.assertEqual(normalize_query("  vllm   paged \n"), "vllm paged")

        for bad in ("", "   ", None, 123):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    normalize_query(bad)

        with self.assertRaises(ValueError):
            normalize_query("x" * 201)

    def test_count_bounds(self):
        self.assertEqual(normalize_count(1), 1)
        self.assertEqual(normalize_count(10), 10)

        for bad in (0, 11, -1, True, 1.5, "3"):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    normalize_count(bad)

    def test_invalid_query_never_reaches_transport(self):
        calls = []
        provider = BochaSearchProvider(
            "k", transport=transport_returning(200, b"{}", calls)
        )

        with self.assertRaises(ValueError):
            provider.search("   ")

        self.assertEqual(calls, [])


class TestFakeProviderAndFactory(unittest.TestCase):
    """离线替身与环境选择。"""

    def test_fake_provider_respects_count(self):
        from src.web.search import SearchHit

        hits = tuple(
            SearchHit(title=f"t{i}", url=f"https://e.org/{i}", snippet="", rank=i)
            for i in range(1, 4)
        )
        provider = FakeSearchProvider(hits)

        self.assertEqual(provider.search("q", count=2).count, 2)
        self.assertEqual(provider.calls, [("q", 2)])

    def test_fake_provider_can_raise(self):
        provider = FakeSearchProvider(error=SearchRateLimited("限流"))

        with self.assertRaises(SearchRateLimited):
            provider.search("q")

    def test_factory_defaults_to_bocha(self):
        from unittest.mock import patch

        with patch.dict("os.environ", {API_KEY_ENV: "k"}, clear=False):
            provider = provider_from_env()

        self.assertIsInstance(provider, BochaSearchProvider)

    def test_factory_unknown_name_is_rejected(self):
        from unittest.mock import patch

        with patch.dict(
            "os.environ", {"WEB_SEARCH_PROVIDER": "nope"}, clear=False
        ):
            with self.assertRaises(ValueError):
                provider_from_env()


if __name__ == "__main__":
    unittest.main()
