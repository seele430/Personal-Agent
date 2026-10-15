"""检索入口：BM25 / 向量 / RRF 融合三条路。

- `bm25`：FTS5 trigram；1–2 字符查询退回逐词 LIKE（中文的坑见 store.py）。
- `vector`：把查询向量化后，在**该 owner 的向量子集**上暴力 cosine。
- `hybrid`：两条路各取一批，用 **RRF** 融合。

为什么是 RRF 而不是加权求和：
BM25 分数是无上界的、跟语料长度有关；cosine 落在 [-1, 1]。
两边的量纲根本不可比，加权求和等于在拿一个"凭感觉调的系数"去掩盖
"两把尺子不一样"这个事实。RRF 只用**名次**：

    score(d) = Σ_channels 1 / (K + rank_channel(d)),  K = 60

它对分数尺度免疫、无需调参、还天然偏向"两个通道都认可"的文档。
`K=60` 是原论文（Cormack et al., 2009）的常用取值，本文不改它。

每次检索都可以落一条审计（`knowledge_retrievals`）：
只存 query 的哈希、命中的 chunk id 与延迟，便于复盘
"引用是否命中"以及"这次检索到底花了多久"。
"""

import time

from src.knowledge.store import KnowledgeStore


SUPPORTED_MODES = ("bm25", "vector", "hybrid")

# RRF 的平滑常数：见模块文档。不暴露成参数，避免"随手调参"。
RRF_K = 60


def _ranked(hits) -> list[dict]:
    return [
        {**hit, "rank": index + 1}
        for index, hit in enumerate(hits)
    ]


def fuse_rrf(*channels) -> list[dict]:
    """RRF 融合：`channels` 是若干条已排名的命中列表。

    返回融合后的列表，保留 `score`（RRF 分）与 `channels`
    （这条命中被哪些通道找到过 —— 复盘"融合到底有没有用"要看它）。
    """

    scores = {}
    payload = {}
    sources = {}

    for name, hits in channels:
        for rank, hit in enumerate(hits, start=1):
            chunk_id = hit["chunk_id"]

            scores[chunk_id] = scores.get(chunk_id, 0.0) + (
                1.0 / (RRF_K + rank)
            )
            payload.setdefault(chunk_id, hit)
            sources.setdefault(chunk_id, []).append(name)

    ordered = sorted(
        scores.items(),
        key=lambda item: (-item[1], item[0]),
    )

    return [
        {
            **payload[chunk_id],
            "score": score,
            "channels": sources[chunk_id],
            "rank": index + 1,
        }
        for index, (chunk_id, score) in enumerate(ordered)
    ]


def retrieve(
    *,
    owner_id: str,
    query: str,
    k: int = 5,
    mode: str = "bm25",
    store: KnowledgeStore | None = None,
    record: bool = True,
    request_id: str | None = None,
    embedder=None,
) -> dict:
    """检索该用户的知识库，返回带排名的片段。

    `embedder` 只在 vector/hybrid 用到；不传就按环境配置挑一个
    （见 `src/knowledge/embed.build_embedder`）。
    """

    if mode not in SUPPORTED_MODES:
        raise ValueError(
            f"暂不支持的检索模式：{mode!r}，"
            f"当前可用：{list(SUPPORTED_MODES)}"
        )

    if type(k) is not int or not 1 <= k <= 50:
        raise ValueError("k 无效")

    store = store or KnowledgeStore()

    started = time.perf_counter()

    channel_modes = {}

    if mode == "bm25":
        bm25 = store.search_bm25(
            owner_id=owner_id,
            query=query,
            limit=k,
        )
        channel_modes["bm25"] = bm25["mode"]
        hits = _ranked(bm25["hits"])

    elif mode == "vector":
        hits, channel_modes = _vector_hits(
            store=store,
            owner_id=owner_id,
            query=query,
            k=k,
            embedder=embedder,
        )

    else:
        bm25 = store.search_bm25(
            owner_id=owner_id,
            query=query,
            limit=k,
        )
        channel_modes["bm25"] = bm25["mode"]

        vector_hits, vector_modes = _vector_hits(
            store=store,
            owner_id=owner_id,
            query=query,
            k=k,
            embedder=embedder,
        )
        channel_modes.update(vector_modes)

        hits = fuse_rrf(
            ("bm25", bm25["hits"]),
            ("vector", vector_hits),
        )[:k]

    latency_ms = int(
        (time.perf_counter() - started) * 1000
    )

    if record:
        store.record_retrieval(
            user_id=owner_id,
            query=query,
            mode=mode,
            k=k,
            latency_ms=latency_ms,
            top_chunk_ids=[
                hit["chunk_id"] for hit in hits
            ],
            request_id=request_id,
        )

    return {
        "mode": mode,
        "channels": channel_modes,
        "hits": hits,
        "latency_ms": latency_ms,
    }


def _vector_hits(
    *,
    store: KnowledgeStore,
    owner_id: str,
    query: str,
    k: int,
    embedder,
) -> tuple:
    """向量通道。返回 `(hits, channel_modes)`。"""

    if embedder is None:
        from src.knowledge.embed import build_embedder

        embedder = build_embedder()

    query_vector = embedder.embed_one(query)

    hits = store.search_vector(
        owner_id=owner_id,
        query_vector=query_vector,
        model=embedder.name,
        limit=k,
    )

    return (
        _ranked(hits),
        {"vector": embedder.name},
    )
