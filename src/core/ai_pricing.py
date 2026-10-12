
"""DeepSeek Flash 保守费用估算。

只读 Token 用量，不修改数据库。
本模块不是硬预算执行器。

费率快照：2026-10-12
来源：https://api-docs.deepseek.com/quick_start/pricing/

统一采用高峰费率，单位 USD / 100 万 Token。
"""

import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path


PRICING_SNAPSHOT = "2026-10-12"

SUPPORTED_HOST = "api.deepseek.com"
SUPPORTED_MODEL = "deepseek-flash"

MILLION = Decimal("1000000")

CACHE_HIT_RATE = Decimal("0.006")
CACHE_MISS_RATE = Decimal("0.30")
OUTPUT_RATE = Decimal("1.20")


class PricingUnavailable(ValueError):
    """当前服务商或模型没有可信的价格配置。"""


def validate_provider(
    provider_host: str,
    model: str,
) -> None:
    """未知模型或服务商必须拒绝估价。"""

    if (
        provider_host.strip().lower() != SUPPORTED_HOST
        or model != SUPPORTED_MODEL
    ):
        raise PricingUnavailable(
            "没有该服务商及模型的已核实价格"
        )


def _check_tokens(
    name: str,
    value: int | None,
) -> None:
    if value is None:
        return

    if type(value) is not int or value < 0:
        raise ValueError(
            f"{name} 必须是非负整数或 None"
        )


def estimate_peak_cost_usd(
    *,
    provider_host: str,
    model: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    cache_hit_tokens: int | None = None,
) -> Decimal | None:
    """估算已知 Token 的高峰费率费用。

    返回 Decimal：输入、输出 Token 数均已知。
    返回 None：关键 Token 数缺失，费用未知。

    若缓存命中数没有提供，则保守地假设全部
    输入 Token 都按较贵的缓存未命中费率计算。

    该值仅对当前价格快照和已知用量有效。
    """

    validate_provider(provider_host, model)

    _check_tokens("prompt_tokens", prompt_tokens)
    _check_tokens(
        "completion_tokens",
        completion_tokens,
    )
    _check_tokens(
        "cache_hit_tokens",
        cache_hit_tokens,
    )

    if (
        prompt_tokens is None
        or completion_tokens is None
    ):
        return None

    cached = (
        cache_hit_tokens
        if cache_hit_tokens is not None
        else 0
    )

    if cached > prompt_tokens:
        raise ValueError(
            "缓存命中 Token 不能超过输入 Token"
        )

    uncached = prompt_tokens - cached

    input_cost = (
        Decimal(uncached) * CACHE_MISS_RATE
        + Decimal(cached) * CACHE_HIT_RATE
    ) / MILLION

    output_cost = (
        Decimal(completion_tokens) * OUTPUT_RATE
    ) / MILLION

    return input_cost + output_cost


def user_observed_cost_report(
    *,
    db_path: str | Path,
    user_id: str,
    provider_host: str,
) -> dict:
    """只读汇总某个用户已记录的模型调用费用。

    没有 usage 的调用计入 unpriced_calls。
    不会将缺失用量当成零费用。
    """

    validate_provider(
        provider_host,
        SUPPORTED_MODEL,
    )

    if (
        not isinstance(user_id, str)
        or not user_id.strip()
    ):
        raise ValueError("user_id 不能为空")

    path = Path(db_path).resolve()

    if not path.is_file():
        raise FileNotFoundError(
            "模型用量数据库不存在"
        )

    # 使用只读 SQLite 连接，不创建或修改数据。
    uri = path.as_uri() + "?mode=ro"

    with closing(
        sqlite3.connect(uri, uri=True)
    ) as conn:

        rows = conn.execute(
            """
            SELECT
                model,
                prompt_tokens,
                completion_tokens,
                cache_hit_tokens
            FROM ai_usage_calls
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchall()

    known_cost = Decimal("0")
    priced_calls = 0
    unpriced_calls = 0

    for (
        model,
        prompt,
        completion,
        cached,
    ) in rows:

        try:
            cost = estimate_peak_cost_usd(
                provider_host=provider_host,
                model=model,
                prompt_tokens=prompt,
                completion_tokens=completion,
                cache_hit_tokens=cached,
            )

        except (PricingUnavailable, ValueError):
            cost = None

        if cost is None:
            unpriced_calls += 1
        else:
            priced_calls += 1
            known_cost += cost

    return {
        "user_id": user_id,
        "pricing_snapshot": PRICING_SNAPSHOT,
        "currency": "USD",
        "total_recorded_calls": len(rows),
        "priced_calls": priced_calls,
        "unpriced_calls": unpriced_calls,

        # 仅针对能够计价的记录。
        # 不能将此数字视为用户真实总花费。
        "known_calls_peak_usd": str(
            known_cost.quantize(
                Decimal("0.000000000001")
            )
        ),
    }