"""把路由决策写进 trajectory。

事件类型固定为 ``route_decided``；顺序上位于 ``run_started`` 之后、
审批与执行事件之前（见 docs/trajectory-schema.md 第 3 节）。
"""

from __future__ import annotations

from typing import Any

from src.routing.router import RouteDecision

EVENT_TYPE = "route_decided"


def record_route_decision(
    store: Any,
    run_id: str,
    decision: RouteDecision,
) -> dict[str, Any]:
    """追加一条 route_decided 事件，返回事件行。"""

    event = decision.to_event()
    payload = {key: value for key, value in event.items() if key != "event"}

    return store.append_event(
        run_id,
        event_type=EVENT_TYPE,
        status="routed" if decision.decision == "route" else "clarify",
        payload=payload,
    )
