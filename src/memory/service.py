"""记忆服务：owner 绑定写入、当前事实召回、历史读取。"""

from __future__ import annotations

from src.memory.models import MemoryError, utc_now
from src.memory.store import MemoryStore, get_memory_store


def remember(
    owner_id: str,
    text: str,
    *,
    key: str = "",
    kind: str = "project_fact",
    layer: str = "project",
    trust: str = "user_confirmed",
    source_ref: str = "api:manual",
):
    return get_memory_store().remember(
        owner_id,
        text=text,
        key=key,
        kind=kind,
        layer=layer,
        trust=trust,
        source_ref=source_ref,
    )


def recall(owner_id: str, query: str, *, k: int = 5) -> dict:
    if not isinstance(query, str) or not query.strip():
        raise MemoryError("query 不能为空")

    if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= 50:
        raise MemoryError("k 必须是 1..50")

    return get_memory_store().search(owner_id, query.strip(), k=k)


def get_item(owner_id: str, item_id: int):
    store = get_memory_store()
    item = store.get_item(item_id)

    if item is None or item.owner_id != owner_id:
        return None

    return item
