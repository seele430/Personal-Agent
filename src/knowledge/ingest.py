"""产品级摄取入口：把三类语料规范化后写进知识库。

三类来源对应 `docs/MEMORY_RAG_PLAN.md` 里的语料设计：

- `monitor`：监控抓回的网页正文（此前只存标题与链接，正文抓完就丢）
- `upload`：用户上传的文件（先支持纯文本 / Markdown）
- `summary`：会话摘要（不把全量历史塞进索引）
"""

from src.knowledge.store import KnowledgeStore


def ingest_monitor_item(
    *,
    owner_id: str,
    item_id: str,
    body: str,
    title: str | None = None,
    url: str | None = None,
    store: KnowledgeStore | None = None,
) -> dict:
    """把一条监控抓到的正文存进知识库。

    幂等键是正文内容本身：同一篇文章被重复抓到（或换了个链接）
    都只会留一条文档。
    """

    if not isinstance(item_id, str) or not item_id.strip():
        raise ValueError("item_id 不能为空")

    return (store or KnowledgeStore()).ingest_document(
        owner_id=owner_id,
        text=body,
        source_type="monitor",
        title=title,
        url=url,
        source_ref=item_id.strip(),
    )


def ingest_upload(
    *,
    owner_id: str,
    filename: str,
    text: str,
    store: KnowledgeStore | None = None,
) -> dict:
    """把用户上传的文件内容存进知识库。"""

    if not isinstance(filename, str) or not filename.strip():
        raise ValueError("文件名不能为空")

    return (store or KnowledgeStore()).ingest_document(
        owner_id=owner_id,
        text=text,
        source_type="upload",
        title=filename.strip(),
        source_ref=filename.strip(),
    )


def ingest_summary(
    *,
    owner_id: str,
    request_id: str,
    text: str,
    store: KnowledgeStore | None = None,
) -> dict:
    """把一段会话摘要存进知识库。"""

    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError("request_id 不能为空")

    return (store or KnowledgeStore()).ingest_document(
        owner_id=owner_id,
        text=text,
        source_type="summary",
        title="会话摘要",
        source_ref=request_id.strip(),
    )
