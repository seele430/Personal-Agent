"""冲突解析：同一个 `conflict_group` 里，谁才是"当前有效的事实"。

规则只有一条主序 + 两级兜底，便于解释也便于测试：

1. **生效时间**：`valid_from` 最新者胜 —— "改主意"总是后写的算数；
2. **信任等级**：同一生效时间，`user_confirmed` > `platform_verified`
   > `external_untrusted` > `model_inferred`（§8.1）；
3. **写入顺序**：再相同就取 `created_at` 更晚、`id` 更大的那条。

**旧条目不会被删**：让它过期（`valid_to`）即可。要能回答"当初为什么这么定"，
就必须留痕；删掉旧版本等于把决策历史一起扔了。
"""

from __future__ import annotations

from dataclasses import dataclass

from src.memory.models import (
    MemoryError,
    MemoryItem,
    trust_rank,
    utc_now,
)


class ResolutionError(MemoryError):
    """冲突链异常（例如 supersedes 成环）。"""


@dataclass(frozen=True)
class Resolution:
    item: MemoryItem | None
    reason: str
    candidates: tuple[MemoryItem, ...] = ()

    @property
    def found(self) -> bool:
        return self.item is not None


def _sort_key(item: MemoryItem):
    return (
        item.valid_from,
        trust_rank(item.trust),
        item.created_at,
        item.id,
    )


def effective_items(
    items: list[MemoryItem],
    *,
    at: str | None = None,
) -> list[MemoryItem]:
    """筛出在 `at` 时刻生效的条目（未删除、已生效、未过期）。"""

    moment = at or utc_now()

    return [item for item in items if item.is_effective(moment)]


def resolve_current(
    items: list[MemoryItem],
    *,
    at: str | None = None,
) -> Resolution:
    """在一组同组条目里选出当前有效的唯一事实。"""

    candidates = effective_items(list(items), at=at)

    if not candidates:
        return Resolution(item=None, reason="没有生效中的条目", candidates=())

    ordered = sorted(candidates, key=_sort_key, reverse=True)
    winner = ordered[0]

    if len(ordered) == 1:
        reason = "组内唯一生效事实"

    elif ordered[1].valid_from < winner.valid_from:
        reason = f"生效时间最新（{winner.valid_from}）"

    elif trust_rank(ordered[1].trust) < trust_rank(winner.trust):
        reason = f"同生效时间按信任等级（{winner.trust}）"

    else:
        reason = "同生效时间同信任，按写入顺序取最新"

    return Resolution(
        item=winner, reason=reason, candidates=tuple(ordered)
    )


def resolve_group(
    store,
    group: str,
    *,
    owner_id: str | None = None,
    at: str | None = None,
) -> Resolution:
    """从存储里取某个冲突组并解析。"""

    if not isinstance(group, str) or not group.strip():
        raise ResolutionError("conflict_group 不能为空")

    items = store.list_items(
        owner_id or _single_owner(store), include_deleted=True, limit=500
    ) if owner_id else _all_items(store)

    grouped = [item for item in items if item.conflict_group == group]

    return resolve_current(grouped, at=at)


def resolve_for_key(
    store,
    owner_id: str,
    *,
    kind: str,
    key: str,
    at: str | None = None,
) -> Resolution:
    """按 `owner + kind + key` 找当前事实（不需要显式分组）。"""

    items = store.list_items(owner_id, kind=kind, include_deleted=True, limit=500)
    candidates = [item for item in items if item.key == key]

    return resolve_current(candidates, at=at)


def lineage(store, item_id: int, *, limit: int = 50) -> list[MemoryItem]:
    """沿 `supersedes_id` 回溯，返回从旧到新的链条。"""

    chain = []
    seen = set()
    current = store.get_item(item_id)

    while current is not None:
        if current.id in seen:
            raise ResolutionError(f"supersedes 链成环：{current.id}")

        seen.add(current.id)
        chain.append(current)

        if len(chain) > limit:
            raise ResolutionError("supersedes 链过长")

        if current.supersedes_id is None:
            break

        current = store.get_item(current.supersedes_id)

    return list(reversed(chain))


def explain(resolution: Resolution) -> dict:
    """给界面/日志用的解释：选谁、为什么、其它候选是谁。"""

    return {
        "chosen_id": None if resolution.item is None else resolution.item.id,
        "reason": resolution.reason,
        "candidates": [
            {
                "id": item.id,
                "trust": item.trust,
                "valid_from": item.valid_from,
                "valid_to": item.valid_to,
            }
            for item in resolution.candidates
        ],
    }


def _all_items(store) -> list[MemoryItem]:
    """没有 owner 时逐 owner 取（存储层要求 owner_id，这里做一次聚拢）。"""

    owners = store.list_owners() if hasattr(store, "list_owners") else []

    items = []

    for owner in owners:
        items.extend(store.list_items(owner, include_deleted=True, limit=500))

    return items


def _single_owner(store) -> str:
    owners = store.list_owners() if hasattr(store, "list_owners") else []

    if len(owners) != 1:
        raise ResolutionError("跨 owner 查询必须显式传 owner_id")

    return owners[0]
