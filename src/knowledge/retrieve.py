"""检索入口。

当前只有一条通道：BM25（FTS5 trigram，1–2 字符查询退回 LIKE）。
R3 会增加向量通道与 RRF 融合，接口保持不变 —— 这也是把 `mode`
作为参数而不是写死的原因。

每次检索都可以落一条审计（`knowledge_retrievals`）：
只存 query 的哈希、命中的 chunk id 与延迟，便于复盘
"引用是否命中"以及"这次检索到底花了多久"。
"""

import time

from src.knowledge.store import KnowledgeStore


SUPPORTED_MODES = ("bm25",)


def retrieve(
    *,
    owner_id: str,
    query: str,
    k: int = 5,
    mode: str = "bm25",
    store: KnowledgeStore | None = None,
    record: bool = True,
    request_id: str | None = None,
) -> dict:
    """检索该用户的知识库，返回带排名的片段。"""

    if mode not in SUPPORTED_MODES:
        raise ValueError(
            f"暂不支持的检索模式：{mode!r}，"
            f"当前可用：{list(SUPPORTED_MODES)}"
        )

    if type(k) is not int or not 1 <= k <= 50:
        raise ValueError("k 无效")

    store = store or KnowledgeStore()

    started = time.perf_counter()

    result = store.search_bm25(
        owner_id=owner_id,
        query=query,
        limit=k,
    )

    latency_ms = int(
        (time.perf_counter() - started) * 1000
    )

    hits = [
        {**hit, "rank": index + 1}
        for index, hit in enumerate(result["hits"])
    ]

    if record:
        store.record_retrieval(
            user_id=owner_id,
            query=query,
            mode=result["mode"],
            k=k,
            latency_ms=latency_ms,
            top_chunk_ids=[
                hit["chunk_id"] for hit in hits
            ],
            request_id=request_id,
        )

    return {
        "mode": result["mode"],
        "hits": hits,
        "latency_ms": latency_ms,
    }
