"""知识库数据层：文档 / 片段 / 全文索引 / 检索审计。

三条硬性约束：

1. **归属隔离写进 SQL**：所有查询都带 `owner_id` 条件，
   不存在"先取全局再过滤"的路径；
2. **内容级幂等**：同一用户下 `(owner_id, sha256)` 唯一，
   重复摄取同一份内容只留一条文档（与监控的 seen_items 同一思路）；
3. **索引与片段同事务**：FTS 行与 chunk 行一起写、一起删，
   不允许出现"检索得到但记录已删"的悬挂。
4. **引用可回溯**：模型回复里的 `[n]` 解析后写回
   `knowledge_retrievals.cited_chunk_ids`，与 `top_chunk_ids`
   同序（即 `[n]` 的编号），于是"检索到了但没用"和
   "引用错了"可以分开复盘。

中文检索的坑（实测 sqlite 3.49）：FTS5 的 `unicode61` 不分中文词，
而 `trigram` 需要查询**至少 3 个字符**才命中。所以：
查询 ≥3 字符走 FTS5(BM25)，1–2 个字符退回 `LIKE` 兜底，
并在返回里如实标注用了哪个通道（`mode`）。
"""

import hashlib
import re
import sqlite3

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from src.config import paths
from src.knowledge.chunker import chunk_text, normalize


SOURCE_TYPES = ("monitor", "upload", "summary")

MIN_FTS_QUERY_CHARS = 3


def utc_now(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    return moment.astimezone(timezone.utc).isoformat()


def content_hash(text: str) -> str:
    """内容哈希：同一份内容无论来源，得到同一个键。"""

    return hashlib.sha256(
        normalize(text).encode("utf-8")
    ).hexdigest()


class KnowledgeStore:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path or paths.knowledge_db)

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # ----------------------------------------------------------------
    # 建表
    # ----------------------------------------------------------------

    def init_db(self) -> None:
        self.db_path.parent.mkdir(
            parents=True, exist_ok=True
        )

        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS knowledge_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id TEXT NOT NULL,
                    bot_id TEXT,
                    source_type TEXT NOT NULL
                        CHECK (source_type IN
                               ('monitor', 'upload', 'summary')),
                    source_ref TEXT,
                    title TEXT,
                    url TEXT,
                    sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    archived INTEGER NOT NULL DEFAULT 0,
                    UNIQUE (owner_id, sha256)
                )
                """
            )

            # 迁移：老库没有过期 / 归档列（R5 新增）。
            doc_columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(knowledge_documents)"
                )
            }

            if "expires_at" not in doc_columns:
                conn.execute(
                    "ALTER TABLE knowledge_documents "
                    "ADD COLUMN expires_at TEXT"
                )

            if "archived" not in doc_columns:
                conn.execute(
                    "ALTER TABLE knowledge_documents "
                    "ADD COLUMN archived INTEGER NOT NULL DEFAULT 0"
                )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS knowledge_chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id INTEGER NOT NULL
                        REFERENCES knowledge_documents(id)
                        ON DELETE CASCADE,
                    seq INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    char_count INTEGER NOT NULL,
                    UNIQUE (document_id, seq)
                )
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_knowledge_documents_owner
                ON knowledge_documents (owner_id, id)
                """
            )

            # 全文索引：rowid = knowledge_chunks.id，手工同步
            # （不用触发器：显式在同一事务里写，行为可读可测）
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS
                    knowledge_chunks_fts
                USING fts5 (text, tokenize='trigram')
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS knowledge_retrievals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT,
                    user_id TEXT NOT NULL,
                    query_sha256 TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    k INTEGER NOT NULL,
                    latency_ms INTEGER NOT NULL,
                    top_chunk_ids TEXT,
                    cited_chunk_ids TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )

            # 迁移：老库没有引用列（R4 新增）。
            # 建表语句对老库无效（IF NOT EXISTS），所以这里补一次。
            columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(knowledge_retrievals)"
                )
            }

            if "cited_chunk_ids" not in columns:
                conn.execute(
                    """
                    ALTER TABLE knowledge_retrievals
                    ADD COLUMN cited_chunk_ids TEXT
                    """
                )

            # 向量（R3）：每个片段一行，float32 BLOB。
            # 片段删除时级联清理，不留悬挂。
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS knowledge_embeddings (
                    chunk_id INTEGER PRIMARY KEY
                        REFERENCES knowledge_chunks(id)
                        ON DELETE CASCADE,
                    model TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    dim INTEGER NOT NULL,
                    vector BLOB NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_knowledge_embeddings_model
                ON knowledge_embeddings (model, chunk_id)
                """
            )

            # 内容级缓存：同一段文本只算一次（模型级，不含 owner —— 见
            # src/knowledge/embed.py 的说明）。
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS
                    knowledge_embedding_cache (
                    model TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    dim INTEGER NOT NULL,
                    vector BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (model, sha256)
                )
                """
            )

    # ----------------------------------------------------------------
    # 写入
    # ----------------------------------------------------------------

    def ingest_document(
        self,
        *,
        owner_id: str,
        text: str,
        source_type: str,
        title: str | None = None,
        url: str | None = None,
        source_ref: str | None = None,
        bot_id: str | None = None,
        expires_at: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        """摄取一份文档；内容相同则直接返回已有记录（幂等）。"""

        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError("owner_id 不能为空")

        if source_type not in SOURCE_TYPES:
            raise ValueError(
                f"未知的来源类型：{source_type!r}"
            )

        chunks = chunk_text(text)

        if not chunks:
            raise ValueError("文档内容为空")

        owner_id = owner_id.strip()
        digest = content_hash(text)

        self.init_db()

        with closing(self._connect()) as conn:

            try:
                conn.execute("BEGIN IMMEDIATE")

                existing = conn.execute(
                    """
                    SELECT id, created_at
                    FROM knowledge_documents
                    WHERE owner_id = ? AND sha256 = ?
                    """,
                    (owner_id, digest),
                ).fetchone()

                if existing is not None:
                    conn.commit()

                    return {
                        "id": existing["id"],
                        "created": False,
                        "chunks": 0,
                        "sha256": digest,
                    }

                # 内容更新归档（R5）：同一 source_ref 的新版本进来，
                # 旧版本标记 archived，从检索候选剔除。
                if source_ref and source_ref.strip():
                    conn.execute(
                        """
                        UPDATE knowledge_documents
                        SET archived = 1
                        WHERE owner_id = ?
                          AND source_ref = ?
                          AND archived = 0
                        """,
                        (owner_id, source_ref.strip()),
                    )

                cursor = conn.execute(
                    """
                    INSERT INTO knowledge_documents (
                        owner_id, bot_id, source_type,
                        source_ref, title, url,
                        sha256, created_at, expires_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        owner_id,
                        bot_id,
                        source_type,
                        source_ref,
                        title,
                        url,
                        digest,
                        utc_now(now),
                        expires_at,
                    ),
                )

                document_id = cursor.lastrowid

                for seq, chunk in enumerate(chunks):
                    chunk_cursor = conn.execute(
                        """
                        INSERT INTO knowledge_chunks (
                            document_id, seq, text,
                            char_count
                        )
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            document_id,
                            seq,
                            chunk,
                            len(chunk),
                        ),
                    )

                    # FTS 行与 chunk 行同事务写入
                    conn.execute(
                        """
                        INSERT INTO knowledge_chunks_fts
                            (rowid, text)
                        VALUES (?, ?)
                        """,
                        (chunk_cursor.lastrowid, chunk),
                    )

                conn.commit()

                return {
                    "id": document_id,
                    "created": True,
                    "chunks": len(chunks),
                    "sha256": digest,
                }

            except Exception:
                conn.rollback()
                raise

    # ----------------------------------------------------------------
    # 读取
    # ----------------------------------------------------------------

    def list_documents(
        self,
        owner_id: str,
        *,
        limit: int = 50,
        include_archived: bool = False,
    ) -> list[dict]:
        self.init_db()

        clause = "" if include_archived else "AND d.archived = 0"

        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT d.id, d.title, d.url, d.source_type,
                       d.source_ref, d.created_at,
                       d.expires_at, d.archived,
                       COUNT(c.id) AS chunks
                FROM knowledge_documents d
                LEFT JOIN knowledge_chunks c
                       ON c.document_id = d.id
                WHERE d.owner_id = ?
                  {clause}
                GROUP BY d.id
                ORDER BY d.id DESC
                LIMIT ?
                """,
                (owner_id, limit),
            ).fetchall()

        return [dict(row) for row in rows]

    def count_chunks(self, owner_id: str | None = None) -> int:
        self.init_db()

        with closing(self._connect()) as conn:
            if owner_id is None:
                return conn.execute(
                    "SELECT COUNT(*) FROM knowledge_chunks"
                ).fetchone()[0]

            return conn.execute(
                """
                SELECT COUNT(*)
                FROM knowledge_chunks c
                JOIN knowledge_documents d
                  ON d.id = c.document_id
                WHERE d.owner_id = ?
                """,
                (owner_id,),
            ).fetchone()[0]

    def delete_document(
        self,
        *,
        owner_id: str,
        document_id: int,
    ) -> bool:
        """删除文档：片段与索引一并清掉（不留悬挂）。"""

        if type(document_id) is not int or document_id <= 0:
            raise ValueError("无效的文档 ID")

        self.init_db()

        with closing(self._connect()) as conn:

            try:
                conn.execute("BEGIN IMMEDIATE")

                owned = conn.execute(
                    """
                    SELECT id FROM knowledge_documents
                    WHERE id = ? AND owner_id = ?
                    """,
                    (document_id, owner_id),
                ).fetchone()

                if owned is None:
                    conn.commit()
                    return False

                chunk_ids = [
                    row[0]
                    for row in conn.execute(
                        """
                        SELECT id FROM knowledge_chunks
                        WHERE document_id = ?
                        """,
                        (document_id,),
                    )
                ]

                for chunk_id in chunk_ids:
                    conn.execute(
                        """
                        DELETE FROM knowledge_chunks_fts
                        WHERE rowid = ?
                        """,
                        (chunk_id,),
                    )

                conn.execute(
                    """
                    DELETE FROM knowledge_chunks
                    WHERE document_id = ?
                    """,
                    (document_id,),
                )

                conn.execute(
                    """
                    DELETE FROM knowledge_documents
                    WHERE id = ? AND owner_id = ?
                    """,
                    (document_id, owner_id),
                )

                conn.commit()
                return True

            except Exception:
                conn.rollback()
                raise

    def set_expiry(
        self,
        *,
        owner_id: str,
        document_id: int,
        expires_at: str | None,
    ) -> bool:
        """设置（或清除）文档过期时间。`None` = 永不过期（R5 软过期）。"""

        if type(document_id) is not int or document_id <= 0:
            raise ValueError("无效的文档 ID")

        self.init_db()

        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """
                UPDATE knowledge_documents
                SET expires_at = ?
                WHERE id = ? AND owner_id = ?
                """,
                (expires_at, document_id, owner_id),
            )

        return cursor.rowcount == 1

    # ----------------------------------------------------------------
    # 检索（BM25）
    # ----------------------------------------------------------------

    def search_bm25(
        self,
        *,
        owner_id: str,
        query: str,
        limit: int = 5,
    ) -> dict:
        """按关键词检索该用户的片段。

        返回 `{"mode": "fts"|"like", "hits": [...]}`：
        查询少于 3 个字符时 trigram 不可能命中，改用 LIKE 兜底，
        并**如实标注**用了哪个通道 —— 不假装它是 BM25 的结果。
        """

        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError("owner_id 不能为空")

        if not isinstance(query, str) or not query.strip():
            raise ValueError("查询不能为空")

        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("limit 无效")

        query = query.strip()
        owner_id = owner_id.strip()

        self.init_db()

        if len(query) >= MIN_FTS_QUERY_CHARS:
            hits = self._search_fts(
                owner_id=owner_id,
                query=query,
                limit=limit,
            )

            if hits:
                return {"mode": "fts", "hits": hits}

        return {
            "mode": "like",
            "hits": self._search_like(
                owner_id=owner_id,
                query=query,
                limit=limit,
            ),
        }

    def _search_fts(
        self,
        *,
        owner_id: str,
        query: str,
        limit: int,
    ) -> list[dict]:
        phrase = self._match_expression(query)

        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT c.id AS chunk_id,
                       c.document_id AS document_id,
                       c.seq AS seq,
                       c.text AS text,
                       d.title AS title,
                       d.url AS url,
                       bm25(knowledge_chunks_fts) AS score
                FROM knowledge_chunks_fts
                JOIN knowledge_chunks c
                  ON c.id = knowledge_chunks_fts.rowid
                JOIN knowledge_documents d
                  ON d.id = c.document_id
                WHERE knowledge_chunks_fts MATCH ?
                  AND d.owner_id = ?
                  AND d.archived = 0
                  AND (d.expires_at IS NULL OR d.expires_at > ?)
                ORDER BY score
                LIMIT ?
                """,
                (phrase, owner_id, utc_now(), limit),
            ).fetchall()

        return [dict(row) for row in rows]

    @staticmethod
    def _query_terms(query: str) -> list[str]:
        """把查询拆成词项（中英文都按分隔符切，空则退回整串）。"""

        terms = [
            part
            for part in re.split(
                r"[\s,，。;；:：!！?？()（）\[\]]+", query
            )
            if part
        ]

        return terms or [query]

    @classmethod
    def _match_expression(cls, query: str) -> str:
        """把用户输入拼成安全的 FTS5 MATCH 表达式。

        每个词单独引号包裹再 `AND` 连接 —— 比把整句当一个短语宽松
        （"asyncio profile" 不必在原文里紧挨着出现），也不会让
        `*` / `(` / `"` 之类的字符破坏 FTS 语法。
        """

        return " AND ".join(
            '"%s"' % term.replace('"', '""')
            for term in cls._query_terms(query)
        )

    def _search_like(
        self,
        *,
        owner_id: str,
        query: str,
        limit: int,
    ) -> list[dict]:
        """字面量兜底：**逐词 AND**，而不是把整串当一个子串。

        为什么不能拿整串去 LIKE：trigram 需要 ≥3 个字符，所以
        "asyncio 瓶颈" 里的 2 字中文词在 FTS 里匹配不到，
        若回退时又要求整串字面出现，就会得到 0 命中 ——
        用户明明搜的词就在文里。
        """

        terms = self._query_terms(query)

        where = " AND ".join(
            "c.text LIKE ? ESCAPE '\\'" for _ in terms
        )

        params = [
            "%{0}%".format(
                term.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            for term in terms
        ]

        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT c.id AS chunk_id,
                       c.document_id AS document_id,
                       c.seq AS seq,
                       c.text AS text,
                       d.title AS title,
                       d.url AS url,
                       0.0 AS score
                FROM knowledge_chunks c
                JOIN knowledge_documents d
                  ON d.id = c.document_id
                WHERE d.owner_id = ?
                  AND d.archived = 0
                  AND (d.expires_at IS NULL OR d.expires_at > ?)
                  AND {where}
                ORDER BY c.id
                LIMIT ?
                """,
                (owner_id, utc_now(), *params, limit),
            ).fetchall()

        return [dict(row) for row in rows]

    # ----------------------------------------------------------------
    # 检索审计
    # ----------------------------------------------------------------

    def record_retrieval(
        self,
        *,
        user_id: str,
        query: str,
        mode: str,
        k: int,
        latency_ms: int,
        top_chunk_ids: list[int],
        request_id: str | None = None,
        now: datetime | None = None,
    ) -> int:
        """记一次检索。query 只存哈希，不落明文。"""

        self.init_db()

        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """
                INSERT INTO knowledge_retrievals (
                    request_id, user_id, query_sha256,
                    mode, k, latency_ms, top_chunk_ids,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    user_id,
                    hashlib.sha256(
                        query.encode("utf-8")
                    ).hexdigest(),
                    mode,
                    k,
                    latency_ms,
                    ",".join(
                        str(item) for item in top_chunk_ids
                    ),
                    utc_now(now),
                ),
            )

        return cursor.lastrowid

    @staticmethod
    def _parse_ids(value) -> list[int]:
        """把审计里的逗号分隔 ID 串解析回整数列表。"""

        return [
            int(part)
            for part in str(value or "").split(",")
            if part.strip().isdigit()
        ]

    def record_citations(
        self,
        *,
        retrieval_id: int,
        chunk_ids: list[int],
    ) -> bool:
        """把模型实际引用的片段回写到审计行（引用可回溯）。"""

        if type(retrieval_id) is not int or retrieval_id <= 0:
            raise ValueError("无效的检索记录 ID")

        ids = [
            item
            for item in chunk_ids
            if type(item) is int and item > 0
        ]

        if not ids:
            raise ValueError("引用片段不能为空")

        self.init_db()

        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """
                UPDATE knowledge_retrievals
                SET cited_chunk_ids = ?
                WHERE id = ?
                """,
                (
                    ",".join(str(item) for item in ids),
                    retrieval_id,
                ),
            )

        return cursor.rowcount == 1

    def list_retrievals(
        self,
        user_id: str,
        *,
        limit: int = 20,
    ) -> list[dict]:
        """按用户列出最近的检索审计。"""

        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("user_id 不能为空")

        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit 无效")

        self.init_db()

        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT id, request_id, user_id, mode, k,
                       latency_ms, top_chunk_ids,
                       cited_chunk_ids, created_at
                FROM knowledge_retrievals
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (user_id.strip(), limit),
            ).fetchall()

        return [
            {
                **dict(row),
                "top_chunk_ids": self._parse_ids(
                    row["top_chunk_ids"]
                ),
                "cited_chunk_ids": self._parse_ids(
                    row["cited_chunk_ids"]
                ),
            }
            for row in rows
        ]

    def chunk_documents(
        self,
        chunk_ids: list[int],
    ) -> dict[int, int]:
        """chunk_id → document_id（把引用回溯到文档）。"""

        ids = [
            item
            for item in chunk_ids
            if type(item) is int and item > 0
        ]

        if not ids:
            return {}

        self.init_db()

        placeholders = ",".join("?" for _ in ids)

        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT id, document_id
                FROM knowledge_chunks
                WHERE id IN ({placeholders})
                """,
                tuple(ids),
            ).fetchall()

        return {
            row["id"]: row["document_id"] for row in rows
        }

    # ----------------------------------------------------------------
    # 向量（R3）
    # ----------------------------------------------------------------

    def chunks_missing_embedding(
        self,
        *,
        owner_id: str,
        model: str,
    ) -> list[dict]:
        """还缺该模型向量的片段（按 owner 收窄）。"""

        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError("owner_id 不能为空")

        if not isinstance(model, str) or not model.strip():
            raise ValueError("model 不能为空")

        self.init_db()

        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT c.id AS chunk_id, c.text AS text
                FROM knowledge_chunks c
                JOIN knowledge_documents d
                  ON d.id = c.document_id
                LEFT JOIN knowledge_embeddings e
                  ON e.chunk_id = c.id
                 AND e.model = ?
                WHERE d.owner_id = ?
                  AND e.chunk_id IS NULL
                ORDER BY c.id
                """,
                (model.strip(), owner_id.strip()),
            ).fetchall()

        return [dict(row) for row in rows]

    def embedding_cache_get(
        self,
        *,
        model: str,
        digests: list[str],
    ) -> dict:
        """按内容哈希取缓存：`{sha256: (dim, blob)}`。"""

        if not digests:
            return {}

        self.init_db()

        found = {}
        items = list(digests)

        with closing(self._connect()) as conn:
            # 分批查，避免 SQLite 变量上限
            for start in range(0, len(items), 400):
                batch = items[start:start + 400]
                placeholders = ",".join("?" for _ in batch)

                rows = conn.execute(
                    f"""
                    SELECT sha256, dim, vector
                    FROM knowledge_embedding_cache
                    WHERE model = ?
                      AND sha256 IN ({placeholders})
                    """,
                    (model, *batch),
                ).fetchall()

                for row in rows:
                    found[row["sha256"]] = (
                        row["dim"],
                        row["vector"],
                    )

        return found

    def save_embedding_cache(
        self,
        *,
        model: str,
        entries: list,
    ) -> int:
        """写内容缓存：`entries` 是 `(sha256, dim, blob)` 列表。"""

        if not entries:
            return 0

        self.init_db()

        with closing(self._connect()) as conn, conn:
            conn.executemany(
                """
                INSERT INTO knowledge_embedding_cache (
                    model, sha256, dim, vector, created_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (model, sha256) DO UPDATE SET
                    dim = excluded.dim,
                    vector = excluded.vector
                """,
                [
                    (
                        model,
                        digest,
                        dim,
                        blob,
                        utc_now(),
                    )
                    for digest, dim, blob in entries
                ],
            )

        return len(entries)

    def save_chunk_embeddings(self, rows: list) -> int:
        """写片段向量：`rows` 是 `(chunk_id, model, sha256, dim, blob)`。"""

        if not rows:
            return 0

        self.init_db()

        with closing(self._connect()) as conn, conn:
            conn.executemany(
                """
                INSERT INTO knowledge_embeddings (
                    chunk_id, model, sha256, dim, vector, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (chunk_id) DO UPDATE SET
                    model = excluded.model,
                    sha256 = excluded.sha256,
                    dim = excluded.dim,
                    vector = excluded.vector
                """,
                [
                    (
                        chunk_id,
                        model,
                        digest,
                        dim,
                        blob,
                        utc_now(),
                    )
                    for chunk_id, model, digest, dim, blob in rows
                ],
            )

        return len(rows)

    def count_embeddings(
        self,
        *,
        owner_id: str | None = None,
        model: str | None = None,
    ) -> int:
        self.init_db()

        with closing(self._connect()) as conn:
            if owner_id is None:
                return conn.execute(
                    "SELECT COUNT(*) FROM knowledge_embeddings"
                ).fetchone()[0]

            return conn.execute(
                """
                SELECT COUNT(*)
                FROM knowledge_embeddings e
                JOIN knowledge_chunks c
                  ON c.id = e.chunk_id
                JOIN knowledge_documents d
                  ON d.id = c.document_id
                WHERE d.owner_id = ?
                  AND (? IS NULL OR e.model = ?)
                """,
                (owner_id, model, model),
            ).fetchone()[0]

    def search_vector(
        self,
        *,
        owner_id: str,
        query_vector,
        model: str,
        limit: int = 5,
    ) -> list[dict]:
        """暴力 cosine 检索：**先按 owner 收窄候选，再算相似度**。

        绝不"先取全局 top-k 再过滤" —— 那既会召回塌陷，也可能把别人的
        内容算进上下文。没有 numpy，点积用纯 Python（延迟由评测如实测出）。
        """

        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError("owner_id 不能为空")

        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("limit 无效")

        query_vector = [float(value) for value in query_vector]

        if not query_vector:
            raise ValueError("查询向量不能为空")

        self.init_db()

        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT c.id AS chunk_id,
                       c.document_id AS document_id,
                       c.seq AS seq,
                       c.text AS text,
                       d.title AS title,
                       d.url AS url,
                       e.dim AS dim,
                       e.vector AS vector
                FROM knowledge_embeddings e
                JOIN knowledge_chunks c
                  ON c.id = e.chunk_id
                JOIN knowledge_documents d
                  ON d.id = c.document_id
                WHERE d.owner_id = ?
                  AND e.model = ?
                  AND d.archived = 0
                  AND (d.expires_at IS NULL OR d.expires_at > ?)
                """,
                (owner_id.strip(), model, utc_now()),
            ).fetchall()

        from src.knowledge.embed import (
            blob_vector,
            cosine,
        )

        scored = []

        for row in rows:
            # 维度不一致的行直接跳过：模型换了、或半途写入失败
            if row["dim"] != len(query_vector):
                continue

            score = cosine(
                query_vector,
                blob_vector(row["vector"]),
            )

            scored.append(
                {
                    "chunk_id": row["chunk_id"],
                    "document_id": row["document_id"],
                    "seq": row["seq"],
                    "text": row["text"],
                    "title": row["title"],
                    "url": row["url"],
                    "score": score,
                }
            )

        scored.sort(
            key=lambda item: (-item["score"], item["chunk_id"])
        )

        return scored[:limit]

