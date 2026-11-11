"""Sleep-time Compute Phase 1：确定性候选、临时副本、检索 A/B。

严格边界（按复审 v1.1）：

- 生产库只读；所有写操作只发生在显式临时副本；
- 候选只做完整重复去重与显式 supersedes 过期；
- 同 key 不同正文只报告，不自动取代；
- base 快照 / 行哈希不匹配则整批失败关闭；
- apply 单事务（BEGIN IMMEDIATE），事件同事务写入；
- 快照不含 memory_events，回滚只承诺条目 + FTS 视图。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from contextlib import closing
from pathlib import Path

from src.memory.models import MemoryError, utc_now
from src.memory.versions import MemoryVersions


RULESET_VERSION = "sleep-deterministic-v1"
SCHEMA_VERSION = 1

FINGERPRINT_FIELDS = (
    "owner_id", "kind", "layer", "key", "value", "text",
    "trust", "source_ref", "source_doc_id",
    "valid_from", "valid_to", "supersedes_id", "conflict_group",
)


class SleepError(MemoryError):
    """候选非法、基线不匹配或发布失败。"""


def _canonical(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(payload) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _normalize(text: str) -> str:
    return " ".join(str(text).split())


def item_hash(item) -> str:
    """一条记忆的完整语义哈希（用于候选绑定与前置状态校验）。"""

    payload = {
        field: getattr(item, field)
        for field in FINGERPRINT_FIELDS
    }
    payload["deleted_at"] = item.deleted_at

    return _sha(payload)


def _fingerprint(item) -> str:
    payload = {field: getattr(item, field) for field in FINGERPRINT_FIELDS}
    payload["text"] = _normalize(item.text)
    payload["deleted_at"] = item.deleted_at

    return _sha(payload)


def _active(item, as_of: str) -> bool:
    return (
        item.deleted_at is None
        and item.valid_from <= as_of
        and (item.valid_to is None or item.valid_to > as_of)
    )


def source_digest(db_path: str | Path) -> str:
    """对记忆三表做内容哈希；用于证明源库没有被写。"""

    digest = hashlib.sha256()

    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row

        for table in ("memory_items", "memory_events", "memory_snapshots"):
            try:
                rows = connection.execute(
                    f"SELECT * FROM {table} ORDER BY rowid"
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []

            digest.update(table.encode())
            digest.update(str(len(rows)).encode())

            for row in rows:
                digest.update(_canonical(dict(row)).encode("utf-8"))

    return digest.hexdigest()


def replica_freeze(source_db: str | Path, replica_db: str | Path) -> dict:
    """SQLite backup API 建一致性副本；返回源库前后摘要。"""

    source = Path(source_db)
    replica = Path(replica_db)

    if not source.is_file():
        raise SleepError("源库不存在")

    if replica.exists():
        raise SleepError("副本已存在，拒绝覆盖")

    before = source_digest(source)
    replica.parent.mkdir(parents=True, exist_ok=True)

    with closing(sqlite3.connect(source)) as origin, closing(sqlite3.connect(replica)) as target:
        origin.backup(target)

    after = source_digest(source)

    if before != after:
        raise SleepError("源库在复制期间发生变化")

    return {
        "source": str(source),
        "replica": str(replica),
        "source_digest_before": before,
        "source_digest_after": after,
        "source_unchanged": before == after,
    }


def propose(store, owner_id: str, *, as_of: str, snapshot) -> dict:
    """生成确定性候选；只读，不写库。"""

    items = store.list_items(owner_id, include_deleted=True, limit=100000)
    active = [item for item in items if _active(item, as_of)]
    by_id = {item.id: item for item in items}
    ops: list[dict] = []
    reports: dict = {
        "already_expired": 0,
        "possible_conflict": [],
        "orphan_supersedes": [],
        "duplicate_groups": 0,
        "supersede_chains": 0,
    }

    for item in items:
        if item.deleted_at is not None:
            continue

        if item.valid_to is not None and item.valid_to <= as_of:
            reports["already_expired"] += 1

    planned: set[int] = set()
    groups: dict[str, list] = {}

    for item in active:
        groups.setdefault(_fingerprint(item), []).append(item)

    for group in groups.values():
        if len(group) < 2:
            continue

        group.sort(key=lambda item: item.id)
        keep = group[-1]
        reports["duplicate_groups"] += 1

        for duplicate in group[:-1]:
            planned.add(duplicate.id)
            ops.append({
                "op": "expire",
                "reason": "exact_dedupe",
                "item_id": duplicate.id,
                "expected_sha256": item_hash(duplicate),
                "evidence_ids": [keep.id],
            })

    for new in active:
        if new.supersedes_id is None:
            continue

        old = by_id.get(new.supersedes_id)

        if old is None:
            reports["orphan_supersedes"].append(new.id)
            continue

        if old.owner_id != owner_id or old.deleted_at is not None:
            reports["orphan_supersedes"].append(new.id)
            continue

        if not _active(old, as_of) or old.id in planned:
            continue

        if old.valid_from > as_of:
            continue

        ops.append({
            "op": "expire",
            "reason": "explicit_supersede",
            "item_id": old.id,
            "expected_sha256": item_hash(old),
            "evidence_ids": [new.id],
        })
        reports["supersede_chains"] += 1

    conflicts: dict[tuple, set] = {}

    for item in active:
        if not item.key:
            continue

        conflicts.setdefault((item.kind, item.key), set()).add(_normalize(item.text))

    reports["possible_conflict"] = [
        {"kind": kind, "key": key, "variants": len(texts)}
        for (kind, key), texts in sorted(conflicts.items())
        if len(texts) > 1
    ]

    candidate = {
        "schema_version": SCHEMA_VERSION,
        "ruleset_version": RULESET_VERSION,
        "base_snapshot_id": snapshot.id,
        "base_sha256": snapshot.sha256,
        "owner_id": owner_id,
        "as_of": as_of,
        "ops": ops,
        "reports": reports,
    }
    candidate["candidate_id"] = _sha(candidate)

    return candidate


def validate(store, candidate: dict, snapshot) -> dict:
    """发布前校验；返回 {ok, errors, ops}，不写库。"""

    errors: list[str] = []

    if candidate.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version")

    if candidate.get("ruleset_version") != RULESET_VERSION:
        errors.append("ruleset_version")

    if candidate.get("base_snapshot_id") != snapshot.id:
        errors.append("base_snapshot_id")

    if candidate.get("base_sha256") != snapshot.sha256:
        errors.append("base_sha256")

    body = {key: value for key, value in candidate.items() if key != "candidate_id"}

    if candidate.get("candidate_id") != _sha(body):
        errors.append("candidate_id")

    owner_id = candidate.get("owner_id")

    if not isinstance(owner_id, str) or not owner_id.strip():
        errors.append("owner_id")

    as_of = candidate.get("as_of")

    if not isinstance(as_of, str) or not as_of.strip():
        errors.append("as_of")

    seen: set[int] = set()
    ops = candidate.get("ops") or []

    if not isinstance(ops, list):
        errors.append("ops")
        ops = []

    for op in ops:
        if not isinstance(op, dict) or op.get("op") != "expire":
            errors.append("op_kind")
            continue

        item_id = op.get("item_id")

        if not isinstance(item_id, int) or item_id in seen:
            errors.append(f"item:{item_id}")
            continue

        seen.add(item_id)
        item = store.get_item(item_id)

        if item is None:
            errors.append(f"missing:{item_id}")
            continue

        if item.owner_id != owner_id:
            errors.append(f"owner:{item_id}")
            continue

        if item.deleted_at is not None:
            errors.append(f"deleted:{item_id}")
            continue

        if op.get("expected_sha256") != item_hash(item):
            errors.append(f"stale:{item_id}")
            continue

        if item.valid_from > as_of:
            errors.append(f"future:{item_id}")
            continue

        if item.valid_to is not None and item.valid_to <= as_of:
            errors.append(f"already_expired:{item_id}")
            continue

        if op.get("reason") not in ("exact_dedupe", "explicit_supersede"):
            errors.append(f"reason:{item_id}")

    return {"ok": not errors, "errors": errors, "ops": ops}


def apply(store, candidate: dict, snapshot) -> dict:
    """在临时副本上单事务 apply；失败整批回滚。"""

    check = validate(store, candidate, snapshot)

    if not check["ok"]:
        raise SleepError("候选校验失败：" + ", ".join(check["errors"]))

    now = utc_now()
    applied: list[int] = []
    connection = sqlite3.connect(store.db_path, timeout=10)

    try:
        connection.execute("BEGIN IMMEDIATE")

        for op in check["ops"]:
            cursor = connection.execute(
                "UPDATE memory_items SET valid_to = ?, updated_at = ? "
                "WHERE id = ? AND owner_id = ? AND deleted_at IS NULL "
                "AND (valid_to IS NULL OR valid_to > ?)",
                (candidate["as_of"], now, op["item_id"], candidate["owner_id"], candidate["as_of"]),
            )

            if cursor.rowcount != 1:
                raise SleepError(f"条件更新失败：{op['item_id']}")

            connection.execute(
                "INSERT INTO memory_events (item_id, action, actor, reason, at) "
                "VALUES (?,?,?,?,?)",
                (op["item_id"], "expire", candidate["owner_id"], f"sleep:{op['reason']}", now),
            )
            applied.append(op["item_id"])

        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    return {"applied": applied, "count": len(applied), "as_of": candidate["as_of"]}


def restore(store, snapshot_id: int) -> dict:
    """恢复条目与 FTS 视图；明确 events 不在快照内。"""

    versions = MemoryVersions(store)
    result = versions.restore(snapshot_id)
    result["events_snapshot_included"] = False

    return result


def ab_retrieval(store, owner_id: str, queries: list[dict], *, k: int = 5) -> dict:
    """检索层 A/B 指标（0 模型调用）。"""

    recall_hits = 0
    recall_total = 0
    reciprocal = 0.0
    samples = []

    for spec in queries:
        expected = set(spec.get("expected_keys") or [])
        response = store.search(owner_id, spec["query"], k=k)
        keys = [hit.item.key for hit in response["hits"]]
        hit = bool(expected & set(keys))
        rank = next((index + 1 for index, key in enumerate(keys) if key in expected), None)
        recall_total += 1
        recall_hits += 1 if hit else 0
        reciprocal += (1.0 / rank) if rank else 0.0
        samples.append({
            "query": spec["query"],
            "expected_keys": sorted(expected),
            "top_keys": keys,
            "hit": hit,
            "rank": rank,
        })

    return {
        "recall_at_5": (recall_hits / recall_total) if recall_total else 0.0,
        "mrr": (reciprocal / recall_total) if recall_total else 0.0,
        "total": recall_total,
        "samples": samples,
    }
