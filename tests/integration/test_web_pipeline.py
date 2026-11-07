"""流水线测试：`search → fetch → extract → cite`。

不联网：搜索用 FakeSearchProvider，抓取与抽取都可注入。
数据库落在临时目录。
"""

import tempfile
import unittest

from pathlib import Path

from src.web.evidence import EvidenceStore
from src.web.extract import ExtractError, extract_text
from src.web.fetch import FetchBlocked, FetchHTTPError, FetchedPage
from src.web.pipeline import (
    CitationError,
    run_research,
    verify_citations,
)
from src.web.pipeline import cite_claim
from src.web.search import (
    FakeSearchProvider,
    SearchHit,
    SearchRateLimited,
)


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "web"
ARXIV_BODY = (FIXTURES / "page_arxiv.html").read_bytes()
SHORT_BODY = (FIXTURES / "page_short.html").read_bytes()


def hit(url, title="标题", rank=1):
    return SearchHit(title=title, url=url, snippet="", rank=rank)


def page(url):
    return FetchedPage(
        url=url,
        status=200,
        media_type="text/html",
        body=ARXIV_BODY,
        redirects=0,
        etag='"e"',
    )


def fetcher_by_url(mapping):
    calls = []

    def fetch(url):
        calls.append(url)

        item = mapping[url]

        if isinstance(item, Exception):
            raise item

        return item

    fetch.calls = calls

    return fetch


class PipelineTestCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.store = EvidenceStore(Path(self._temporary.name) / "evidence.db")


class TestHappyPath(PipelineTestCase):
    def test_search_fetch_extract_and_store(self):
        provider = FakeSearchProvider(
            (hit("https://arxiv.org/abs/2309.06180"),)
        )

        run = run_research(
            "pagedattention",
            provider=provider,
            store=self.store,
            count=3,
            max_pages=2,
            min_chars=200,
            fetcher=fetcher_by_url(
                {"https://arxiv.org/abs/2309.06180": page("https://arxiv.org/abs/2309.06180")}
            ),
        )

        self.assertTrue(run.ok)
        self.assertEqual(run.hits, 1)
        self.assertEqual(len(run.stored), 1)
        self.assertEqual(run.failures, ())
        self.assertEqual(self.store.count_sources(), 1)

        record = self.store.get_source(run.evidence_ids[0])

        self.assertIn("PagedAttention", record.title)
        self.assertGreaterEqual(record.char_count, 500)
        self.assertIn("Efficient Memory Management", record.title)
        self.assertEqual(record.query, "pagedattention")

    def test_duplicate_content_is_not_stored_twice(self):
        url = "https://arxiv.org/abs/2309.06180"
        provider = FakeSearchProvider((hit(url),))

        for _ in range(2):
            run = run_research(
                "again",
                provider=provider,
                store=self.store,
                min_chars=200,
                fetcher=fetcher_by_url({url: page(url)}),
            )

        self.assertEqual(
            [item.status for item in run.sources], ["duplicate"]
        )
        self.assertEqual(self.store.count_sources(), 1)


class TestFailureVisibility(PipelineTestCase):
    def test_each_failure_has_a_classification(self):
        hits = (
            hit("javascript:alert(1)"),
            hit("https://example.org/403"),
            hit("https://example.org/short"),
            hit("https://example.org/blocked"),
        )

        def fetcher(url):
            if url.endswith("/403"):
                raise FetchHTTPError("forbidden", 403)

            if url.endswith("/blocked"):
                raise FetchBlocked("不允许访问非公网地址")

            if url.endswith("/short"):
                return FetchedPage(
                    url=url,
                    status=200,
                    media_type="text/html",
                    body=SHORT_BODY,
                    redirects=0,
                )

            return page(url)

        run = run_research(
            "failures",
            provider=FakeSearchProvider(hits),
            store=self.store,
            min_chars=500,
            fetcher=fetcher,
            extractor=extract_text,
        )

        statuses = [item.status for item in run.sources]

        self.assertIn("bad_url", statuses)
        self.assertIn("http_403", statuses)
        self.assertIn("extract_failed", statuses)
        self.assertIn("blocked", statuses)

        # 失败一律不进证据库
        self.assertFalse(run.ok)
        self.assertEqual(self.store.count_sources(), 0)

    def test_extractor_failure_produces_empty_body_error(self):
        def extractor(body, *, url="", min_chars=200):
            raise ExtractError("正文过短")

        run = run_research(
            "short",
            provider=FakeSearchProvider((hit("https://example.org/s"),)),
            store=self.store,
            min_chars=200,
            fetcher=lambda url: FetchedPage(
                url=url,
                status=200,
                media_type="text/html",
                body=SHORT_BODY,
                redirects=0,
            ),
            extractor=extractor,
        )

        self.assertEqual(run.sources[0].status, "extract_failed")
        self.assertEqual(self.store.count_sources(), 0)

    def test_search_error_is_reported_not_raised(self):
        run = run_research(
            "q",
            provider=FakeSearchProvider(error=SearchRateLimited("限流")),
            store=self.store,
        )

        self.assertFalse(run.ok)
        self.assertIn("限流", run.error)
        self.assertEqual(run.sources, ())


class TestBounds(PipelineTestCase):
    def test_same_url_twice_is_deduped(self):
        url = "https://example.org/x"
        provider = FakeSearchProvider((hit(url), hit(url)))

        run = run_research(
            "dup",
            provider=provider,
            store=self.store,
            min_chars=200,
            fetcher=fetcher_by_url({url: page(url)}),
        )

        self.assertEqual(
            [item.status for item in run.sources], ["stored", "duplicate_hit"]
        )

    def test_max_pages_caps_fetching(self):
        urls = [f"https://example.org/{index}" for index in range(5)]
        fetch = fetcher_by_url({url: page(url) for url in urls})

        run = run_research(
            "cap",
            provider=FakeSearchProvider(tuple(hit(u, rank=i) for i, u in enumerate(urls))),
            store=self.store,
            count=5,
            max_pages=2,
            min_chars=200,
            fetcher=fetch,
        )

        self.assertEqual(len(fetch.calls), 2)
        self.assertEqual(len(run.stored), 2)
        self.assertEqual(self.store.count_sources(), 2)

    def test_bad_bounds_are_rejected(self):
        provider = FakeSearchProvider(())

        for kwargs in (
            {"count": 0},
            {"count": 11},
            {"max_pages": 0},
            {"max_pages": 99},
            {"min_chars": 0},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    run_research("q", provider=provider, store=self.store, **kwargs)


class TestCitations(PipelineTestCase):
    def store_one(self):
        run = run_research(
            "cite",
            provider=FakeSearchProvider((hit("https://arxiv.org/abs/2309.06180"),)),
            store=self.store,
            min_chars=200,
            fetcher=fetcher_by_url(
                {"https://arxiv.org/abs/2309.06180": page("https://arxiv.org/abs/2309.06180")}
            ),
        )

        return run.evidence_ids[0]

    def test_quote_must_exist_in_the_evidence(self):
        evidence_id = self.store_one()

        citation = cite_claim(
            self.store,
            report_key="r1",
            claim_key="c1",
            evidence_id=evidence_id,
            quote="key-value cache for each request is large",
        )

        self.assertEqual(citation.source_id, evidence_id)
        self.assertEqual(verify_citations(self.store, "r1"), [])

    def test_fabricated_quote_is_rejected(self):
        evidence_id = self.store_one()

        with self.assertRaises(CitationError):
            cite_claim(
                self.store,
                report_key="r1",
                claim_key="c1",
                evidence_id=evidence_id,
                quote="这句话根本不在原文里",
            )

    def test_citation_to_missing_evidence_is_rejected(self):
        from src.web.evidence import EvidenceError

        with self.assertRaises(EvidenceError):
            cite_claim(
                self.store,
                report_key="r1",
                claim_key="c1",
                evidence_id=999,
                quote="x",
            )

    def test_verify_detects_tampered_quote(self):
        evidence_id = self.store_one()

        # 绕过 cite_claim 直接写一条与正文不符的引用，模拟"事后改稿"
        self.store.add_citation(
            report_key="r2",
            claim_key="c9",
            source_id=evidence_id,
            quote="编造的引文",
        )

        problems = verify_citations(self.store, "r2")

        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0]["reason"], "引文不在正文里")


if __name__ == "__main__":
    unittest.main()
