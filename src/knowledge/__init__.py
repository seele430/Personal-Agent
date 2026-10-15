"""长期记忆 / 知识库（RAG）。

规划见 `docs/MEMORY_RAG_PLAN.md`：
- 摄取与索引（R1）：`ingest` + `store`
- BM25 基线（R2）：`retrieve`
- 向量与融合（R3，未实现）、注入与引用（R4，未实现）、
  观测与运维（R5，部分：检索审计已落地）
"""

from src.knowledge.chunker import chunk_text, normalize
from src.knowledge.ingest import (
    ingest_monitor_item,
    ingest_summary,
    ingest_upload,
)
from src.knowledge.retrieve import retrieve
from src.knowledge.store import (
    KnowledgeStore,
    content_hash,
)


__all__ = [
    "KnowledgeStore",
    "chunk_text",
    "content_hash",
    "ingest_monitor_item",
    "ingest_summary",
    "ingest_upload",
    "normalize",
    "retrieve",
]
