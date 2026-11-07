"""网页研究能力（W1）：搜索 → 抓取 → 抽取 → 证据库 → 引用对账。

对外只暴露这条链上的稳定入口；具体实现细节留在各子模块。

    from src.web import run_research, cite_claim

    run = run_research("vllm pagedattention")
    for source in run.stored:
        print(source.evidence_id, source.url)
    for failure in run.failures:
        print(failure.status, failure.url)   # 失败必须看得见

安全口径：
- 抓取只走 `src/monitors/http_fetch` 那一条 SSRF 安全路径；
- 搜索结果、网页正文都是**不可信外部数据**，只用作文本，不进命令；
- 抽取失败抛异常，不会被伪装成"资料"。
"""

from src.web.evidence import (
    CitationRecord,
    EvidenceError,
    EvidenceRecord,
    EvidenceStore,
    get_evidence_store,
    normalize_url,
    reset_store,
)
from src.web.extract import (
    ExtractedText,
    ExtractError,
    decode_html,
    extract_text,
    non_space_length,
    text_sha256,
)
from src.web.fetch import (
    FetchedPage,
    FetchBlocked,
    FetchError,
    FetchHTTPError,
    FetchTimeout,
    FetchTooLarge,
    FetchUnsupportedType,
    fetch_page,
)
from src.web.pipeline import (
    CitationError,
    ResearchRun,
    SourceOutcome,
    cite_claim,
    run_research,
    verify_citations,
)
from src.web.search import (
    BochaSearchProvider,
    FakeSearchProvider,
    SearchAuthError,
    SearchError,
    SearchHit,
    SearchOutcome,
    SearchPayloadError,
    SearchProvider,
    SearchRateLimited,
    SearchUnavailable,
    normalize_count,
    normalize_query,
    provider_from_env,
)


__all__ = [
    "BochaSearchProvider",
    "CitationError",
    "CitationRecord",
    "EvidenceError",
    "EvidenceRecord",
    "EvidenceStore",
    "ExtractedText",
    "ExtractError",
    "FakeSearchProvider",
    "FetchBlocked",
    "FetchError",
    "FetchHTTPError",
    "FetchTimeout",
    "FetchTooLarge",
    "FetchUnsupportedType",
    "FetchedPage",
    "ResearchRun",
    "SearchAuthError",
    "SearchError",
    "SearchHit",
    "SearchOutcome",
    "SearchPayloadError",
    "SearchProvider",
    "SearchRateLimited",
    "SearchUnavailable",
    "SourceOutcome",
    "cite_claim",
    "decode_html",
    "extract_text",
    "fetch_page",
    "get_evidence_store",
    "non_space_length",
    "normalize_count",
    "normalize_query",
    "normalize_url",
    "provider_from_env",
    "reset_store",
    "run_research",
    "text_sha256",
    "verify_citations",
]
