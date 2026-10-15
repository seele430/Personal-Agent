"""RSS / Atom 数据源。

抓取本身（SSRF 策略、IP 固定、逐跳复检、体积上限）全部在
`src.monitors.http_fetch` —— 所有数据源共用同一条硬化路径。
这里只负责"把响应体解析成标准化条目"。

`MAX_RESPONSE_BYTES` 在本模块仍然作为公开名字保留：
调用方可以在调用前覆盖它来收紧上限。
"""

import feedparser

from src.monitors.http_fetch import (
    MAX_RESPONSE_BYTES,
    fetch_bytes,
)


__all__ = [
    "MAX_RESPONSE_BYTES",
    "fetch_rss",
]


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

    content = fetch_bytes(
        feed_url,
        resolver=resolver,
        max_bytes=MAX_RESPONSE_BYTES,
    )

    return _parse_articles(content)
