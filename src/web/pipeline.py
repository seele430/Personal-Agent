"""研究流水线：`search → fetch → extract → cite`，有界、去重、失败显式。

三条设计红线（路线图 §2 原则 5 / §4.3）：

1. **失败不冒充资料**：抓取/抽取的每一种失败都进 `failures` 并带分类标签，
   绝不写进证据库。
2. **有界**：查询条数、最多抓几页、每页字符门槛都由参数控制，不无界翻页。
3. **引用必须命中证据**：`cite_claim()` 会回查证据正文，
   引文不在正文里就抛 `CitationError`——这是"论断被原文支持"的机器检查。

抓取与抽取都可注入（`fetcher=` / `extractor=`），所以单测完全不联网。
"""

from __future__ import annotations

import time

from dataclasses import dataclass

from src.web.evidence import (
    CitationRecord,
    EvidenceError,
    EvidenceRecord,
    EvidenceStore,
    get_evidence_store,
    normalize_url,
)
from src.web.extract import ExtractError, extract_text, text_sha256
from src.web.fetch import (
    FetchBlocked,
    FetchError,
    FetchHTTPError,
    FetchTimeout,
    FetchTooLarge,
    FetchUnsupportedType,
    fetch_page,
)
from src.web.search import (
    SearchError,
    SearchHit,
    SearchOutcome,
    SearchProvider,
    provider_from_env,
)


DEFAULT_MIN_CHARS = 500
DEFAULT_MAX_PAGES = 3
HARD_MAX_PAGES = 10

STATUS_STORED = "stored"
STATUS_DUPLICATE = "duplicate"


class CitationError(RuntimeError):
    """引用不成立：证据不存在，或引文不在正文里。"""


@dataclass(frozen=True)
class SourceOutcome:
    """一条搜索结果的去向。失败项同样记录，便于分类统计。"""

    url: str
    status: str
    evidence_id: int | None = None
    final_url: str = ""
    title: str = ""
    char_count: int = 0
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_STORED, STATUS_DUPLICATE)


@dataclass(frozen=True)
class ResearchRun:
    """一次研究的完整记录。"""

    query: str
    provider: str
    hits: int
    sources: tuple[SourceOutcome, ...] = ()
    latency_ms: float = 0.0
    search_latency_ms: float = 0.0
    error: str = ""

    @property
    def stored(self) -> tuple[SourceOutcome, ...]:
        return tuple(
            item for item in self.sources
            if item.status in (STATUS_STORED, STATUS_DUPLICATE)
        )

    @property
    def failures(self) -> tuple[SourceOutcome, ...]:
        return tuple(item for item in self.sources if not item.ok)

    @property
    def evidence_ids(self) -> tuple[int, ...]:
        return tuple(
            item.evidence_id
            for item in self.stored
            if item.evidence_id is not None
        )

    @property
    def ok(self) -> bool:
        return bool(self.stored) and not self.error


def _classify(error: Exception) -> str:
    """把异常翻译成稳定的分类标签（§4.3 要求按类分类失败）。"""

    if isinstance(error, FetchBlocked):
        return "blocked"

    if isinstance(error, FetchTimeout):
        return "timeout"

    if isinstance(error, FetchHTTPError):
        return f"http_{error.status}"

    if isinstance(error, FetchTooLarge):
        return "too_large"

    if isinstance(error, FetchUnsupportedType):
        return "unsupported_type"

    if isinstance(error, ExtractError):
        return "extract_failed"

    if isinstance(error, EvidenceError):
        return "evidence_error"

    if isinstance(error, FetchError):
        return "fetch_failed"

    return "unexpected"


def _validate_bounds(count: int, max_pages: int, min_chars: int) -> None:
    for name, value, low, high in (
        ("count", count, 1, 10),
        ("max_pages", max_pages, 1, HARD_MAX_PAGES),
        ("min_chars", min_chars, 1, 100_000),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} 必须是整数")

        if not low <= value <= high:
            raise ValueError(f"{name} 必须在 {low}..{high} 之间")


def run_research(
    query: str,
    *,
    provider: SearchProvider | None = None,
    store: EvidenceStore | None = None,
    count: int = 5,
    max_pages: int = DEFAULT_MAX_PAGES,
    min_chars: int = DEFAULT_MIN_CHARS,
    fetcher=None,
    extractor=None,
    report_query: str | None = None,
) -> ResearchRun:
    """跑一次有界的检索→取证。

    返回的 `ResearchRun` 无论成败都完整记录，调用方据此判断
    "这次研究有几条证据、失败都是什么类型"。
    """

    _validate_bounds(count, max_pages, min_chars)

    provider = provider or provider_from_env()
    store = store or get_evidence_store()
    fetcher = fetcher or fetch_page
    extractor = extractor or extract_text

    started = time.perf_counter()

    try:
        outcome: SearchOutcome = provider.search(query, count=count)

    except SearchError as error:
        return ResearchRun(
            query=str(query),
            provider=getattr(provider, "name", "unknown"),
            hits=0,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            error=f"搜索失败：{error}",
        )

    hits: tuple[SearchHit, ...] = outcome.hits
    seen: set[str] = set()
    sources: list[SourceOutcome] = []

    for hit in hits:
        if len([item for item in sources if item.ok]) >= max_pages:
            break

        try:
            canonical = normalize_url(hit.url)

        except ValueError:
            sources.append(
                SourceOutcome(url=hit.url, status="bad_url")
            )
            continue

        if canonical in seen:
            sources.append(
                SourceOutcome(url=hit.url, status="duplicate_hit")
            )
            continue

        seen.add(canonical)

        try:
            page = fetcher(hit.url)
            extracted = extractor(page.body, url=page.url, min_chars=min_chars)

        except Exception as error:  # noqa: BLE001 - 分类后记录，不吞
            sources.append(
                SourceOutcome(
                    url=hit.url,
                    status=_classify(error),
                    detail=str(error)[:300],
                )
            )
            continue

        digest = text_sha256(extracted.text)
        existing = store.find_by_text_sha(digest)

        if existing is not None and existing.url == canonical:
            sources.append(
                SourceOutcome(
                    url=hit.url,
                    status=STATUS_DUPLICATE,
                    evidence_id=existing.id,
                    final_url=page.url,
                    title=extracted.title or existing.title,
                    char_count=existing.char_count,
                )
            )
            continue

        try:
            record = store.add_source(
                url=hit.url,
                final_url=page.url,
                text=extracted.text,
                http_status=page.status,
                media_type=page.media_type,
                title=extracted.title,
                method=extracted.method,
                byte_length=page.content_length,
                etag=page.etag,
                query=report_query if report_query is not None else outcome.query,
                rank=hit.rank,
            )

        except EvidenceError as error:
            sources.append(
                SourceOutcome(
                    url=hit.url,
                    status="evidence_error",
                    detail=str(error)[:300],
                )
            )
            continue

        sources.append(
            SourceOutcome(
                url=hit.url,
                status=STATUS_STORED,
                evidence_id=record.id,
                final_url=page.url,
                title=record.title,
                char_count=record.char_count,
            )
        )

    return ResearchRun(
        query=outcome.query,
        provider=outcome.provider,
        hits=len(hits),
        sources=tuple(sources),
        latency_ms=round((time.perf_counter() - started) * 1000, 1),
        search_latency_ms=outcome.latency_ms,
    )


def _squash(text: str) -> str:
    return " ".join(text.split())


def cite_claim(
    store: EvidenceStore,
    *,
    report_key: str,
    claim_key: str,
    evidence_id: int,
    quote: str = "",
) -> CitationRecord:
    """把一条论断绑定到一条证据，并核对引文确实在正文里。

    `quote` 为空时只校验证据存在（适用于"整篇引用"）；
    给了 `quote` 就必须能在正文中按空白归一后找到。
    """

    text = store.get_text(evidence_id)

    if quote:
        if _squash(quote) not in _squash(text):
            raise CitationError(
                f"引文不在证据 {evidence_id} 的正文里"
            )

    return store.add_citation(
        report_key=report_key,
        claim_key=claim_key,
        source_id=evidence_id,
        quote=quote,
    )


def verify_citations(
    store: EvidenceStore,
    report_key: str,
) -> list[dict]:
    """逐条回查某个报告的全部引用，返回问题清单（空表示全部成立）。"""

    problems = []

    for citation in store.citations_for(report_key):
        record: EvidenceRecord | None = store.get_source(citation.source_id)

        if record is None:
            problems.append(
                {
                    "claim_key": citation.claim_key,
                    "source_id": citation.source_id,
                    "reason": "证据不存在",
                }
            )
            continue

        if citation.quote:
            text = store.get_text(citation.source_id)

            if _squash(citation.quote) not in _squash(text):
                problems.append(
                    {
                        "claim_key": citation.claim_key,
                        "source_id": citation.source_id,
                        "reason": "引文不在正文里",
                    }
                )

    return problems
