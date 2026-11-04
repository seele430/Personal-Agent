"""span 存储：平铺的行，之后由查询侧拼成树。

设计约束（与 docs 里的一致）：
- 只存 `parent_id`，不存层级路径 —— 树是算出来的。
- 不复用 request_id：它是幂等键，语义不同。
- 写入失败不能影响主流程：所有调用点都包了 try/except。
"""

import json

import atexit

import logging

import sqlite3

from contextlib import closing
from pathlib import Path


logger = logging.getLogger("personal_agent.trace")


_INSERT_SQL = """
    INSERT INTO spans (
        span_id,
        trace_id,
        parent_id,
        name,
        start_ms,
        dur_ms,
        status,
        attrs,
        created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


# 进程退出前把没写完的 span 落盘
_ATEXIT_STORES: list["TraceStore"] = []


def _flush_all() -> None:

    for store in list(_ATEXIT_STORES):
        try:
            store.flush()
        except Exception:
            pass


atexit.register(_flush_all)


class TraceStore:
    """SQLite 版 span 存储。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

        self.db_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        # 缓冲：单条 span 直接落盘约 8ms，攒一批只花一次提交
        self._pending: list[tuple] = []

        _ATEXIT_STORES.append(self)

        self.init_db()

    def _connect(self):
        conn = sqlite3.connect(
            self.db_path,
            timeout=10,
        )

        conn.row_factory = sqlite3.Row

        # 观测写入必须便宜：WAL 只在建库时设一次（它是库级属性），
        # 每条连接只调 synchronous=NORMAL。
        conn.execute("PRAGMA synchronous=NORMAL")

        return conn

    def init_db(self):
        with closing(self._connect()) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")

            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS spans (
                    span_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL,
                    parent_id TEXT,
                    name TEXT NOT NULL,
                    start_ms REAL NOT NULL,
                    dur_ms REAL NOT NULL,
                    status TEXT NOT NULL,
                    attrs TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS
                    idx_spans_trace
                ON spans(trace_id);

                CREATE INDEX IF NOT EXISTS
                    idx_spans_created
                ON spans(created_at);
                """
            )

    # ======================================
    # 写一条 span
    # ======================================

    def record_span(
        self,
        *,
        trace_id: str,
        span_id: str,
        parent_id: str | None,
        name: str,
        start_ms: float,
        dur_ms: float,
        status: str = "ok",
        attrs: dict | None = None,
    ) -> None:

        for field, value in (
            ("trace_id", trace_id),
            ("span_id", span_id),
            ("name", name),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} 不能为空")

        payload = None

        if attrs:
            # 只存标量，避免把对象塞进库
            cleaned = {
                key: value
                for key, value in attrs.items()
                if isinstance(value, (str, int, float, bool))
            }

            if cleaned:
                payload = json.dumps(
                    cleaned,
                    ensure_ascii=False,
                )

        from datetime import datetime, timezone

        created_at = datetime.now(
            timezone.utc
        ).isoformat()

        self._pending.append((
            span_id,
            trace_id,
            parent_id,
            name,
            float(start_ms),
            float(dur_ms),
            status,
            payload,
            created_at,
        ))

        # 根 span 结束 = 这次运行结束，落一次盘；
        # 子 span 攒够一批再落，避免每条都付一次提交。
        if parent_id is None or len(self._pending) >= 20:
            self.flush()

    def flush(self) -> None:
        """把缓冲区写成一批。失败只记日志，不抛给业务。"""

        if not self._pending:
            return

        rows, self._pending = self._pending, []

        try:
            with closing(self._connect()) as conn, conn:
                conn.executemany(_INSERT_SQL, rows)

        except Exception:
            logger.exception(
                "span 批量写入失败，丢弃 %s 条",
                len(rows),
            )

    # ======================================
    # 查
    # ======================================

    def fetch_trace(self, trace_id: str) -> list[dict]:

        self.flush()

        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT span_id, trace_id, parent_id, name,
                       start_ms, dur_ms, status, attrs
                FROM spans
                WHERE trace_id = ?
                ORDER BY start_ms ASC
                """,
                (trace_id,),
            ).fetchall()

        return [self._row_to_dict(row) for row in rows]

    def recent_traces(self, limit: int = 20) -> list[dict]:
        """按根 span 汇总最近若干次运行。"""

        self.flush()

        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT trace_id,
                       MIN(start_ms) AS first_start,
                       SUM(dur_ms) AS total_dur,
                       COUNT(*) AS span_count
                FROM spans
                GROUP BY trace_id
                ORDER BY first_start DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

            roots = {}

            for row in rows:
                root = conn.execute(
                    """
                    SELECT name, status
                    FROM spans
                    WHERE trace_id = ? AND parent_id IS NULL
                    LIMIT 1
                    """,
                    (row["trace_id"],),
                ).fetchone()

                roots[row["trace_id"]] = root

        result = []

        for row in rows:
            root = roots.get(row["trace_id"])

            result.append({
                "trace_id": row["trace_id"],
                "root_name": root["name"] if root else "(无根 span)",
                "status": root["status"] if root else "?",
                "span_count": row["span_count"],
                "dur_ms": round(_root_duration(row["trace_id"]), 1),
            })

        return result

    @staticmethod
    def _row_to_dict(row) -> dict:

        return {
            "span_id": row["span_id"],
            "trace_id": row["trace_id"],
            "parent_id": row["parent_id"],
            "name": row["name"],
            "start_ms": row["start_ms"],
            "dur_ms": row["dur_ms"],
            "status": row["status"],
            "attrs": json.loads(row["attrs"]) if row["attrs"] else {},
        }


def _root_duration(trace_id: str) -> float:
    """根 span 的耗时；取不到就退回 0。"""

    store = get_trace_store()

    for item in store.fetch_trace(trace_id):
        if item["parent_id"] is None:
            return item["dur_ms"]

    return 0.0


# ==========================================
# 单例：随 config.paths 变化自动重建
# ==========================================
#
# 测试会用 PERSONAL_AGENT_DATA_DIR 把数据目录切到临时目录，
# 所以这里必须**每次调用时**读 config.paths，不能在 import 时定死。

_STORE = None

_STORE_PATH = None


def get_trace_store() -> TraceStore:

    global _STORE, _STORE_PATH

    from src import config

    db_path = (
        config.paths.chat_requests_db.parent / "traces.db"
    )

    if _STORE is None or _STORE_PATH != db_path:
        _STORE = TraceStore(db_path)
        _STORE_PATH = db_path

    return _STORE
