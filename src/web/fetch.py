"""网页抓取层：把 URL 变成"可抽取的页面"，并且只走一条安全路径。

**不重新实现 SSRF 防护**。`src/monitors/http_fetch.py` 已经有一条经过测试的
路径（策略校验 → 连接固定到已验证 IP → 逐跳复检重定向 → 体积上限），
这里复用它，只为"需要拿回状态码 / 媒体类型 / 最终 URL"这个额外需求
补一层包装。

复用的是 `http_fetch` 的模块级实现细节（`_pinned_session` / `_read_limited`），
这是**有意的**：多写一份就等于多一处可能写错的 SSRF 实现，
而那正是 `http_fetch` 文档里明确反对的。若将来 `http_fetch` 暴露了
公开的 `fetch_response()`，这里改调公开接口即可，行为不变。
"""

from __future__ import annotations

from urllib.parse import urljoin, urlsplit

import requests

from dataclasses import dataclass

from src.monitors.http_fetch import (
    MAX_REDIRECTS,
    MAX_RESPONSE_BYTES,
    REDIRECT_STATUSES,
    REQUEST_TIMEOUT,
    USER_AGENT,
    _pinned_session,
    _read_limited,
)

from src.monitors.url_policy import (
    UrlPolicyError,
    resolve_for_fetch,
)


ALLOWED_MEDIA_TYPES = frozenset({
    "text/html",
    "application/xhtml+xml",
    "text/plain",
})

ACCEPT_HEADER = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "text/plain;q=0.8"
)


class FetchError(RuntimeError):
    """抓取失败的基类 —— 失败必须显式，不能变成"空资料"。"""


class FetchBlocked(FetchError):
    """被 URL 策略拒绝（非 HTTPS、内网地址、重定向越界……）。"""


class FetchTooLarge(FetchError):
    """响应体超过上限。"""


class FetchUnsupportedType(FetchError):
    """媒体类型不在允许集合内（例如 PDF、图片、压缩包）。"""


class FetchHTTPError(FetchError):
    """对端返回了非 2xx。`status` 保留原始状态码。"""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


class FetchTimeout(FetchError):
    """请求超时。"""


@dataclass(frozen=True)
class FetchedPage:
    """一次抓取的结果。`url` 是**重定向之后**的最终地址。"""

    url: str
    status: int
    media_type: str
    body: bytes
    redirects: int
    etag: str = ""

    @property
    def content_length(self) -> int:
        return len(self.body)


def _media_type(headers) -> str:
    raw = (headers or {}).get("Content-Type") or ""

    return raw.split(";", 1)[0].strip().lower()


def fetch_page(
    url: str,
    *,
    resolver=None,
    max_bytes: int = MAX_RESPONSE_BYTES,
    timeout: float = REQUEST_TIMEOUT,
    allowed_media_types: frozenset[str] = ALLOWED_MEDIA_TYPES,
) -> FetchedPage:
    """按安全策略抓取一个页面。

    `resolver` 仅用于测试注入假 DNS，生产路径不传。
    任何一步失败都抛 `FetchError` 子类，**绝不返回空页面**。
    """

    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or max_bytes < 1
    ):
        raise ValueError("max_bytes 必须是正整数")

    current = url
    redirects = 0

    while True:
        try:
            host, addresses = resolve_for_fetch(current, resolver=resolver)

        except UrlPolicyError as error:
            raise FetchBlocked(str(error)) from error

        port = urlsplit(current).port or 443

        session = _pinned_session(
            host=host, connect_host=addresses[0], port=port
        )

        response = None

        try:
            try:
                response = session.get(
                    current,
                    timeout=timeout,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Host": host,
                        "Accept": ACCEPT_HEADER,
                        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    },
                    allow_redirects=False,
                    stream=True,
                )

            except requests.Timeout as error:
                raise FetchTimeout(f"抓取超时：{host}") from error

            except requests.RequestException as error:
                raise FetchError(f"抓取失败：{host}（{error}）") from error

            if (
                response.is_redirect
                or response.status_code in REDIRECT_STATUSES
            ):
                location = response.headers.get("Location")

                if not location:
                    raise FetchHTTPError(
                        "重定向响应缺少 Location",
                        response.status_code,
                    )

                redirects += 1

                if redirects > MAX_REDIRECTS:
                    raise FetchBlocked("重定向次数过多")

                current = urljoin(current, location)
                continue

            if response.status_code != 200:
                raise FetchHTTPError(
                    f"对端返回 HTTP {response.status_code}（{host}）",
                    response.status_code,
                )

            media_type = _media_type(response.headers)

            if (
                allowed_media_types
                and media_type not in allowed_media_types
            ):
                raise FetchUnsupportedType(
                    f"不支持的媒体类型：{media_type or '未声明'}"
                )

            try:
                body = _read_limited(response, max_bytes=max_bytes)

            except UrlPolicyError as error:
                raise FetchTooLarge(str(error)) from error

            return FetchedPage(
                url=current,
                status=response.status_code,
                media_type=media_type,
                body=body,
                redirects=redirects,
                etag=(response.headers.get("ETag") or "")[:128],
            )

        finally:
            if response is not None:
                response.close()

            session.close()
