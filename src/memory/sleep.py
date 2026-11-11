"""Sleep-time Compute Phase 1：确定性候选、临时副本、检索 A/B。

安全合同（复审 v1.1 + 返修）：

- 生产库只读；写只发生在显式临时副本；
- 候选必须等于「同一 base 上重新生成的确定性 ops」，否则整批拒绝；
- supersedes 成环、缺失 evidence、矛盾 op 失败关闭；
- apply 在 BEGIN IMMEDIATE 内重新核对 base 与每行前置哈希；
- 单事务写事件；restore 只承诺条目 + FTS，events 不入快照；
- A/B 固定 as_of，Recall/MRR 任一下降即失败。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time

from contextlib import closing
from pathlib import Path

from src.memory.models import MemoryError, normalize_time, utc_now
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
    payload = {field: getattr(item, field) for field in FINGERPRINT_FIELDS}
    payload["deleted_at"] = item.deleted_at

    return _sha(payload)


def _dict_hash(item: dict) -> str:
    payload = {field: item.get(field) for field in FINGERPRINT_FIELDS}
    payload["deleted_at"] = item.get("deleted_at")

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
    """三表净内容哈希；缺表失败关闭。"""

    digest = hashlib.sha256()

    with closing(sqlite3.connect(db_path)) as connection:
        for table in ("memory_items", "memory_events", "memory_snapshots"):
            try:
                rows = connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                columns = [column[0] for column in connection.execute(
                    f"SELECT * FROM {table} LIMIT 0"
                ).description]
            except sqlite3.OperationalError as error:
                raise SleepError(f"缺少表：{table}") from error

            digest.update(table.encode())
            digest.update(str(len(rows)).encode())

            for row in rows:
                digest.update(_canonical(dict(zip(columns, row))).encode("utf-8"))

    return digest.hexdigest()


def replica_freeze(source_db: str | Path, replica_db: str | Path) -> dict:
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


def _supersede_cycles(items: list) -> set[int]:
    links = {item.id: item.supersedes_id for item in items if item.supersedes_id is not None}
    cycles: set[int] = set()

    for start in links:
        seen: list[int] = []
        current = start

        while current in links and current not in seen:
            seen.append(current)
            current = links[current]

        if current in seen:
            for node in seen[seen.index(current):]:
                cycles.add(node)

    return cycles


def propose(store, owner_id: str, *, as_of: str, snapshot) -> dict:
    as_of = normalize_time(as_of, field="as_of") or ""
    items = store.list_items(owner_id, include_deleted=True, limit=100000)
    active = [item for item in items if _active(item, as_of)]
    by_id = {item.id: item for item in items}
    cycles = _supersede_cycles(items)
    ops: list[dict] = []
    reports: dict = {
        "already_expired": 0,
        "possible_conflict": [],
        "orphan_supersedes": [],
        "duplicate_groups": 0,
        "supersede_chains": 0,
        "supersede_cycles": sorted(cycles),
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

        if old is None or old.owner_id != owner_id or old.deleted_at is not None:
            reports["orphan_supersedes"].append(new.id)
            continue

        if new.id in cycles or old.id in cycles:
            continue

        if not _active(old, as_of) or old.id in planned or old.valid_from > as_of:
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

    ops.sort(key=lambda op: (op["item_id"], op["reason"]))
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


def _ops_signature(ops: list[dict]) -> list[tuple]:
    return sorted(
        (
            op.get("item_id"),
            op.get("reason"),
            op.get("expected_sha256"),
            tuple(op.get("evidence_ids") or []),
        )
        for op in ops
        if isinstance(op, dict)
    )


def validate(store, candidate: dict, snapshot) -> dict:
    """候选语义校验：必须在同一 base 上重算并严格相等。"""

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

    try:
        normalized_as_of = normalize_time(as_of, field="as_of")
    except MemoryError:
        normalized_as_of = None
        errors.append("as_of_invalid")

    if normalized_as_of != as_of:
        errors.append("as_of_not_normalized")

    ops = candidate.get("ops")

    if not isinstance(ops, list):
        errors.append("ops")
        ops = []

    seen: set[int] = set()

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

    if not errors:
        expected = propose(store, owner_id, as_of=as_of, snapshot=snapshot)

        if _ops_signature(ops) != _ops_signature(expected["ops"]):
            errors.append("semantic_mismatch")

        if expected["reports"]["supersede_cycles"]:
            errors.append("supersede_cycle")

    return {"ok": not errors, "errors": errors, "ops": ops}


def _snapshot_matches(connection, snapshot_items: list[dict]) -> bool:
    rows = connection.execute("SELECT * FROM memory_items ORDER BY id").fetchall()
    columns = [column[0] for column in connection.execute(
        "SELECT * FROM memory_items LIMIT 0"
    ).description]
    current = {row[0]: dict(zip(columns, row)) for row in rows}

    if set(current) != {item["id"] for item in snapshot_items}:
        return False

    for item in snapshot_items:
        if _canonical(current[item["id"]]) != _canonical(item):
            return False

    return True


def apply(store, candidate: dict, snapshot) -> dict:
    """临时副本单事务 apply：base 与行哈希都在 BEGIN IMMEDIATE 内核对。"""

    check = validate(store, candidate, snapshot)

    if not check["ok"]:
        raise SleepError("候选校验失败：" + ", ".join(check["errors"]))

    versions = MemoryVersions(store)
    stored_snapshot, snapshot_items = versions.get(snapshot.id)

    if stored_snapshot.sha256 != snapshot.sha256:
        raise SleepError("快照 SHA 不一致")

    now = utc_now()
    applied: list[int] = []
    connection = sqlite3.connect(store.db_path, timeout=10)

    try:
        connection.execute("BEGIN IMMEDIATE")

        if not _snapshot_matches(connection, snapshot_items):
            raise SleepError("base_mismatch：当前副本已偏离 base snapshot")

        for op in check["ops"]:
            row = connection.execute(
                "SELECT * FROM memory_items WHERE id = ?", (op["item_id"],)
            ).fetchone()
            columns = [column[0] for column in connection.execute(
                "SELECT * FROM memory_items LIMIT 0"
            ).description]
            current = dict(zip(columns, row))

            if _dict_hash(current) != op["expected_sha256"]:
                raise SleepError(f"stale_item：{op['item_id']}")

            cursor = connection.execute(
                "UPDATE memory_items SET valid_to = ?, updated_at = ? "
                "WHERE id = ? AND owner_id = ? AND deleted_at IS NULL "
                "AND updated_at = ? AND (valid_to IS ?)",
                (
                    candidate["as_of"], now, op["item_id"], candidate["owner_id"],
                    current["updated_at"], current["valid_to"],
                ),
            )

            if cursor.rowcount != 1:
                raise SleepError(f"条件更新失败：{op['item_id']}")

            connection.execute(
                "INSERT INTO memory_events (item_id, action, actor, reason, at) "
                "VALUES (?,?,?,?,?)",
                (op["item_id"], "expire", candidate["owner_id"], f"sleep:{op['reason']}", now),
            )
            applied.append(op["item_id"])

        for item_id in applied:
            row = connection.execute(
                "SELECT valid_to FROM memory_items WHERE id = ?", (item_id,)
            ).fetchone()

            if row is None or row[0] != candidate["as_of"]:
                raise SleepError(f"提交前复核失败：{item_id}")

        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    return {"applied": applied, "count": len(applied), "as_of": candidate["as_of"]}


def restore(store, snapshot_id: int) -> dict:
    versions = MemoryVersions(store)
    result = versions.restore(snapshot_id)

    with closing(sqlite3.connect(store.db_path)) as connection:
        fts_count = connection.execute("SELECT COUNT(*) FROM memory_items_fts").fetchone()[0]
        live = connection.execute(
            "SELECT COUNT(*) FROM memory_items WHERE deleted_at IS NULL"
        ).fetchone()[0]

    result["items_restored"] = result.pop("restored")
    result["fts_view_restored"] = fts_count == live
    result["events_snapshot_included"] = False

    return result


def _latency(samples: list[float]) -> dict:
    ordered = sorted(samples)

    def percentile(ratio: float) -> float:
        if not ordered:
            return 0.0

        index = min(len(ordered) - 1, int(round((len(ordered) - 1) * ratio)))

        return ordered[index]

    return {"p50_ms": percentile(0.5), "p95_ms": percentile(0.95), "samples_ms": samples}


def ab_retrieval(store, owner_id: str, queries: list[dict], *, as_of: str, k: int = 5) -> dict:
    """检索层 A/B；显式固定 as_of（评测侧时钟注入）。"""

    import src.memory.store as store_module

    original = store_module.utc_now
    store_module.utc_now = lambda: as_of
    recall_hits = 0
    reciprocal = 0.0
    samples = []
    timings = []

    try:
        for spec in queries:
            expected = set(spec.get("expected_keys") or [])
            started = time.perf_counter()
            response = store.search(owner_id, spec["query"], k=k)
            timings.append((time.perf_counter() - started) * 1000)
            keys = [hit.item.key for hit in response["hits"]]
            hit = bool(expected & set(keys))
            rank = next((index + 1 for index, key in enumerate(keys) if key in expected), None)
            recall_hits += 1 if hit else 0
            reciprocal += (1.0 / rank) if rank else 0.0
            samples.append({
                "query": spec["query"],
                "expected_keys": sorted(expected),
                "top_keys": keys,
                "hit": hit,
                "rank": rank,
            })
    finally:
        store_module.utc_now = original

    total = len(queries)

    return {
        "recall_at_5": (recall_hits / total) if total else 0.0,
        "mrr": (reciprocal / total) if total else 0.0,
        "total": total,
        "samples": samples,
        "latency": _latency(timings),
        "as_of": as_of,
    }


def evaluate_ab(before: dict, after: dict, *, before_groups=None, after_groups=None) -> dict:
    reasons = []

    if after["recall_at_5"] < before["recall_at_5"]:
        reasons.append("recall_regressed")

    if after["mrr"] < before["mrr"]:
        reasons.append("mrr_regressed")

    if before_groups is not None and after_groups is not None:
        for name, before_group in before_groups.items():
            after_group = after_groups.get(name, {})

            if after_group.get("recall_at_5", 0.0) < before_group.get("recall_at_5", 0.0):
                reasons.append(f"group_recall_regressed:{name}")

            if after_group.get("mrr", 0.0) < before_group.get("mrr", 0.0):
                reasons.append(f"group_mrr_regressed:{name}")

            before_top1 = before_group.get("conflict_top1")
            after_top1 = after_group.get("conflict_top1")

            if before_top1 is not None and (after_top1 is None or after_top1 < before_top1):
                reasons.append(f"conflict_top1_regressed:{name}")

            if after_group.get("unanswerable_false_hits", 0) > 0:
                reasons.append(f"unanswerable_false_hits:{name}")

            if after_group.get("revived", 0) > 0:
                reasons.append(f"deleted_revived:{name}")

    return {
        "passed": not reasons, "reasons": reasons,
        "before": before, "after": after,
        "before_groups": before_groups, "after_groups": after_groups,
    }

def group_metrics(store, owner_id: str, groups: list[dict], *, as_of: str, k: int = 5) -> dict:
    """按组统计 recall / 冲突 top1 / 不可回答误命中 / 删除复活。"""

    import src.memory.store as store_module

    original = store_module.utc_now
    store_module.utc_now = lambda: as_of
    report = {}

    try:
        for group in groups:
            expected_hits = 0
            reciprocal = 0.0
            total = 0
            conflict_top1 = 0
            conflict_total = 0
            false_hits = 0
            revived = 0

            for spec in group.get("queries", []):
                expected = set(spec.get("expected_keys") or [])
                response = store.search(owner_id, spec["query"], k=k)
                keys = [hit.item.key for hit in response["hits"]]

                if spec.get("unanswerable"):
                    false_hits += 1 if keys else 0
                    continue

                total += 1
                hit = bool(expected & set(keys))
                expected_hits += 1 if hit else 0
                rank = next((index + 1 for index, key in enumerate(keys) if key in expected), None)
                reciprocal += (1.0 / rank) if rank else 0.0

                if expected:
                    conflict_total += 1
                    conflict_top1 += 1 if keys and keys[0] in expected else 0

                if spec.get("forbidden_keys"):
                    revived += len(set(keys) & set(spec["forbidden_keys"]))

            report[group["name"]] = {
                "total": total,
                "recall_at_5": (expected_hits / total) if total else 0.0,
                "mrr": (reciprocal / total) if total else 0.0,
                "conflict_top1": (conflict_top1 / conflict_total) if conflict_total else None,
                "unanswerable_false_hits": false_hits,
                "revived": revived,
            }
    finally:
        store_module.utc_now = original

    return report

def ab_paired(before_store, after_store, owner_id: str, queries: list[dict], *, as_of: str, rounds: int = 3) -> dict:
    """ABBA 交错多轮延迟：每轮 A,B,B,A；保存每轮原始样本。"""

    import src.memory.store as store_module

    as_of = normalize_time(as_of, field="as_of") or ""
    original = store_module.utc_now
    store_module.utc_now = lambda: as_of
    order = [("before", before_store), ("after", after_store), ("after", after_store), ("before", before_store)]
    samples = {"before": [], "after": []}

    try:
        for _ in range(max(1, int(rounds))):
            for arm, current in order:
                values = []

                for spec in queries:
                    started = time.perf_counter()
                    current.search(owner_id, spec["query"], k=5)
                    values.append((time.perf_counter() - started) * 1000)

                samples[arm].append(values)
    finally:
        store_module.utc_now = original

    deltas = []
    for index, (before_round, after_round) in enumerate(zip(samples["before"], samples["after"])):
        deltas.append([after - before for before, after in zip(before_round, after_round)])

    pooled_before = [value for round_values in samples["before"] for value in round_values]
    pooled_after = [value for round_values in samples["after"] for value in round_values]

    return {
        "rounds": max(1, int(rounds)),
        "order": [arm for arm, _ in order],
        "before_samples_ms": samples["before"],
        "after_samples_ms": samples["after"],
        "paired_deltas_ms": deltas,
        "before_latency": _latency(pooled_before),
        "after_latency": _latency(pooled_after),
    }
