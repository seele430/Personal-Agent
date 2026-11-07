"""网页搜索 Provider 层。

只做一件事：把「一次查询」翻译成结构化的 `SearchHit`，并把所有失败
翻译成**显式异常**。搜索结果一律视为**不可信外部数据** —— 这里只做
长度与类型收敛，不做任何"看起来像指令就执行"的事。

错误分三类，调用方据此决定是否重试：

- `SearchAuthError`（401/403、未配置 key）：配置问题，**不重试**
- `SearchRateLimited`（429）：限流，可以退避后重试
- `SearchUnavailable`（超时、连接失败、5xx）：临时故障，可以重试
- `SearchPayloadError`（JSON 坏、结构不对）：对端变了，**不重试**

传输层可注入（`transport=`），所以单测完全离线。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from dataclasses import dataclass, replace
from typing import Callable, Protocol


BOCHA_ENDPOINT = "https://api.bochaai.com/v1/web-search"

PROVIDER_ENV = "WEB_SEARCH_PROVIDER"
API_KEY_ENV = "BOCHA_API_KEY"

MAX_QUERY_CHARS = 200
MAX_TITLE_CHARS = 200
MAX_SNIPPET_CHARS = 300
MAX_URL_CHARS = 2048
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_RESULTS = 10

DEFAULT_TIMEOUT = 20


class SearchError(RuntimeError):
    """搜索失败的基类。"""


class SearchAuthError(SearchError):
    """凭据缺失或被拒绝 —— 重试没有意义。"""


class SearchRateLimited(SearchError):
    """被限流（429）。"""


class SearchUnavailable(SearchError):
    """超时 / 连接失败 / 5xx。"""


class SearchPayloadError(SearchError):
    """响应不是预期的 JSON 结构。"""


@dataclass(frozen=True)
class SearchHit:
    """一条搜索结果。字段全部是不可信外部文本。"""

    title: str
    url: str
    snippet: str
    rank: int


@dataclass(frozen=True)
class SearchOutcome:
    """一次查询的结果 + 用量（供成本账本记录）。"""

    provider: str
    query: str
    hits: tuple[SearchHit, ...]
    latency_ms: float

    @property
    def count(self) -> int:
        return len(self.hits)


class SearchProvider(Protocol):
    """Provider 协议：只要能按查询返回一组结果。"""

    name: str

    def search(self, query: str, *, count: int = 5) -> SearchOutcome:
        ...


#: 传输函数签名：(url, headers, body, timeout) -> (status, payload_bytes)
Transport = Callable[[str, dict[str, str], bytes, float], tuple[int, bytes]]


def _http_post(
    url: str,
    headers: dict[str, str],
    body: bytes,
    timeout: float,
) -> tuple[int, bytes]:
    """默认传输：一次 POST，读回受限长度的响应体。

    4xx/5xx 不抛异常，交给调用方按状态码分类 —— 只有这样才能把
    401 与 503 区分成"配置错"和"临时故障"。
    """

    request = urllib.request.Request(
        url, data=body, method="POST", headers=headers
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)

            if len(payload) > MAX_RESPONSE_BYTES:
                raise SearchPayloadError("搜索响应过大")

            return response.status, payload

    except urllib.error.HTTPError as error:
        return error.code, b""

    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise SearchUnavailable(f"搜索请求失败：{error}") from error


def _clean_text(value, *, limit: int) -> str:
    """把任意输入收敛成一段安全文本。"""

    if not isinstance(value, str):
        return ""

    text = " ".join(value.split())

    return text[:limit]


def _clean_url(value) -> str:
    """只接受 http(s) 且长度受限的 URL，其余丢弃。"""

    if not isinstance(value, str):
        return ""

    text = value.strip()

    if not text or len(text) > MAX_URL_CHARS:
        return ""

    parts = urllib.parse.urlsplit(text)

    if parts.scheme.lower() not in {"http", "https"}:
        return ""

    if not parts.hostname:
        return ""

    return text


def normalize_query(query: str) -> str:
    """查询词校验与归一：去首尾空白、压空白、限长、拒空。"""

    if not isinstance(query, str):
        raise ValueError("查询词必须是字符串")

    text = " ".join(query.split())

    if not text:
        raise ValueError("查询词不能为空")

    if len(text) > MAX_QUERY_CHARS:
        raise ValueError(f"查询词过长（上限 {MAX_QUERY_CHARS} 字符）")

    return text


def normalize_count(count) -> int:
    """结果条数校验：1..MAX_RESULTS 的整数，bool 不算整数。"""

    if isinstance(count, bool) or not isinstance(count, int):
        raise ValueError("结果条数必须是整数")

    if not 1 <= count <= MAX_RESULTS:
        raise ValueError(f"结果条数必须在 1..{MAX_RESULTS} 之间")

    return count


class BochaSearchProvider:
    """博查 Web Search API 的实现。"""

    name = "bocha"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        endpoint: str = BOCHA_ENDPOINT,
        timeout: float = DEFAULT_TIMEOUT,
        transport: Transport | None = None,
    ):
        self._api_key = (
            api_key if api_key is not None else os.getenv(API_KEY_ENV, "")
        ).strip()
        self._endpoint = endpoint
        self._timeout = timeout
        self._transport = transport or _http_post

    def search(self, query: str, *, count: int = 5) -> SearchOutcome:
        text = normalize_query(query)
        limit = normalize_count(count)

        if not self._api_key:
            raise SearchAuthError(
                f"未配置 {API_KEY_ENV}，无法调用搜索 Provider"
            )

        payload = json.dumps(
            {"query": text, "summary": True, "count": limit},
            ensure_ascii=False,
        ).encode("utf-8")

        started = time.perf_counter()

        try:
            status, body = self._transport(
                self._endpoint,
                {
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                payload,
                self._timeout,
            )

        except SearchError:
            raise

        except (OSError, TimeoutError) as error:
            # 任何传输层实现（含注入的替身）抛网络类异常，统一翻译成
            # "临时故障"，调用方据此决定重试。
            raise SearchUnavailable(f"搜索请求失败：{error}") from error

        latency_ms = round((time.perf_counter() - started) * 1000, 1)

        if status in (401, 403):
            raise SearchAuthError(
                f"搜索 Provider 拒绝了凭据（HTTP {status}）"
            )

        if status == 429:
            raise SearchRateLimited("搜索 Provider 限流（HTTP 429）")

        if status >= 500:
            raise SearchUnavailable(
                f"搜索 Provider 服务端错误（HTTP {status}）"
            )

        if status != 200:
            raise SearchPayloadError(
                f"搜索 Provider 返回了非预期状态码（HTTP {status}）"
            )

        hits = self._parse(body)

        return SearchOutcome(
            provider=self.name,
            query=text,
            hits=hits,
            latency_ms=latency_ms,
        )

    def _parse(self, body: bytes) -> tuple[SearchHit, ...]:
        try:
            data = json.loads(body.decode("utf-8", "replace"))

        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SearchPayloadError("搜索响应不是合法 JSON") from error

        if not isinstance(data, dict):
            raise SearchPayloadError("搜索响应顶层不是对象")

        pages = data.get("data")

        if isinstance(pages, dict):
            pages = pages.get("webPages")

        if isinstance(pages, dict):
            pages = pages.get("value")

        if pages is None:
            return ()

        if not isinstance(pages, list):
            raise SearchPayloadError("搜索响应的结果列表结构异常")

        hits = []

        for rank, item in enumerate(pages, start=1):
            if not isinstance(item, dict):
                continue

            url = _clean_url(item.get("url"))

            if not url:
                # 没有可用 URL 的结果对下游没有意义，直接丢弃。
                continue

            snippet = _clean_text(
                item.get("summary") or item.get("snippet"),
                limit=MAX_SNIPPET_CHARS,
            )

            hits.append(
                SearchHit(
                    title=_clean_text(
                        item.get("name"), limit=MAX_TITLE_CHARS
                    ),
                    url=url,
                    snippet=snippet,
                    rank=rank,
                )
            )

        return tuple(hits)


class FakeSearchProvider:
    """离线替身：按预设返回结果或抛错，用于流水线与单测。"""

    name = "fake"

    def __init__(self, hits=(), *, error: Exception | None = None):
        self._hits = tuple(hits)
        self._error = error
        self.calls: list[tuple[str, int]] = []

    def search(self, query: str, *, count: int = 5) -> SearchOutcome:
        text = normalize_query(query)
        limit = normalize_count(count)

        self.calls.append((text, limit))

        if self._error is not None:
            raise self._error

        return SearchOutcome(
            provider=self.name,
            query=text,
            hits=self._hits[:limit],
            latency_ms=0.0,
        )


def provider_from_env() -> SearchProvider:
    """按 `WEB_SEARCH_PROVIDER` 选 Provider；默认博查。

    `fake` 只用于离线环境与演示，会明确报错说明它没有真实结果。
    """

    choice = os.getenv(PROVIDER_ENV, "bocha").strip().lower()

    if choice in ("", "bocha"):
        return BochaSearchProvider()

    if choice == "fake":
        return FakeSearchProvider(())

    raise ValueError(f"未知的搜索 Provider：{choice}")


def with_hits(outcome: SearchOutcome, hits) -> SearchOutcome:
    """替换结果集（便于测试构造场景）。"""

    return replace(outcome, hits=tuple(hits))
