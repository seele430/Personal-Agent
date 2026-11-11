"""记忆服务：owner 绑定写入、当前事实召回、历史读取。"""

from __future__ import annotations

import sqlite3

from contextlib import closing

from src.memory.models import MemoryError, utc_now
from src.memory.resolve import resolve_current
from src.memory.store import get_memory_store


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

class HistoryError(MemoryError):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def _public(item) -> dict:
    return {
        "id": item.id, "key": item.key, "kind": item.kind, "layer": item.layer,
        "text": item.text, "trust": item.trust,
        "valid_from": item.valid_from, "valid_to": item.valid_to,
        "supersedes_id": item.supersedes_id, "created_at": item.created_at,
    }


def list_current(owner_id: str, *, limit: int = 20, offset: int = 0) -> dict:
    store = get_memory_store()
    items = store.list_items(owner_id, include_deleted=True, limit=100000)
    moment = utc_now()
    groups: dict[str, list] = {}
    current = []

    for item in items:
        if not item.is_effective(moment):
            continue

        if item.conflict_group:
            groups.setdefault(item.conflict_group, []).append(item)
        else:
            current.append(item)

    for members in groups.values():
        resolution = resolve_current(members, at=moment)

        if resolution.item is not None and resolution.item.is_effective(moment):
            current.append(resolution.item)

    current.sort(key=lambda item: item.id, reverse=True)
    total = len(current)
    page = current[offset:offset + limit]

    return {
        "mode": "list",
        "items": [_public(item) for item in page],
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < total,
    }


def history(owner_id: str, item_id: int, *, limit: int = 50) -> dict:
    store = get_memory_store()
    anchor = store.get_item(item_id)

    if anchor is None or anchor.owner_id != owner_id:
        raise HistoryError("memory_not_found")

    items = store.list_items(owner_id, include_deleted=True, limit=100000)
    by_id = {item.id: item for item in items}
    visited = {anchor.id}
    ancestors = []
    current = anchor

    while current.supersedes_id is not None:
        parent_id = current.supersedes_id
        parent = by_id.get(parent_id)

        if parent is None:
            other = store.get_item(parent_id)

            if other is not None and other.owner_id != owner_id:
                raise HistoryError("history_owner_mismatch")

            raise HistoryError("history_broken")

        if parent.id in visited:
            raise HistoryError("history_cycle")

        visited.add(parent.id)
        ancestors.append(parent)
        current = parent

    children: dict[int, list] = {}

    for item in items:
        if item.supersedes_id is not None:
            children.setdefault(item.supersedes_id, []).append(item)

    descendants = []
    current = anchor

    while children.get(current.id):
        options = children[current.id]

        if len(options) > 1:
            raise HistoryError("history_forked")

        child = options[0]

        if child.id in visited:
            raise HistoryError("history_cycle")

        visited.add(child.id)
        descendants.append(child)
        current = child

    chain = list(reversed(ancestors)) + [anchor] + descendants

    for node in chain:
        if len(children.get(node.id, [])) > 1:
            raise HistoryError("history_forked")

    with closing(sqlite3.connect(store.db_path)) as connection:
        placeholders = ",".join("?" for _ in chain)
        rows = connection.execute(
            f"SELECT id, owner_id FROM memory_items WHERE supersedes_id IN ({placeholders})",
            [node.id for node in chain],
        ).fetchall()

        for _child_id, child_owner in rows:
            if child_owner != owner_id:
                raise HistoryError("history_owner_mismatch")

    if len(chain) > 500:
        raise HistoryError("history_too_long")

    total = len(chain)
    truncated = total > limit
    anchor_index = chain.index(anchor)

    if truncated:
        start = max(0, anchor_index - limit // 2)
        start = min(start, total - limit)
        window = chain[start:start + limit]
        truncation_reason = "limit"
    else:
        window = chain
        truncation_reason = ""

    moment = utc_now()

    if anchor.conflict_group:
        group_items = [item for item in items if item.conflict_group == anchor.conflict_group]
        resolution = resolve_current(group_items, at=moment)
        current_id = resolution.item.id if resolution.item is not None else None
    else:
        current_id = anchor.id if anchor.is_effective(moment) and anchor.deleted_at is None else None
    now = utc_now()
    rows = []

    for item in window:
        if item.deleted_at is not None:
            status = "deleted"
        elif item.valid_to is not None and item.valid_to <= now:
            status = "expired"
        elif item.valid_from > now:
            status = "future"
        elif item.id == current_id:
            status = "current"
        else:
            status = "superseded"

        rows.append({
            **_public(item), "deleted_at": item.deleted_at,
            "is_current": item.id == current_id, "version_status": status,
        })

    return {
        "anchor_id": anchor.id, "current_id": current_id, "items": rows,
        "count": len(rows), "total_count": total, "truncated": truncated,
        "truncation_reason": truncation_reason,
        "window_has_current": current_id is not None and any(row["id"] == current_id for row in rows),
    }

def delete(owner_id: str, item_id: int) -> bool:
    """删除一条记忆链：有 conflict_group 时软删整组，避免旧版复活。"""

    store = get_memory_store()
    item = store.get_item(item_id)

    if item is None or item.owner_id != owner_id:
        return False

    if item.conflict_group:
        members = [
            member for member in store.list_items(owner_id, include_deleted=False, limit=100000)
            if member.conflict_group == item.conflict_group
        ]
    else:
        members = [item]

    for member in members:
        store.delete_item(member.id)

    return True
