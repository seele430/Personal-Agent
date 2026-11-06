"""供应商故障时的安全降级。

原则：降级只换 provider，**不降风险等级、不绕过审批、不放宽执行约束**。
（成本/模型档位路由未实现，`model_policy` 字段已移除。）
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from src.routing.router import RouteDecision


@dataclass(frozen=True)
class FallbackPlan:
    provider: str
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
            risk=decision.risk,
            requires_approval=decision.requires_approval,
            constraints=dict(decision.constraints),
            reason="no_available_provider",
        )

    return FallbackPlan(
        provider=candidates[0],
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
        reason=f"{decision.reason}|{plan.reason}",
    )
