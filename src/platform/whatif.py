"""What-if 模拟：只算不改（V2 §8.2）。

三条硬约束：

1. **复用同一个计算入口** —— 直接调 ``autoscale.desired_capacity``，
   前端和这里都不允许复制扩缩容公式；
2. **绝不改真实状态** —— 不碰节点表、队列、事件表与审计；
3. **结果必须自证是模拟** —— 每次带 ``simulation_id`` 与
   ``data_source="simulation"``，页面据此显示「仿真」。
"""

from __future__ import annotations

import uuid

from dataclasses import asdict, fields
from typing import Any

from src.platform.autoscale import AutoscalePolicy, desired_capacity
from src.platform.scale_events import describe

POLICY_FIELDS = {item.name: item for item in fields(AutoscalePolicy)}


def _build_policy(overrides: dict[str, Any] | None) -> AutoscalePolicy:
    if not overrides:
        return AutoscalePolicy()

    unknown = set(overrides) - set(POLICY_FIELDS)

    if unknown:
        raise ValueError(f"未知策略字段：{', '.join(sorted(unknown))}")

    values: dict[str, Any] = {}

    for name, raw in overrides.items():
        annotation = POLICY_FIELDS[name].type
        is_int = annotation in (int, "int")

        try:
            values[name] = int(raw) if is_int else float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"策略字段 {name} 数值非法：{raw!r}") from None

    policy = AutoscalePolicy(**values)

    if policy.min_nodes < 1 or policy.max_nodes < policy.min_nodes:
        raise ValueError("节点范围非法：需要 1 <= min_nodes <= max_nodes")

    if policy.capacity_per_node < 1:
        raise ValueError("capacity_per_node 必须是正整数")

    if policy.headroom < 0:
        raise ValueError("headroom 不能为负")

    if policy.scale_up_after < 1:
        raise ValueError("scale_up_after 至少为 1")

    if policy.scale_down_cooldown_seconds < 0:
        raise ValueError("scale_down_cooldown_seconds 不能为负")

    return policy


def simulate(
    *,
    running: int,
    queued: int,
    current_capacity: int,
    policy: AutoscalePolicy | None = None,
    policy_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """给一组负载与当前容量，算期望容量与动作；不触碰任何真实状态。"""

    for name, value in (
        ("running", running),
        ("queued", queued),
        ("current_capacity", current_capacity),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{name} 必须是非负整数")

    effective = policy if policy is not None else _build_policy(policy_overrides)

    desired = desired_capacity(running, queued, effective)

    if desired > current_capacity:
        action, reason = "scale_up", "load_above_capacity"
    elif desired < current_capacity:
        action, reason = "scale_down", "load_below_capacity"
    else:
        action, reason = "none", "steady"

    info = describe(action, reason)

    return {
        "simulation_id": f"sim-{uuid.uuid4().hex[:12]}",
        "data_source": "simulation",
        "applied": False,
        "message": "仅模拟，未改变真实容量、任务或审计",
        "inputs": {
            "running": running,
            "queued": queued,
            "current_capacity": current_capacity,
        },
        "policy": asdict(effective),
        "desired_capacity": desired,
        "action": action,
        "reason": reason,
        "label": info["label"],
        "title": info["title"],
        "delta": desired - current_capacity,
        "stabilization": {
            "required_samples": effective.scale_up_after,
            "note": "真实路径需要连续这么多次采样才会扩容；本结果假设已满足",
        },
        "cooldown": {
            "seconds": effective.scale_down_cooldown_seconds,
            "note": "真实缩容还要等冷却期结束；本结果假设已满足",
        },
    }
