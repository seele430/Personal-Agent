
"""AI 预算结算核对器。

只根据完整、匹配的模型用量释放预算差额。
缺失、冲突或无法计价的记录一律按未知费用处理。

注意：只有正式后端才能提供 expected_calls，
不能信任浏览器提交的调用次数。
"""

import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from src.core.ai_budget import settle_budget
from src.core.ai_pricing import (
    PricingUnavailable,
    estimate_peak_cost_usd,
    validate_provider,
)


def _valid_tokens(value: object) -> bool:
    return type(value) is int and value >= 0


def observed_request_cost(
    *,
    usage_db_path: str | Path,
    request_id: str,
    user_id: str,
    expected_calls: int,
    provider_host: str,
    model: str,
) -> Decimal | None:
    """核对指定聊天请求的完整模型用量。

    返回 Decimal：每次调用均有完整可信的用量记录。
    返回 None：记录缺失、冲突或无法计价。

    只读数据库，不执行模型调用。
    """

    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError("request_id 不能为空")

    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("user_id 不能为空")

    if type(expected_calls) is not int or expected_calls not in (1, 2):
        raise ValueError("当前只支持核对 1～2 次模型调用")

    try:
        validate_provider(provider_host, model)
    except (PricingUnavailable, ValueError, AttributeError):
        return None

    path = Path(usage_db_path).resolve()

    if not path.is_file():
        return None

    # 只读打开用量数据库。
    # 缺少数据库时不创建空文件。
    uri = path.as_uri() + "?mode=ro"

    with closing(sqlite3.connect(uri, uri=True)) as conn:
        rows = conn.execute(
            """
            SELECT
                call_number,
                user_id,
                model,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                cache_hit_tokens
            FROM ai_usage_calls
            WHERE request_id = ?
            ORDER BY call_number
            """,
            (request_id,),
        ).fetchall()

    # 不能只找到第 1 次调用，就假设第 2 次免费。
    # 也不能允许出现额外的未核对调用。
    if len(rows) != expected_calls:
        return None

    total_cost = Decimal("0")

    for expected_number, row in enumerate(rows, start=1):
        (
            call_number,
            row_user_id,
            row_model,
            prompt_tokens,
            completion_tokens,
            total_tokens,
            cached_tokens,
        ) = row

        if call_number != expected_number:
            return None

        if row_user_id != user_id or row_model != model:
            return None

        if not all(
            _valid_tokens(value)
            for value in (
                prompt_tokens,
                completion_tokens,
                total_tokens,
            )
        ):
            return None

        # 防止不完整或自相矛盾的 usage 被计价。
        if total_tokens != prompt_tokens + completion_tokens:
            return None

        if cached_tokens is not None:
            if (
                not _valid_tokens(cached_tokens)
                or cached_tokens > prompt_tokens
            ):
                return None

        try:
            cost = estimate_peak_cost_usd(
                provider_host=provider_host,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cache_hit_tokens=cached_tokens,
            )
        except (PricingUnavailable, ValueError):
            return None

        if cost is None:
            return None

        total_cost += cost

    return total_cost


def reconcile_recorded_request(
    *,
    budget_db_path: str | Path,
    usage_db_path: str | Path,
    request_id: str,
    user_id: str,
    provider_host: str,
    model: str,
    completed: bool,
    expected_calls: int | None,
) -> str:
    """结算一次聊天的预算记录。

    只有聊天成功完成、且所有模型调用用量完整时，
    才按已观测到的保守费用结算。

    其余情况保留全部预留额。

    返回 ai_budget.py 的结算状态：
    settled / uncertain / overrun。

    已处于 uncertain 的历史记录不能自动重新结算。
    """

    if type(completed) is not bool:
        raise ValueError("completed 必须是布尔值")

    actual_cost = None

    if (
        completed
        and type(expected_calls) is int
        and expected_calls in (1, 2)
    ):
        actual_cost = observed_request_cost(
            usage_db_path=usage_db_path,
            request_id=request_id,
            user_id=user_id,
            expected_calls=expected_calls,
            provider_host=provider_host,
            model=model,
        )

    path = Path(budget_db_path)

    if not path.is_file():
        raise FileNotFoundError("预算数据库不存在")

    with closing(sqlite3.connect(path, timeout=10)) as conn:
        return settle_budget(
            conn,
            request_id=request_id,
            user_id=user_id,
            actual_usd=actual_cost,
        )