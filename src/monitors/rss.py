from urllib.parse import urlparse

import requests
import feedparser


def fetch_rss(feed_url: str) -> list[dict]:
    """
    读取 RSS/Atom Feed，返回标准化文章数据。
    """
    parsed_url = urlparse(feed_url)

    if parsed_url.scheme != "https":
        raise ValueError("RSS 地址必须使用 HTTPS")

    response = requests.get(
        feed_url,
        timeout=15,
        headers={
            "User-Agent": "PersonalAgent/0.1"
        }
    )

    response.raise_for_status()

    feed = feedparser.parse(response.content)

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