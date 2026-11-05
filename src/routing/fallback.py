"""模型/供应商故障时的安全降级。

原则：降级只换 provider / 模型，**不降风险等级、不绕过审批、不放宽执行约束**。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from src.routing.router import RouteDecision


@dataclass(frozen=True)
class FallbackPlan:
    provider: str
    model_policy: str
    risk: str
    requires_approval: bool
    constraints: dict
    reason: str


def plan_fallback(
    decision: RouteDecision,
    *,
    failed_provider: str,
    available_providers: tuple[str, ...] | list[str],
    preferred: tuple[str, ...] = ("deepseek", "local"),
) -> FallbackPlan:
    """按偏好顺序选一个可用 provider；没有可用 provider 时返回失败计划。"""

    candidates = [
        provider
        for provider in preferred
        if provider in available_providers and provider != failed_provider
    ]

    if not candidates:
        return FallbackPlan(
            provider="",
            model_policy=decision.model_policy,
            risk=decision.risk,
            requires_approval=decision.requires_approval,
            constraints=dict(decision.constraints),
            reason="no_available_provider",
        )

    provider = candidates[0]

    # 本地 provider 只能跑便宜/本地模型，不能假装还是强模型。
    model_policy = (
        "local" if provider == "local" else decision.model_policy
    )

    return FallbackPlan(
        provider=provider,
        model_policy=model_policy,
        risk=decision.risk,
        requires_approval=decision.requires_approval,
        constraints=dict(decision.constraints),
        reason=f"fallback_after:{failed_provider}",
    )


def apply_fallback(decision: RouteDecision, plan: FallbackPlan) -> RouteDecision:
    """把降级结果写回决策，风险与审批要求原样保留。"""

    if not plan.provider:
        raise RuntimeError("没有可用的 provider")

    return replace(
        decision,
        model_policy=plan.model_policy,
        reason=f"{decision.reason}|{plan.reason}",
    )
