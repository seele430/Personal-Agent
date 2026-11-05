"""弹性扩缩容：期望容量计算 + 带稳定性与冷却的控制器。

纯逻辑，不直接改数据库；调用方（脚本或平台）按决策去注册/摘除节点，
并把 ``ScaleEvent`` 写进审计。
"""

from __future__ import annotations

import math
import time

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class AutoscalePolicy:
    min_nodes: int = 1
    max_nodes: int = 4
    capacity_per_node: int = 8
    headroom: float = 0.2
    scale_up_after: int = 2
    scale_down_cooldown_seconds: float = 60.0

    def clamp(self, value: int) -> int:
        return max(self.min_nodes, min(self.max_nodes, value))


@dataclass(frozen=True)
class ScaleEvent:
    action: str
    current: int
    desired: int
    reason: str
    at: str


@dataclass(frozen=True)
class ScaleDecision:
    action: str
    current: int
    desired: int
    reason: str
    at: str


def desired_capacity(
    running: int,
    queued: int,
    policy: AutoscalePolicy,
) -> int:
    """按「运行 + 排队」与每节点容量算期望节点数，预留 headroom。"""

    demand = max(0, running) + max(0, queued)
    needed = math.ceil(
        demand * (1 + policy.headroom) / max(policy.capacity_per_node, 1)
    )

    return policy.clamp(max(needed, policy.min_nodes))


class Autoscaler:
    def __init__(
        self,
        policy: AutoscalePolicy | None = None,
        *,
        now_fn=time.monotonic,
    ):
        self.policy = policy or AutoscalePolicy()
        self._now = now_fn
        self._up_streak = 0
        self._last_change: float | None = None
        self.events: list[ScaleEvent] = []

    def observe(
        self,
        *,
        running: int,
        queued: int,
        current: int,
        now: float | None = None,
    ) -> ScaleDecision:
        moment = self._now() if now is None else now
        desired = desired_capacity(running, queued, self.policy)

        if desired > current:
            self._up_streak += 1

            if self._up_streak >= self.policy.scale_up_after:
                self._up_streak = 0
                self._last_change = moment
                return self._decide(
                    "scale_up",
                    current,
                    desired,
                    "load_above_capacity",
                    moment,
                )

            return self._decide(
                "none",
                current,
                desired,
                f"scale_up_pending:{self._up_streak}",
                moment,
            )

        self._up_streak = 0

        if desired < current:
            if (
                self._last_change is not None
                and moment - self._last_change
                < self.policy.scale_down_cooldown_seconds
            ):
                return self._decide(
                    "none",
                    current,
                    desired,
                    "scale_down_cooldown",
                    moment,
                )

            self._last_change = moment
            return self._decide(
                "scale_down",
                current,
                desired,
                "load_below_capacity",
                moment,
            )

        return self._decide("none", current, desired, "steady", moment)

    def _decide(
        self,
        action: str,
        current: int,
        desired: int,
        reason: str,
        moment: float,
    ) -> ScaleDecision:
        at = datetime.fromtimestamp(moment, timezone.utc).isoformat()

        decision = ScaleDecision(
            action=action,
            current=current,
            desired=desired,
            reason=reason,
            at=at,
        )

        if action != "none":
            self.events.append(
                ScaleEvent(
                    action=action,
                    current=current,
                    desired=desired,
                    reason=reason,
                    at=at,
                )
            )

        return decision
