"""记忆条目存储：同一个 `knowledge.db` 里的新表，不动既有 11 张表。

三件事必须一起成立，否则"删除"是假的：

1. 主表标记（`deleted_at`）；
2. FTS 索引行删除（否则检索仍能命中）；
3. 冲突组解绑（否则解析时还可能被当成候选）。

检索的中文口径照抄 `src/knowledge/store.py` 的实测结论：
FTS5 `trigram` 需要 ≥3 个字符，1–2 字查询退 `LIKE` 兜底，并**如实标注** `mode`。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3

from contextlib import closing
from pathlib import Path

from src.memory.models import (
    MAX_GROUP_CHARS,
    MAX_REF_CHARS,
    MemoryError,
    MemoryEvent,
    MemoryHit,
    MemoryItem,
    normalize_time,
    trusted_at_least,
    utc_now,
    validate_kind,
    validate_layer,
    validate_owner,
    validate_short,
    validate_text,
    validate_trust,
)
from src.memory.resolve import resolve_current


MIN_FTS_QUERY_CHARS = 3

#: shingle 兜底的阈值：覆盖率 ≥50% 且至少命中 2 个 shingle 才算候选。
#: 这两个数是为了挡"只碰到一个高频词就当成命中"，不是调参空间。
SHINGLE_MIN_COVERAGE = 0.5
SHINGLE_MIN_MATCHED = 2
SHINGLE_MAX_CANDIDATES = 500

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id      TEXT    NOT NULL,
    kind          TEXT    NOT NULL
                  CHECK (kind IN ('preference','project_fact','constraint','episode')),
    layer         TEXT    NOT NULL
                  CHECK (layer IN ('identity','project','session')),
    trust         TEXT    NOT NULL
                  CHECK (trust IN ('user_confirmed','platform_verified',
                                   'external_untrusted','model_inferred')),
    key           TEXT    NOT NULL DEFAULT '',
    value         TEXT    NOT NULL DEFAULT '',
    text          TEXT    NOT NULL,
    source_ref    TEXT    NOT NULL DEFAULT '',
    source_doc_id INTEGER,
    valid_from    TEXT    NOT NULL,
    valid_to      TEXT,
    supersedes_id INTEGER,
    conflict_group TEXT   NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    deleted_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_memory_owner
    ON memory_items (owner_id, kind, layer);
CREATE INDEX IF NOT EXISTS idx_memory_group
    ON memory_items (conflict_group);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_items_fts
    USING fts5 (text, tokenize='trigram');
CREATE TABLE IF NOT EXISTS memory_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL,
    action  TEXT    NOT NULL,
    actor   TEXT    NOT NULL DEFAULT '',
    reason  TEXT    NOT NULL DEFAULT '',
    at      TEXT    NOT NULL
);
"""

_TABLE_NAMES = (
    "memory_items",
    "memory_items_fts",
    "memory_events",
)


def _terms(query: str) -> list[str]:
    parts = [
        part
        for part in re.split(r"[\s,，。;；:：!！?？()（）\[\]]+", query)
        if part
    ]

    return parts or [query]


def _match_expression(query: str) -> str:
    return " AND ".join(
        '"%s"' % term.replace('"', '""') for term in _terms(query)
    )


class MemoryStore:
    def __init__(self, db_path: str | Path | None = None):
        from src import config

        self.db_path = Path(db_path or config.paths.knowledge_db)
        self.init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        with closing(self._connect()) as connection, connection:
            connection.executescript(SCHEMA)

    # ---- 写入 -------------------------------------------------------

    def add_item(
        self,
        owner_id: str,
        *,
        kind: str,
        layer: str,
        trust: str,
        text: str,
        key: str = "",
        value: str = "",
        source_ref: str = "",
        source_doc_id: int | None = None,
        valid_from: str | None = None,
        valid_to: str | None = None,
        conflict_group: str = "",
        supersedes_id: int | None = None,
    ) -> MemoryItem:
        owner_id = validate_owner(owner_id)
        kind = validate_kind(kind)
        layer = validate_layer(layer)
        trust = validate_trust(trust)
        text = validate_text(text)
        key = validate_short(key, field="key", limit=200)
        value = validate_short(value, field="value", limit=2000)
        source_ref = validate_short(
            source_ref, field="source_ref", limit=MAX_REF_CHARS
        )
        conflict_group = validate_short(
            conflict_group, field="conflict_group", limit=MAX_GROUP_CHARS
        )

        starts = normalize_time(valid_from, field="valid_from") or utc_now()
        ends = normalize_time(valid_to, field="valid_to")

        if ends is not None and ends <= starts:
            raise MemoryError("valid_to 必须晚于 valid_from")

        if supersedes_id is not None:
            if self.get_item(supersedes_id) is None:
                raise MemoryError(f"被取代的条目不存在：{supersedes_id}")

        stamp = utc_now()

        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "INSERT INTO memory_items ("
                " owner_id, kind, layer, trust, key, value, text,"
                " source_ref, source_doc_id, valid_from, valid_to,"
                " supersedes_id, conflict_group, created_at, updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    owner_id,
                    kind,
                    layer,
                    trust,
                    key,
                    value,
                    text,
                    source_ref,
                    source_doc_id,
                    starts,
                    ends,
                    supersedes_id,
                    conflict_group,
                    stamp,
                    stamp,
                ),
            )

            item_id = int(cursor.lastrowid)

            connection.execute(
                "INSERT INTO memory_items_fts (rowid, text) VALUES (?, ?)",
                (item_id, text),
            )

            self._record_event(
                connection, item_id, "add", actor=owner_id, reason=""
            )

        return self.get_item(item_id)

    def remember(
        self,
        owner_id: str,
        *,
        text: str,
        kind: str = "project_fact",
        layer: str = "project",
        key: str = "",
        trust: str = "user_confirmed",
        source_ref: str = "",
    ) -> MemoryItem:
        """同 key 原子更新：选当前条、写新条、设分组与 supersedes。

        全过程在 BEGIN IMMEDIATE 内完成，避免两个并发写手各指向同一个旧条。
        """

        owner_id = validate_owner(owner_id)
        kind = validate_kind(kind)
        layer = validate_layer(layer)
        trust = validate_trust(trust)
        text = validate_text(text)
        key = validate_short(key, field="key", limit=200)
        source_ref = validate_short(source_ref, field="source_ref", limit=MAX_REF_CHARS)
        now = utc_now()
        stamp = now

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")

            try:
                old = None
                group = ""

                if key:
                    rows = connection.execute(
                        "SELECT * FROM memory_items WHERE owner_id=? AND kind=? AND key=? "
                        "AND deleted_at IS NULL AND valid_from<=? "
                        "AND (valid_to IS NULL OR valid_to>?)",
                        (owner_id, kind, key, now, now),
                    ).fetchall()
                    members = [_to_item(row) for row in rows]
                    groups = {member.conflict_group for member in members if member.conflict_group}

                    if len(groups) > 1:
                        raise MemoryError("同 key 条目分组冲突，拒绝自动合并")

                    group = next(iter(groups)) if groups else (
                        "key-v1:" + kind + ":" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
                    )

                    for member in members:
                        if not member.conflict_group:
                            connection.execute(
                                "UPDATE memory_items SET conflict_group=?, updated_at=? WHERE id=?",
                                (group, stamp, member.id),
                            )
                            self._record_event(
                                connection, member.id, "group_bind", actor=owner_id, reason="same-key"
                            )

                    if members:
                        old = resolve_current(members, at=now).item

                cursor = connection.execute(
                    "INSERT INTO memory_items ("
                    " owner_id, kind, layer, trust, key, value, text,"
                    " source_ref, source_doc_id, valid_from, valid_to,"
                    " supersedes_id, conflict_group, created_at, updated_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        owner_id, kind, layer, trust, key, "", text,
                        source_ref, None, now, None,
                        old.id if old is not None else None, group, stamp, stamp,
                    ),
                )
                item_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO memory_items_fts (rowid, text) VALUES (?, ?)",
                    (item_id, text),
                )
                self._record_event(connection, item_id, "add", actor=owner_id, reason="manual")
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        return self.get_item(item_id)

    def delete_group(self, owner_id: str, group: str) -> int:
        """单事务软删整组；任一失败整体回滚。"""

        if not group:
            raise MemoryError("group 不能为空")

        now = utc_now()
        deleted: list[int] = []

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")

            try:
                rows = connection.execute(
                    "SELECT id FROM memory_items WHERE owner_id=? AND conflict_group=? "
                    "AND deleted_at IS NULL",
                    (owner_id, group),
                ).fetchall()

                for row in rows:
                    item_id = int(row[0])
                    connection.execute(
                        "UPDATE memory_items SET deleted_at=?, updated_at=?, conflict_group='' WHERE id=?",
                        (now, now, item_id),
                    )
                    connection.execute(
                        "DELETE FROM memory_items_fts WHERE rowid=?", (item_id,)
                    )
                    self._record_event(
                        connection, item_id, "delete", actor=owner_id, reason="group-delete"
                    )
                    deleted.append(item_id)

                connection.commit()
            except Exception:
                connection.rollback()
                raise

        return len(deleted)

    def expire_item(
        self,
        item_id: int,
        *,
        at: str | None = None,
        reason: str = "",
    ) -> MemoryItem:
        """让条目在 `at` 之后失效（不删除，保留历史）。"""

        item = self.get_item(item_id)

        if item is None:
            raise MemoryError(f"记忆条目不存在：{item_id}")

        moment = normalize_time(at, field="at") or utc_now()

        if moment < item.valid_from:
            raise MemoryError("失效时间必须晚于生效时间")

        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE memory_items SET valid_to = ?, updated_at = ? "
                "WHERE id = ?",
                (moment, utc_now(), item_id),
            )
            self._record_event(
                connection, item_id, "expire", actor=item.owner_id, reason=reason
            )

        return self.get_item(item_id)

    def delete_item(self, item_id: int, *, purge: bool = False) -> bool:
        """删除条目，并清掉派生数据（FTS 行、冲突组）。

        `purge=True` 时物理删除；否则软删（保留 `deleted_at` 供审计）。
        """

        item = self.get_item(item_id)

        if item is None:
            return False

        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM memory_items_fts WHERE rowid = ?", (item_id,))

            if purge:
                connection.execute("DELETE FROM memory_items WHERE id = ?", (item_id,))
                action = "purge"

            else:
                connection.execute(
                    "UPDATE memory_items SET deleted_at = ?, updated_at = ?,"
                    " conflict_group = '' WHERE id = ?",
                    (utc_now(), utc_now(), item_id),
                )
                action = "delete"

            self._record_event(
                connection, item_id, action, actor=item.owner_id, reason=""
            )

        return True

    def _record_event(self, connection, item_id, action, *, actor, reason):
        connection.execute(
            "INSERT INTO memory_events (item_id, action, actor, reason, at) "
            "VALUES (?,?,?,?,?)",
            (item_id, action, actor or "", reason[:300], utc_now()),
        )

    # ---- 读取 -------------------------------------------------------

    def get_item(self, item_id: int) -> MemoryItem | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM memory_items WHERE id = ?", (item_id,)
            ).fetchone()

        return _to_item(row) if row is not None else None

    def get_by_key(self, owner_id: str, *, kind: str, key: str) -> MemoryItem | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM memory_items WHERE owner_id = ? AND kind = ? "
                "AND key = ? AND deleted_at IS NULL ORDER BY id DESC LIMIT 1",
                (validate_owner(owner_id), kind, key),
            ).fetchone()

        return _to_item(row) if row is not None else None

    def list_items(
        self,
        owner_id: str,
        *,
        kind: str | None = None,
        layer: str | None = None,
        include_deleted: bool = False,
        limit: int = 200,
    ) -> list[MemoryItem]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")

        clauses = ["owner_id = ?"]
        params: list = [validate_owner(owner_id)]

        if kind is not None:
            clauses.append("kind = ?")
            params.append(validate_kind(kind))

        if layer is not None:
            clauses.append("layer = ?")
            params.append(validate_layer(layer))

        if not include_deleted:
            clauses.append("deleted_at IS NULL")

        params.append(limit)

        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM memory_items WHERE "
                + " AND ".join(clauses)
                + " ORDER BY id LIMIT ?",
                params,
            ).fetchall()

        return [_to_item(row) for row in rows]

    def count_items(self, owner_id: str | None = None, *, include_deleted=False) -> int:
        clauses = []
        params: list = []

        if owner_id is not None:
            clauses.append("owner_id = ?")
            params.append(validate_owner(owner_id))

        if not include_deleted:
            clauses.append("deleted_at IS NULL")

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM memory_items" + where, params
            ).fetchone()

        return int(row["n"])

    def list_owners(self, *, include_deleted: bool = False) -> list[str]:
        """列出出现过的 owner（评测与快照用）。"""

        sql = "SELECT DISTINCT owner_id FROM memory_items"

        if not include_deleted:
            sql += " WHERE deleted_at IS NULL"

        sql += " ORDER BY owner_id"

        with closing(self._connect()) as connection:
            rows = connection.execute(sql).fetchall()

        return [row["owner_id"] for row in rows]

    def fts_count(self) -> int:
        """FTS 索引里还有多少行 —— 删条目后必须跟着减。"""

        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM memory_items_fts"
            ).fetchone()

        return int(row["n"])

    def events(self, item_id: int | None = None) -> list[MemoryEvent]:
        query = "SELECT * FROM memory_events"
        params: list = []

        if item_id is not None:
            query += " WHERE item_id = ?"
            params.append(item_id)

        query += " ORDER BY id"

        with closing(self._connect()) as connection:
            rows = connection.execute(query, params).fetchall()

        return [
            MemoryEvent(
                id=int(row["id"]),
                item_id=int(row["item_id"]),
                action=row["action"],
                actor=row["actor"],
                reason=row["reason"],
                at=row["at"],
            )
            for row in rows
        ]

    # ---- 检索 -------------------------------------------------------

    def search(
        self,
        owner_id: str,
        query: str,
        *,
        k: int = 5,
        kind: str | None = None,
        layer: str | None = None,
        min_trust: str | None = None,
        include_expired: bool = False,
        include_future: bool = False,
    ) -> dict:
        """按关键词检索记忆条目，返回 `{"mode": "fts"|"like", "hits": [...]}`。

        默认只返回：未删除、已生效、未过期、达到 `min_trust` 的条目。
        """

        owner_id = validate_owner(owner_id)

        if not isinstance(query, str) or not query.strip():
            raise ValueError("查询不能为空")

        if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= 50:
            raise ValueError("k 无效")

        clauses = ["m.owner_id = ?", "m.deleted_at IS NULL"]
        params: list = [owner_id]
        moment = utc_now()

        if not include_expired:
            clauses.append("m.valid_from <= ?")
            params.append(moment)
            clauses.append("(m.valid_to IS NULL OR m.valid_to > ?)")
            params.append(moment)

        elif not include_future:
            clauses.append("m.valid_from <= ?")
            params.append(moment)

        if kind is not None:
            clauses.append("m.kind = ?")
            params.append(validate_kind(kind))

        if layer is not None:
            clauses.append("m.layer = ?")
            params.append(validate_layer(layer))

        if min_trust is not None:
            allowed = trusted_at_least(min_trust)
            placeholders = ",".join("?" for _ in allowed)
            clauses.append(f"m.trust IN ({placeholders})")
            params.extend(allowed)

        if len(query.strip()) >= MIN_FTS_QUERY_CHARS:
            hits = self._search_fts(
                _match_expression(query), clauses, params, k
            )

            if hits:
                return self._with_current_facts(
                    {"mode": "fts", "hits": hits}, owner_id
                )

            # 中文记忆检索的真问题：用户会说"时区按北京时间"，
            # 而条目写的是"用户时区是 Asia/Shanghai"——没有连续子串。
            # 这里用**2 字 shingle 覆盖率**兜底：能容忍措辞差异，
            # 又用覆盖率下限挡住"只碰到一个常见词"的假命中。
            shingle_hits = self._search_shingle(query, clauses, params, k)

            if shingle_hits:
                return self._with_current_facts(
                    {"mode": "shingle", "hits": shingle_hits}, owner_id
                )

        return self._with_current_facts(
            {"mode": "like", "hits": self._search_like(query, clauses, params, k)},
            owner_id,
        )

    def _with_current_facts(self, response: dict, owner_id: str) -> dict:
        """同一个冲突组只返回"当前有效事实"。

        为什么必须在检索层做：新旧两条都在库里、都能被检索到，如果原样返回，
        排序稍差一点就会把**过期事实**排到第一。这里把同组的多条压成一条，
        并且以库里解析出的当前事实为准（即使它没进 top-k 也要补进来）。
        """

        hits = response["hits"]
        groups = [
            hit.item.conflict_group
            for hit in hits
            if hit.item.conflict_group
        ]

        if not groups:
            return response

        winners = {}

        for group in dict.fromkeys(groups):
            members = [
                item
                for item in self.list_items(
                    owner_id, include_deleted=True, limit=500
                )
                if item.conflict_group == group
            ]
            resolution = resolve_current(members)

            if resolution.item is not None:
                best_score = max(
                    hit.score for hit in hits if hit.item.conflict_group == group
                )
                winners[group] = MemoryHit(
                    item=resolution.item,
                    score=best_score,
                    rank=0,
                    mode=hits[0].mode,
                )

        ordered = []
        seen = set()

        for hit in hits:
            group = hit.item.conflict_group

            if not group:
                ordered.append(hit)
                continue

            if group in seen:
                continue

            seen.add(group)

            if group in winners:
                ordered.append(winners[group])

        return {
            "mode": response["mode"],
            "hits": [
                MemoryHit(
                    item=hit.item, score=hit.score, rank=index + 1, mode=hit.mode
                )
                for index, hit in enumerate(ordered)
            ],
        }

    def _search_fts(self, phrase: str, clauses, params, k: int) -> list[MemoryHit]:
        sql = (
            "SELECT m.*, bm25(memory_items_fts) AS score "
            "FROM memory_items_fts "
            "JOIN memory_items m ON m.id = memory_items_fts.rowid "
            "WHERE memory_items_fts MATCH ? AND "
            + " AND ".join(clauses)
            + " ORDER BY score, m.id LIMIT ?"
        )

        with closing(self._connect()) as connection:
            rows = connection.execute(sql, [phrase, *params, k]).fetchall()

        return [
            MemoryHit(
                item=_to_item(row),
                score=float(row["score"]),
                rank=index + 1,
                mode="fts",
            )
            for index, row in enumerate(rows)
        ]

    def _search_like(self, query: str, clauses, params, k: int) -> list[MemoryHit]:
        terms = _terms(query)
        like_clauses = []
        like_params = []

        for term in terms:
            escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like_clauses.append(
                "(m.text LIKE ? ESCAPE '\\' OR m.key LIKE ? ESCAPE '\\' "
                "OR m.value LIKE ? ESCAPE '\\')"
            )
            like_params.extend([f"%{escaped}%"] * 3)

        sql = (
            "SELECT m.*, 0.0 AS score FROM memory_items m WHERE "
            + " AND ".join(like_clauses + clauses)
            + " ORDER BY m.id LIMIT ?"
        )

        with closing(self._connect()) as connection:
            rows = connection.execute(sql, [*like_params, *params, k]).fetchall()

        return [
            MemoryHit(
                item=_to_item(row),
                score=0.0,
                rank=index + 1,
                mode="like",
            )
            for index, row in enumerate(rows)
        ]

    def _search_shingle(self, query: str, clauses, params, k: int) -> list[MemoryHit]:
        """按 2 字 shingle 覆盖率兜底（中文措辞差异）。

        先按"任意一个 shingle 命中"取候选，再在 Python 里算覆盖率：
        覆盖率 = 命中的 shingle 数 / 全部 shingle 数。
        只有覆盖率 ≥ 阈值且命中数 ≥2 的条目才算候选 —— 只碰到一个
        高频词（比如"用户"）不会被当成命中。
        """

        shingles = _shingles(query)

        if len(shingles) < SHINGLE_MIN_MATCHED:
            return []

        or_clauses = []
        or_params = []

        for shingle in shingles:
            escaped = (
                shingle.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            or_clauses.append(
                "(m.text LIKE ? ESCAPE '\\' OR m.key LIKE ? ESCAPE '\\' "
                "OR m.value LIKE ? ESCAPE '\\')"
            )
            or_params.extend([f"%{escaped}%"] * 3)

        sql = (
            "SELECT m.*, 0.0 AS score FROM memory_items m WHERE ("
            + " OR ".join(or_clauses)
            + ") AND "
            + " AND ".join(clauses)
            + " LIMIT ?"
        )

        with closing(self._connect()) as connection:
            rows = connection.execute(
                sql, [*or_params, *params, SHINGLE_MAX_CANDIDATES]
            ).fetchall()

        scored = []

        for row in rows:
            # 只拿正文和值算覆盖率，**不拿 key**：key 是内部标识（如
            # `fact_disk`），里面的单个字母会把无关条目顶到前面。
            haystack = " ".join(
                [row["text"] or "", row["value"] or ""]
            ).lower()
            matched = sum(1 for shingle in shingles if shingle.lower() in haystack)
            coverage = matched / len(shingles)

            if matched >= SHINGLE_MIN_MATCHED and coverage >= SHINGLE_MIN_COVERAGE:
                scored.append((coverage, row))

        scored.sort(key=lambda pair: (-pair[0], pair[1]["id"]))

        return [
            MemoryHit(
                item=_to_item(row),
                score=round(coverage, 4),
                rank=index + 1,
                mode="shingle",
            )
            for index, (coverage, row) in enumerate(scored[:k])
        ]


def _shingles(query: str) -> list[str]:
    """把查询切成 2 字中文 shingle + 英文/数字词，去重保序。"""

    runs = re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9_]+", query)
    shingles = []

    for run in runs:
        if run[0].isascii():
            shingles.append(run.lower())
            continue

        if len(run) == 1:
            shingles.append(run)
            continue

        for index in range(len(run) - 1):
            shingles.append(run[index:index + 2])

    seen = []
    for shingle in shingles:
        if shingle not in seen:
            seen.append(shingle)

    return seen


def _to_item(row) -> MemoryItem:
    return MemoryItem(
        id=int(row["id"]),
        owner_id=row["owner_id"],
        kind=row["kind"],
        layer=row["layer"],
        trust=row["trust"],
        key=row["key"],
        value=row["value"],
        text=row["text"],
        source_ref=row["source_ref"],
        source_doc_id=row["source_doc_id"],
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        supersedes_id=row["supersedes_id"],
        conflict_group=row["conflict_group"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row["deleted_at"],
    )


_STORE = None
_STORE_PATH = None


def get_memory_store() -> MemoryStore:
    global _STORE, _STORE_PATH

    from src import config

    db_path = config.paths.knowledge_db

    if _STORE is None or _STORE_PATH != db_path:
        _STORE = MemoryStore(db_path)
        _STORE_PATH = db_path

    return _STORE


def reset_store() -> None:
    global _STORE, _STORE_PATH
    _STORE = None
    _STORE_PATH = None
