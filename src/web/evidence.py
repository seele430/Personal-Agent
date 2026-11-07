"""Evidence Store：把"抓到的正文"变成可对账的证据。

设计约束（来自路线图 §4.3 / §8.7 / §9.5）：

- 每条证据必须有 **SHA256**，重复抓取同一正文不会产生第二条证据；
- 证据只存「规范化 URL + 正文 + 元数据」，不存 Cookie / 授权头；
- 支持**一键清空** —— 搜索/抓取条款未确认时，这是保守策略的兜底；
- 引用（citation）必须绑定到具体 `source_id`，不允许"引用了空气"。

落点沿用 `trace_store` / `team_store` 的先例：数据库文件由父目录派生，
**不修改 `src/config.py` 的 Paths 定义**。
"""

from __future__ import annotations

import sqlite3
import threading

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from src.web.extract import text_sha256


DEFAULT_DB_NAME = "evidence.db"

MAX_TEXT_BYTES = 4 * 1024 * 1024

_SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence_sources (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    url           TEXT    NOT NULL,
    final_url     TEXT    NOT NULL DEFAULT '',
    fetched_at    TEXT    NOT NULL,
    http_status   INTEGER NOT NULL,
    media_type    TEXT    NOT NULL DEFAULT '',
    byte_length   INTEGER NOT NULL DEFAULT 0,
    title         TEXT    NOT NULL DEFAULT '',
    text_sha256   TEXT    NOT NULL,
    content_text  TEXT    NOT NULL,
    char_count    INTEGER NOT NULL DEFAULT 0,
    method        TEXT    NOT NULL DEFAULT '',
    etag          TEXT    NOT NULL DEFAULT '',
    query         TEXT    NOT NULL DEFAULT '',
    rank          INTEGER NOT NULL DEFAULT 0,
    UNIQUE (url, text_sha256)
);
CREATE INDEX IF NOT EXISTS idx_evidence_sha
    ON evidence_sources (text_sha256);
CREATE TABLE IF NOT EXISTS evidence_citations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    report_key  TEXT    NOT NULL,
    claim_key   TEXT    NOT NULL,
    source_id   INTEGER NOT NULL,
    quote       TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL,
    UNIQUE (report_key, claim_key, source_id)
);
CREATE INDEX IF NOT EXISTS idx_citation_report
    ON evidence_citations (report_key);
"""


class EvidenceError(RuntimeError):
    """证据库操作失败。"""


@dataclass(frozen=True)
class EvidenceRecord:
    id: int
    url: str
    final_url: str
    fetched_at: str
    http_status: int
    media_type: str
    byte_length: int
    title: str
    text_sha256: str
    char_count: int
    method: str
    query: str
    rank: int

    @property
    def is_empty(self) -> bool:
        return self.char_count <= 0


@dataclass(frozen=True)
class CitationRecord:
    id: int
    report_key: str
    claim_key: str
    source_id: int
    quote: str
    created_at: str


def normalize_url(url: str) -> str:
    """规范化 URL：小写主机、去 fragment、去默认端口、保留查询串。

    只做"同一条资源不要存成两条证据"所需的归一，不做激进改写。
    """

    if not isinstance(url, str) or not url.strip():
        raise ValueError("URL 不能为空")

    parts = urlsplit(url.strip())

    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")

    if not scheme or not host:
        raise ValueError("URL 缺少协议或主机")

    port = parts.port

    if port in (443, 80):
        port = None

    netloc = host if port is None else f"{host}:{port}"

    if parts.username or parts.password:
        raise ValueError("URL 不能包含用户名或密码")

    return urlunsplit((scheme, netloc, parts.path, parts.query, ""))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class EvidenceStore:
    """证据库。所有写操作都自建连接，避免跨线程复用同一个连接。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def init_db(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(_SCHEMA)

    # ---- 证据写入 ---------------------------------------------------

    def add_source(
        self,
        *,
        url: str,
        text: str,
        http_status: int = 200,
        media_type: str = "text/html",
        title: str = "",
        method: str = "",
        final_url: str = "",
        byte_length: int = 0,
        etag: str = "",
        query: str = "",
        rank: int = 0,
    ) -> EvidenceRecord:
        """登记一条证据。

        同 `(url, text_sha256)` 已存在时**返回已有记录**，不重复写入
        —— 这是"重复抓取不产生重复证据"的唯一实现点。
        """

        if not isinstance(text, str) or not text.strip():
            raise EvidenceError("证据正文为空，不能登记")

        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise EvidenceError("证据正文过大")

        if not isinstance(http_status, int) or http_status < 100:
            raise EvidenceError("HTTP 状态码不合法")

        canonical = normalize_url(url)
        digest = text_sha256(text)
        char_count = len("".join(text.split()))

        with self._lock:
            with closing(self._connect()) as connection, connection:
                existing = connection.execute(
                    "SELECT * FROM evidence_sources "
                    "WHERE url = ? AND text_sha256 = ?",
                    (canonical, digest),
                ).fetchone()

                if existing is not None:
                    return _to_record(existing)

                cursor = connection.execute(
                    "INSERT INTO evidence_sources ("
                    " url, final_url, fetched_at, http_status, media_type,"
                    " byte_length, title, text_sha256, content_text,"
                    " char_count, method, etag, query, rank"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        canonical,
                        final_url or canonical,
                        _now(),
                        http_status,
                        media_type,
                        byte_length,
                        title[:200],
                        digest,
                        text,
                        char_count,
                        method,
                        etag[:128],
                        query[:200],
                        rank,
                    ),
                )

                row = connection.execute(
                    "SELECT * FROM evidence_sources WHERE id = ?",
                    (cursor.lastrowid,),
                ).fetchone()

        return _to_record(row)

    # ---- 证据读取 ---------------------------------------------------

    def get_source(self, source_id: int) -> EvidenceRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM evidence_sources WHERE id = ?",
                (source_id,),
            ).fetchone()

        return _to_record(row) if row is not None else None

    def get_text(self, source_id: int) -> str:
        """取回证据正文；不存在时抛错，避免"空引用"。"""

        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT content_text FROM evidence_sources WHERE id = ?",
                (source_id,),
            ).fetchone()

        if row is None:
            raise EvidenceError(f"证据不存在：{source_id}")

        return row["content_text"]

    def find_by_url(self, url: str) -> EvidenceRecord | None:
        canonical = normalize_url(url)

        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM evidence_sources WHERE url = ? "
                "ORDER BY id DESC LIMIT 1",
                (canonical,),
            ).fetchone()

        return _to_record(row) if row is not None else None

    def find_by_text_sha(self, sha256: str) -> EvidenceRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM evidence_sources WHERE text_sha256 = ? "
                "ORDER BY id LIMIT 1",
                (sha256,),
            ).fetchone()

        return _to_record(row) if row is not None else None

    def list_sources(self, limit: int = 100) -> list[EvidenceRecord]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")

        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM evidence_sources ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()

        return [_to_record(row) for row in rows]

    def count_sources(self) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM evidence_sources"
            ).fetchone()

        return int(row["n"])

    # ---- 引用 -------------------------------------------------------

    def add_citation(
        self,
        *,
        report_key: str,
        claim_key: str,
        source_id: int,
        quote: str = "",
    ) -> CitationRecord:
        """把一条论断绑定到一条证据。证据不存在时直接失败。"""

        for name, value in (
            ("report_key", report_key),
            ("claim_key", claim_key),
        ):
            if not isinstance(value, str) or not value.strip():
                raise EvidenceError(f"{name} 不能为空")

        if self.get_source(source_id) is None:
            raise EvidenceError(f"引用指向不存在的证据：{source_id}")

        with self._lock:
            with closing(self._connect()) as connection, connection:
                existing = connection.execute(
                    "SELECT * FROM evidence_citations "
                    "WHERE report_key = ? AND claim_key = ? AND source_id = ?",
                    (report_key, claim_key, source_id),
                ).fetchone()

                if existing is not None:
                    return _to_citation(existing)

                cursor = connection.execute(
                    "INSERT INTO evidence_citations ("
                    " report_key, claim_key, source_id, quote, created_at"
                    ") VALUES (?,?,?,?,?)",
                    (report_key, claim_key, source_id, quote[:500], _now()),
                )

                row = connection.execute(
                    "SELECT * FROM evidence_citations WHERE id = ?",
                    (cursor.lastrowid,),
                ).fetchone()

        return _to_citation(row)

    def citations_for(self, report_key: str) -> list[CitationRecord]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM evidence_citations WHERE report_key = ? "
                "ORDER BY id",
                (report_key,),
            ).fetchall()

        return [_to_citation(row) for row in rows]

    # ---- 清理 -------------------------------------------------------

    def clear(self) -> int:
        """清空证据与引用（保留表结构）。返回删除的证据条数。"""

        with self._lock:
            with closing(self._connect()) as connection, connection:
                removed = connection.execute(
                    "SELECT COUNT(*) AS n FROM evidence_sources"
                ).fetchone()["n"]
                connection.execute("DELETE FROM evidence_citations")
                connection.execute("DELETE FROM evidence_sources")

        return int(removed)


def _to_record(row) -> EvidenceRecord:
    return EvidenceRecord(
        id=int(row["id"]),
        url=row["url"],
        final_url=row["final_url"],
        fetched_at=row["fetched_at"],
        http_status=int(row["http_status"]),
        media_type=row["media_type"],
        byte_length=int(row["byte_length"]),
        title=row["title"],
        text_sha256=row["text_sha256"],
        char_count=int(row["char_count"]),
        method=row["method"],
        query=row["query"],
        rank=int(row["rank"]),
    )


def _to_citation(row) -> CitationRecord:
    return CitationRecord(
        id=int(row["id"]),
        report_key=row["report_key"],
        claim_key=row["claim_key"],
        source_id=int(row["source_id"]),
        quote=row["quote"],
        created_at=row["created_at"],
    )


_STORE = None
_STORE_PATH = None


def get_evidence_store() -> EvidenceStore:
    """进程级单例；路径变化（测试隔离）时自动重建。"""

    global _STORE, _STORE_PATH

    from src import config

    db_path = config.paths.chat_requests_db.parent / DEFAULT_DB_NAME

    if _STORE is None or _STORE_PATH != db_path:
        _STORE = EvidenceStore(db_path)
        _STORE_PATH = db_path

    return _STORE


def reset_store() -> None:
    """测试用：丢掉单例（临时目录切换后必须调用）。"""

    global _STORE, _STORE_PATH
    _STORE = None
    _STORE_PATH = None
