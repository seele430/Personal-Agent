"""RSS / Atom 抓取。

安全约束（SSRF 防护）全部走 `src.monitors.url_policy`：

- 只允许 HTTPS，禁止带用户名密码的 URL；
- 抓取前解析域名，**任何一个**解析结果不是公网地址就拒绝；
- 不自动跟随重定向，**每一跳都重新做一次完整校验**
  （否则 https://公网站/302 → http://169.254.169.254/ 就绕过了防护）；
- 响应体积有上限，声明值与实际读取量都要检查。

异常口径故意保持不变：HTTP 错误仍抛 `requests.HTTPError`（带 `.response`），
超时 / 连接失败仍抛 requests 自己的异常 —— `rss_monitor` 依赖
`exc.response.status_code` 决定重试策略。策略违规抛 `UrlPolicyError`
（继承 ValueError），属于"不该重试"的配置类错误。
"""

from urllib.parse import urljoin

import feedparser
import requests

from src.monitors.url_policy import (
    UrlPolicyError,
    check_fetch_url,
)


MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
REQUEST_TIMEOUT = 15
CHUNK_SIZE = 64 * 1024

USER_AGENT = "PersonalAgent/0.1"

REDIRECT_STATUSES = frozenset({
    301, 302, 303, 307, 308,
})


def _declared_length(response) -> int | None:
    """读取 Content-Length；头不可信，解析失败就当没有。"""

    value = response.headers.get("Content-Length")

    if value is None:
        return None

    try:
        return int(value)

    except (TypeError, ValueError):
        return None


def _read_limited(response) -> bytes:
    """流式读取响应体，超过上限立即失败。"""

    declared = _declared_length(response)

    if declared is not None and declared > MAX_RESPONSE_BYTES:
        raise UrlPolicyError("RSS 响应体过大")

    chunks = []
    total = 0

    for chunk in response.iter_content(
        chunk_size=CHUNK_SIZE
    ):
        if not chunk:
            continue

        total += len(chunk)

        if total > MAX_RESPONSE_BYTES:
            raise UrlPolicyError("RSS 响应体过大")

        chunks.append(chunk)

    return b"".join(chunks)


def _parse_articles(content: bytes) -> list[dict]:
    feed = feedparser.parse(content)

    if feed.bozo and not feed.entries:
        raise ValueError("RSS 数据解析失败")

    articles = []

    for entry in feed.entries:
        article = {
            "id": entry.get(
                "id",
                entry.get("link", "")
            ),
            "title": entry.get(
                "title",
                "无标题"
            ),
            "link": entry.get(
                "link",
                ""
            ),
            "published": entry.get(
                "published",
                ""
            )
        }

        if article["id"]:
            articles.append(article)

    return articles


def fetch_rss(
    feed_url: str,
    *,
    resolver=None,
) -> list[dict]:
    """读取 RSS/Atom Feed，返回标准化文章数据。

    resolver 仅用于测试注入假 DNS；生产路径不传。
    """

    current = feed_url

    for _ in range(MAX_REDIRECTS + 1):

        # 每一跳都重新校验：重定向目标同样要做 SSRF 检查。
        check_fetch_url(current, resolver=resolver)

        response = requests.get(
            current,
            timeout=REQUEST_TIMEOUT,
            headers={
                "User-Agent": USER_AGENT
            },
            allow_redirects=False,
            stream=True,
        )

        try:
            if (
                response.is_redirect
                or response.status_code
                in REDIRECT_STATUSES
            ):
                location = response.headers.get(
                    "Location"
                )

                if not location:
                    raise ValueError(
                        "重定向响应缺少 Location"
                    )

                current = urljoin(current, location)
                continue

            response.raise_for_status()

            content = _read_limited(response)

        finally:
            response.close()

        return _parse_articles(content)

    raise UrlPolicyError("重定向次数过多")
