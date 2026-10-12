"""聊天请求用户归属的 SQLite 存储操作。

第一阶段只提供独立的数据层，不改变现有 API 路由。
所有 user_id 应来自服务端已经验证的登录会话，绝不能信任前端传入的 user_id。

现有 chat_requests 的 request_id 仍是全局唯一键：
- 迁移前的旧记录保留，但 user_id 为 NULL，任何账户都不能直接读取。
- 新记录归属某个 user_id，其他用户不能读取、复用或更新。
- 抢占新请求时使用 BEGIN IMMEDIATE，保留原有防重复计费的原子性。
"""

from __future__ import annotations

import sqlite3


class RequestOwnerMismatch(Exception):
    """请求 ID 已被其他账户占用（或是迁移前没有归属的旧记录）。"""


class RequestMessageConflict(Exception):
    """相同用户复用 request_id 时，消息摘要不同。"""


def _check_user_id(user_id: str) -> None:
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("user_id 必须来自有效的服务端登录身份")


def migrate_chat_ownership(conn: sqlite3.Connection) -> None:
    """非破坏性迁移：只增加可空 user_id 列和索引。

    调用方负责数据库连接和提交事务。必须先有 chat_requests 表。
    注意：不要将任何旧记录自动分配给站长或其他账户。
    """
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(chat_requests)")
    }
    if not columns:
        raise sqlite3.OperationalError(
            "chat_requests 表不存在，请先完成原始数据库初始化"
        )

    if "user_id" not in columns:
        conn.execute(
            "ALTER TABLE chat_requests ADD COLUMN user_id TEXT"
        )

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_requests_user_id "
        "ON chat_requests(user_id)"
    )


def read_owned_record(
    conn: sqlite3.Connection,
    request_id: str,
    user_id: str,
) -> dict | None:
    """只读取归属于当前用户的记录，旧记录和其他用户记录都不可见。"""
    _check_user_id(user_id)

    row = conn.execute(
        """
        SELECT request_id, message_hash, status, reply
        FROM chat_requests
        WHERE request_id = ? AND user_id = ?
        """,
        (request_id, user_id),
    ).fetchone()
    if row is None:
        return None

    return {
        "request_id": row[0],
        "message_hash": row[1],
        "status": row[2],
        "reply": row[3],
    }


def claim_owned_request(
    conn: sqlite3.Connection,
    request_id: str,
    digest: str,
    user_id: str,
    timestamp: str,
) -> dict | None:
    """原子领取请求。

    必须使用当前操作专用且未开启事务的 SQLite 连接。
    返回 None 表示成功领取新请求；返回 dict 表示当前用户已领取过。
    其他用户或旧版无归属记录的同名 request_id 会被拒绝。
    """
    _check_user_id(user_id)

    with conn:
        conn.execute("BEGIN IMMEDIATE")

        existing = conn.execute(
            """
            SELECT request_id, message_hash, status, reply, user_id
            FROM chat_requests
            WHERE request_id = ?
            """,
            (request_id,),
        ).fetchone()

        if existing is not None:
            # 先判断归属，避免跨用户通过摘要差异探测请求信息。
            if existing[4] != user_id:
                raise RequestOwnerMismatch(
                    "请求不存在或无权访问"
                )

            if existing[1] != digest:
                raise RequestMessageConflict(
                    "同一个 request_id 不能对应不同消息"
                )

            return {
                "request_id": existing[0],
                "message_hash": existing[1],
                "status": existing[2],
                "reply": existing[3],
            }

        conn.execute(
            """
            INSERT INTO chat_requests (
                request_id,
                message_hash,
                status,
                reply,
                created_at,
                updated_at,
                user_id
            )
            VALUES (?, ?, 'processing', NULL, ?, ?, ?)
            """,
            (request_id, digest, timestamp, timestamp, user_id),
        )

    return None


def update_owned_status(
    conn: sqlite3.Connection,
    request_id: str,
    user_id: str,
    status: str,
    reply: str | None,
    timestamp: str,
) -> bool:
    """只更新当前用户仍在 processing 的请求。"""
    _check_user_id(user_id)

    if status not in ("completed", "unknown"):
        raise ValueError("非法的目标请求状态")

    with conn:
        result = conn.execute(
            """
            UPDATE chat_requests
            SET status = ?, reply = ?, updated_at = ?
            WHERE request_id = ?
              AND user_id = ?
              AND status = 'processing'
            """,
            (status, reply, timestamp, request_id, user_id),
        )

    return result.rowcount == 1
