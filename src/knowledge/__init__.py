"""长期记忆 / 知识库（RAG）。

规划见 `docs/MEMORY_RAG_PLAN.md`：
- 摄取与索引（R1）：`ingest` + `store`
- BM25 基线（R2）：`retrieve`
- 向量与融合（R3）：`embed` + `retrieve(mode='vector'|'hybrid')`
- 注入与引用（R4）：`inject`
- 观测与运维（R5，部分：检索审计 + 引用可回溯已落地）
"""

from src.knowledge.chunker import chunk_text, normalize
from src.knowledge.embed import (
    Embedder,
    HashingEmbedder,
    OpenAICompatibleEmbedder,
    build_embedder,
    cosine,
    index_embeddings,
)
from src.knowledge.ingest import (
    ingest_monitor_item,
    ingest_summary,
    ingest_upload,
)
from src.knowledge.inject import (
    InjectedContext,
    build_block,
    build_retrieval_context,
    estimate_tokens,
    map_citations,
    parse_citations,
)
from src.knowledge.retrieve import (
    RRF_K,
    fuse_rrf,
    retrieve,
)
from src.knowledge.store import (
    KnowledgeStore,
    content_hash,
)


__all__ = [
    "Embedder",
    "HashingEmbedder",
    "InjectedContext",
    "KnowledgeStore",
    "OpenAICompatibleEmbedder",
    "RRF_K",
    "build_block",
    "build_embedder",
    "build_retrieval_context",
    "chunk_text",
    "content_hash",
    "cosine",
    "estimate_tokens",
    "fuse_rrf",
    "index_embeddings",
    "ingest_monitor_item",
    "ingest_summary",
    "ingest_upload",
    "map_citations",
    "normalize",
    "parse_citations",
    "retrieve",
]
