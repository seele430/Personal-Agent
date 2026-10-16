"""聊天请求存储与校验工具库。

正式聊天 API 位于 src/main.py（带认证），本模块只提供共享工具：
数据库路径与连接、启动初始化、request_id 校验、消息摘要和请求模型。

旧的无认证入口 src.api:app 已下线。
"""

import hashlib
import logging
import re
import sqlite3

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, Field

# 正式 API 通过本模块调用模型（legacy_chat.chat），测试也在这里 patch。
from src.agent import chat  # noqa: F401
from src.config import paths


# ==========================================
# 基础配置
# ==========================================

BASE_DIR = Path(__file__).resolve().parent.parent

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
        paths.chat_requests_db,
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
# 请求模型
# ==========================================

class ChatRequest(BaseModel):
    message: str = Field(
        min_length=1,
        max_length=8000
    )

    request_id: str | None = None

    # 可选：单人聊天时选中的 Bot（S3）。
    bot_id: int | None = None


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