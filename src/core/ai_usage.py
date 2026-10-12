
"""模型响应的 Token 用量记录。

不保存聊天内容或 API 密钥。
缺失的 usage 使用 NULL，不假设为零。
"""

import sqlite3

from collections.abc import Mapping
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def _get(obj, name):
    if isinstance(obj, Mapping):
        return obj.get(name)

    return getattr(obj, name, None)


def _token(value):
    if type(value) is int and value >= 0:
        return value

    return None


class AIUsageStore:

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

        self.db_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.init_db()

    def _connect(self):
        conn = sqlite3.connect(
            self.db_path,
            timeout=10,
        )

        conn.row_factory = sqlite3.Row

        return conn

    # ======================================
    # 初始化用量数据库
    # ======================================

    def init_db(self):
        with closing(self._connect()) as conn, conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS ai_usage_calls (
                    request_id TEXT NOT NULL,

                    call_number INTEGER NOT NULL
                        CHECK(call_number >= 1),

                    user_id TEXT NOT NULL,
                    model TEXT NOT NULL,

                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    total_tokens INTEGER,
                    cache_hit_tokens INTEGER,

                    created_at TEXT NOT NULL,

                    PRIMARY KEY (
                        request_id,
                        call_number
                    )
                );

                CREATE INDEX IF NOT EXISTS
                    idx_ai_usage_user
                ON ai_usage_calls(user_id);
                """
            )

    # ======================================
    # 记录一次模型调用
    # ======================================

    def record_completion(
        self,
        *,
        user_id: str,
        request_id: str,
        call_number: int,
        model: str,
        response,
    ) -> bool:

        # 身份必须由服务端提供
        for name, value in (
            ("user_id", user_id),
            ("request_id", request_id),
            ("model", model),
        ):
            if (
                not isinstance(value, str)
                or not value.strip()
            ):
                raise ValueError(
                    f"{name} 不能为空"
                )

        if (
            type(call_number) is not int
            or call_number < 1
        ):
            raise ValueError(
                "call_number 必须是正整数"
            )

        # OpenAI 兼容接口通常从
        # response.usage 返回 Token 数据
        usage = _get(
            response,
            "usage",
        )

        prompt = _token(
            _get(usage, "prompt_tokens")
        )

        completion = _token(
            _get(usage, "completion_tokens")
        )

        total = _token(
            _get(usage, "total_tokens")
        )

        # 部分兼容服务提供此字段
        cached = _token(
            _get(
                usage,
                "prompt_cache_hit_tokens",
            )
        )

        # 兼容另一种缓存用量结构
        if cached is None:
            details = _get(
                usage,
                "prompt_tokens_details",
            )

            cached = _token(
                _get(details, "cached_tokens")
            )

        timestamp = (
            datetime.now(timezone.utc).isoformat()
        )

        with closing(self._connect()) as conn, conn:

            # 相同请求的相同调用序号
            # 只能记录一次
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO ai_usage_calls (
                    request_id,
                    call_number,
                    user_id,
                    model,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    cache_hit_tokens,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    call_number,
                    user_id,
                    model,
                    prompt,
                    completion,
                    total,
                    cached,
                    timestamp,
                ),
            )

            return cursor.rowcount == 1

    # ======================================
    # 查询指定用户的用量汇总
    # ======================================

    def user_summary(
        self,
        user_id: str,
    ) -> dict:

        if (
            not isinstance(user_id, str)
            or not user_id.strip()
        ):
            raise ValueError(
                "user_id 不能为空"
            )

        with closing(self._connect()) as conn:

            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS calls,

                    SUM(
                        CASE
                            WHEN total_tokens IS NULL
                            THEN 1
                            ELSE 0
                        END
                    ) AS unreported_calls,

                    COALESCE(
                        SUM(prompt_tokens),
                        0
                    ) AS observed_prompt_tokens,

                    COALESCE(
                        SUM(completion_tokens),
                        0
                    ) AS observed_completion_tokens,

                    COALESCE(
                        SUM(total_tokens),
                        0
                    ) AS observed_total_tokens,

                    COALESCE(
                        SUM(cache_hit_tokens),
                        0
                    ) AS observed_cache_hit_tokens

                FROM ai_usage_calls

                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()

        return dict(row)