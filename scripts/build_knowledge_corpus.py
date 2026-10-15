"""构建知识库评测语料：从真实订阅源抓标题与摘要，固化成快照。

为什么要固化：评测必须**可复现**。订阅源的内容每天在变，
只有把"当时抓到的内容"提交进仓库，recall@k 的数字才可追溯、
才能在改动检索策略后做前后对比。

抓取走 `src.monitors.http_fetch`（与生产同一条硬化路径：
SSRF 策略 + IP 固定 + 体积上限），不在评测里另开网络出口。

用法：
    .\\.venv\\Scripts\\python.exe scripts\\build_knowledge_corpus.py
"""

import html
import json
import re
import sys

from pathlib import Path

import feedparser

# 让 `python scripts/xxx.py` 也能 import src：
# 直接跑脚本时 sys.path[0] 是 scripts/，不是仓库根目录。
ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.monitors.http_fetch import fetch_bytes


FEED_URL = "https://blog.python.org/rss.xml"
OUTPUT = Path("evals/knowledge/corpus.jsonl")
LIMIT = 40

TAGS = re.compile(r"<[^>]+>")
SPACES = re.compile(r"\s+")


def to_text(value: str) -> str:
    """去掉 HTML 标签与实体，压缩空白。"""

    text = TAGS.sub(" ", value or "")

    return SPACES.sub(" ", html.unescape(text)).strip()


def main() -> int:
    raw = fetch_bytes(FEED_URL, max_bytes=2 * 1024 * 1024)
    feed = feedparser.parse(raw)

    rows = []

    for index, entry in enumerate(feed.entries[:LIMIT], 1):
        title = to_text(entry.get("title", ""))
        summary = to_text(
            entry.get("summary")
            or entry.get("description")
            or ""
        )

        if not title:
            continue

        rows.append(
            {
                "id": f"doc_{index:02d}",
                "title": title,
                "url": entry.get("link", ""),
                "published": entry.get("published", ""),
                "text": (
                    f"{title}\n\n{summary}"
                    if summary
                    else title
                ),
            }
        )

    if not rows:
        print("没有抓到任何条目，中止（不覆盖已有语料）")
        return 1

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    with OUTPUT.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(
                json.dumps(row, ensure_ascii=False) + "\n"
            )

    with_summary = sum(
        1 for row in rows if row["text"] != row["title"]
    )

    print(f"已写入 {OUTPUT}：{len(rows)} 条")
    print(f"  带摘要的条目：{with_summary} / {len(rows)}")
    print(
        "  字符数区间：%d ~ %d"
        % (
            min(len(row["text"]) for row in rows),
            max(len(row["text"]) for row in rows),
        )
    )
    print(f"  来源：{FEED_URL}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
