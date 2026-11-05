"""确定性审批闸门：风险由规则判定，概率不能降低权限。

Router 只给建议（risk / requires_approval）；真正能不能执行由这里裁定。
"""

from __future__ import annotations

from dataclasses import dataclass

from src.routing.router import RouteDecision


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    requires_approval: bool
    reason: str


def approval_gate(
    decision: RouteDecision,
    *,
    approved: bool | None = None,
) -> GateResult:
    """返回平台侧准入结论。

    - clarify：直接拒绝（先澄清再执行）
    - high：必须审批；approved=True 才放行，**不看置信度**
    - medium：默认放行；显式 approved=False 时拒绝
    - low：放行
    """

    if decision.decision == "clarify":
        return GateResult(False, False, "clarify_first")

    if decision.risk == "high":
        if approved is True:
            return GateResult(True, False, "high_risk_approved")

        return GateResult(False, True, "high_risk_requires_approval")

    if decision.risk == "medium" and approved is False:
        return GateResult(False, True, "medium_rejected")

    return GateResult(True, False, "allowed")
