
"""Personal Agent API：聊天幂等与请求状态管理。"""

import hashlib
import logging
import re
import sqlite3
import uuid

from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.agent import chat


# ==========================================
# 基础配置
# ==========================================

BASE_DIR = Path(__file__).resolve().parent.parent
CHAT_DB_PATH = BASE_DIR / "chat_requests.db"

REQUEST_ID_RE = re.compile(
    r"^[A-Za-z0-9_-]{8,128}$"
)

logger = logging.getLogger("personal_agent.chat")

if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)s | "
            "%(name)s | %(message)s"
        )
    )
    logger.addHandler(handler)

logger.setLevel(logging.INFO)
logger.propagate = False


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(
        CHAT_DB_PATH,
        timeout=10
    )
    conn.row_factory = sqlite3.Row
    return conn


# ==========================================
# 数据库初始化
# ==========================================

def init_chat_db() -> None:
    """
    初始化请求数据库。

    服务重新启动后，将未完成的请求
    标记为 unknown，避免自动重复调用模型。
    """

    with closing(connect_db()) as conn:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_requests (
                    request_id TEXT PRIMARY KEY,
                    message_hash TEXT NOT NULL,

                    status TEXT NOT NULL
                        CHECK (
                            status IN (
                                'processing',
                                'completed',
                                'unknown'
                            )
                        ),

                    reply TEXT,

                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_chat_status
                ON chat_requests(status)
                """
            )

            conn.execute(
                """
                UPDATE chat_requests
                SET status = 'unknown',
                    updated_at = ?
                WHERE status = 'processing'
                """,
                (now_utc(),)
            )

    logger.info("Chat request storage ready")


# ==========================================
# FastAPI 生命周期
# ==========================================

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_chat_db()
    yield


app = FastAPI(
    title="Personal Agent API",
    lifespan=lifespan
)


# ==========================================
# 请求模型
# ==========================================

class ChatRequest(BaseModel):
    message: str = Field(
        min_length=1,
        max_length=8000
    )

    request_id: str | None = None


# ==========================================
# 请求工具
# ==========================================

def validate_request_id(request_id: str) -> None:
    if not REQUEST_ID_RE.fullmatch(request_id):
        raise HTTPException(
            status_code=400,
            detail="无效的 request_id"
        )


def message_digest(message: str) -> str:
    return hashlib.sha256(
        message.encode("utf-8")
    ).hexdigest()


def read_record(request_id: str) -> dict | None:
    with closing(connect_db()) as conn:
        row = conn.execute(
            """
            SELECT
                request_id,
                message_hash,
                status,
                reply
            FROM chat_requests
            WHERE request_id = ?
            """,
            (request_id,)
        ).fetchone()

    return dict(row) if row is not None else None


# ==========================================
# 原子领取请求
# ==========================================

def claim_request(
    request_id: str,
    digest: str
) -> dict | None:
    """
    原子注册请求。

    返回 None：本次成功创建新请求。
    返回记录：同一个请求已经存在。
    """

    with closing(connect_db()) as conn:
        with conn:
            conn.execute("BEGIN IMMEDIATE")

            row = conn.execute(
                """
                SELECT
                    request_id,
                    message_hash,
                    status,
                    reply
                FROM chat_requests
                WHERE request_id = ?
                """,
                (request_id,)
            ).fetchone()

            if row is not None:
                previous = dict(row)

                if previous["message_hash"] != digest:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "同一个 request_id "
                            "不能对应不同消息"
                        )
                    )

                return previous

            timestamp = now_utc()

            conn.execute(
                """
                INSERT INTO chat_requests (
                    request_id,
                    message_hash,
                    status,
                    reply,
                    created_at,
                    updated_at
                )
                VALUES (
                    ?, ?, 'processing', NULL, ?, ?
                )
                """,
                (
                    request_id,
                    digest,
                    timestamp,
                    timestamp
                )
            )

    return None


# ==========================================
# 更新请求状态
# ==========================================

def update_status(
    request_id: str,
    status: str,
    reply: str | None = None
) -> bool:

    with closing(connect_db()) as conn:
        with conn:
            cursor = conn.execute(
                """
                UPDATE chat_requests
                SET status = ?,
                    reply = ?,
                    updated_at = ?
                WHERE request_id = ?
                  AND status = 'processing'
                """,
                (
                    status,
                    reply,
                    now_utc(),
                    request_id
                )
            )

            return cursor.rowcount == 1


# ==========================================
# 健康检查
# ==========================================

@app.get("/health")
def health():
    return {"status": "ok"}


# ==========================================
# 查询聊天请求状态
# ==========================================

@app.get("/chat/requests/{request_id}")
def get_chat_request(request_id: str):

    validate_request_id(request_id)

    record = read_record(request_id)

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="请求不存在"
        )

    return {
        "request_id": request_id,
        "status": record["status"],
        "reply": (
            record["reply"]
            if record["status"] == "completed"
            else None
        )
    }


# ==========================================
# 聊天接口
# ==========================================

@app.post("/chat")
def chat_endpoint(request: ChatRequest):

    message = request.message.strip()

    if not message:
        raise HTTPException(
            status_code=400,
            detail="消息不能为空"
        )

    # 兼容现有前端。
    # 完整防重复提交需要前端保存并复用 request_id。
    request_id = (
        request.request_id
        or uuid.uuid4().hex
    )

    validate_request_id(request_id)

    existing = claim_request(
        request_id,
        message_digest(message)
    )

    # --------------------------------------
    # 已存在的请求
    # --------------------------------------

    if existing is not None:

        if existing["status"] == "completed":

            logger.info(
                "Cache hit request_id=%s",
                request_id
            )

            return {
                "request_id": request_id,
                "status": "completed",
                "reply": existing["reply"]
            }

        if existing["status"] == "processing":

            return JSONResponse(
                status_code=202,
                content={
                    "request_id": request_id,
                    "status": "processing",
                    "detail": "请求仍在处理中，请查询状态"
                }
            )

        return JSONResponse(
            status_code=409,
            content={
                "request_id": request_id,
                "status": "unknown",
                "detail": (
                    "结果无法确认，为避免重复计费，"
                    "请勿自动重试"
                )
            }
        )

    # --------------------------------------
    # 新请求：调用模型
    # --------------------------------------

    logger.info(
        "Chat started request_id=%s",
        request_id
    )

    try:
        reply = chat(message)

        if (
            not isinstance(reply, str)
            or not reply.strip()
        ):
            raise ValueError(
                "模型没有返回有效文本"
            )

    except Exception:

        logger.exception(
            "Model request failed request_id=%s",
            request_id
        )

        try:
            update_status(
                request_id,
                "unknown"
            )

        except sqlite3.Error:
            logger.exception(
                "Failed recording unknown request_id=%s",
                request_id
            )

        return JSONResponse(
            status_code=502,
            content={
                "request_id": request_id,
                "status": "unknown",
                "detail": (
                    "模型调用结果无法确认，"
                    "请勿直接重复发送"
                )
            }
        )

    # --------------------------------------
    # 保存成功结果
    # --------------------------------------

    try:
        saved = update_status(
            request_id,
            "completed",
            reply
        )

    except sqlite3.Error:

        logger.exception(
            "Could not save model reply request_id=%s",
            request_id
        )

        return JSONResponse(
            status_code=503,
            content={
                "request_id": request_id,
                "status": "unknown",
                "detail": (
                    "回复保存失败，"
                    "结果需要人工确认"
                )
            }
        )

    if not saved:

        logger.error(
            "Request state changed before saving request_id=%s",
            request_id
        )

        return JSONResponse(
            status_code=409,
            content={
                "request_id": request_id,
                "status": "unknown",
                "detail": (
                    "请求状态发生变化，"
                    "请查询状态"
                )
            }
        )

    logger.info(
        "Chat completed request_id=%s",
        request_id
    )

    return {
        "request_id": request_id,
        "status": "completed",
        "reply": reply
    }