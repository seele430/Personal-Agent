"""网页内容变化数据源（第二个 Source 实现）。

它存在的意义是证明引擎真的通用：这个源**复用与 RSS 完全相同**的
"基线 + 去重 + 恰好一次"语义，判断与动作那一层一行都没改。

item_id 用正文归一化后的 sha256：

- 同一内容重复抓取 → 同一个 id → seen_items 里去重 → 不重复通知；
- 内容变了 → 新 id → 恰好一条通知。

已知局限（诚实标注）：只做空白归一化，所以页面里的时间戳、广告位、
轮播切换这类"每次都不一样"的内容仍会产生噪音。要做成品级，
需要按选择器抽取正文；当前阶段先不做。
"""

import hashlib
import re

from src.core.task_engine import detect_new_items
from src.monitors.http_fetch import fetch_bytes


WHITESPACE = re.compile(r"\s+")

MAX_TEXT_BYTES = 512 * 1024


def normalize_text(content: bytes) -> str:
    """解码并压缩空白：让"只有排版变化"不被当成内容更新。"""

    text = content.decode("utf-8", errors="ignore")

    return WHITESPACE.sub(" ", text).strip()


def run_http_text_monitor(
    monitor_id: str,
    source_url: str,
) -> int:
    """抓取一个 URL，内容变化时产生一条通知。返回新增条数。"""

    content = fetch_bytes(
        source_url,
        max_bytes=MAX_TEXT_BYTES,
    )

    normalized = normalize_text(content)

    digest = hashlib.sha256(
        normalized.encode("utf-8")
    ).hexdigest()

    item = {
        "id": digest,
        "title": f"网页内容更新（{len(normalized)} 字符）",
        "link": source_url,
    }

    return len(detect_new_items(monitor_id, [item]))
