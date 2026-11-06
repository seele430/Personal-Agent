"""W3 弹性控制器：把 ``autoscale`` 的决策接到真实容量上。

一次 tick 的链路：

    队列快照 → running / queued
    节点表   → 真实可用容量（accepting 的节点数）
    Autoscaler.observe → 决策（每 tick 都有，包含 hold）

口径（W3 复核 R1 / R8）：

- **决策**每 tick 都产生并留在内存（``last_decision`` / ``recent_decisions``），
  包含 hold 类（等待稳定期、冷却中、稳定）；
- **事件**只在真实容量发生变化时才落库（``platform_scale_events``）。
  默认 ``apply=False`` 时控制器只计算不驱动，此时 ``blocked="compute_only"``，
  页面必须显示“已计算，未驱动真实容量”，不得把决策当成扩容成功。

环境开关（都默认关闭，测试与本地不会自动改真实节点）：

- ``PERSONAL_AGENT_ELASTIC=1``        启动后台 tick 线程；
- ``PERSONAL_AGENT_ELASTIC_APPLY=1``  允许真的注册 / 摘除节点；
- ``PERSONAL_AGENT_ELASTIC_INTERVAL`` tick 周期（秒，默认 30）。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
import uuid

from collections import deque
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src import config
from src.platform.autoscale import AutoscalePolicy, Autoscaler
from src.platform.node_registry import (
    InvalidNodeState,
    NodeBusy,
    NodeNotFound,
    NodeRegistry,
    StaleGeneration,
)
from src.platform.scale_events import (
    ACTION_SCALE_DOWN,
    ACTION_SCALE_UP,
    CAPACITY_UNIT_NODES,
    SOURCE_LIVE,
    ScaleEventStore,
    describe,
    init_scale_schema,
    is_change,
)

logger = logging.getLogger("personal_agent.platform.elastic")

ENABLED_ENV = "PERSONAL_AGENT_ELASTIC"
APPLY_ENV = "PERSONAL_AGENT_ELASTIC_APPLY"
INTERVAL_ENV = "PERSONAL_AGENT_ELASTIC_INTERVAL"

DEFAULT_INTERVAL_SECONDS = 30.0

MODE_APPLY = "apply"
MODE_COMPUTE_ONLY = "compute_only"


def _flag(name: str, default: str = "0") -> bool:
    raw = os.getenv(name, default).strip().lower()

    return raw not in ("", "0", "off", "false", "no")


def enabled() -> bool:
    return _flag(ENABLED_ENV, "0")


def apply_enabled() -> bool:
    return _flag(APPLY_ENV, "0")


def tick_interval_seconds() -> float:
    raw = os.getenv(INTERVAL_ENV, "").strip()

    if not raw:
        return DEFAULT_INTERVAL_SECONDS

    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_INTERVAL_SECONDS

    return value if value > 0 else DEFAULT_INTERVAL_SECONDS


def ensure_schema() -> None:
    """启动时把事件表建好，读接口不背建表的副作用。"""

    path = Path(config.paths.platform_db)
    path.parent.mkdir(parents=True, exist_ok=True)

    with closing(sqlite3.connect(path, timeout=30)) as conn, conn:
        init_scale_schema(conn)


class ElasticController:
    def __init__(
        self,
        *,
        policy: AutoscalePolicy | None = None,
        registry: NodeRegistry | None = None,
        store: ScaleEventStore | None = None,
        queue=None,
        wall_fn=time.time,
        now_fn=None,
        apply: bool | None = None,
        capacity_unit: str = CAPACITY_UNIT_NODES,
        max_decisions: int = 60,
    ):
        self.policy = policy or AutoscalePolicy()
        self.registry = registry if registry is not None else NodeRegistry()
        self.store = store if store is not None else ScaleEventStore()
        self.apply = apply_enabled() if apply is None else bool(apply)
        self.capacity_unit = capacity_unit
        self.last_decision: dict[str, Any] | None = None
        self._queue = queue
        self._decisions: deque[dict[str, Any]] = deque(maxlen=max(1, max_decisions))
        self._owned: dict[str, dict[str, int]] = {}
        self._run_seq = 0

        self.scaler = Autoscaler(
            self.policy,
            now_fn=now_fn if now_fn is not None else time.monotonic,
            wall_fn=wall_fn,
        )

    # ------------------------------------------------------------------
    # 观测
    # ------------------------------------------------------------------

    @property
    def mode(self) -> str:
        return MODE_APPLY if self.apply else MODE_COMPUTE_ONLY

    def _queue_snapshot(self) -> dict[str, int]:
        if self._queue is not None:
            return dict(self._queue.snapshot())

        from src.platform.exec_queue import get_queue

        return dict(get_queue().snapshot())

    def capacity(self) -> dict[str, Any]:
        """真实可用容量：只有 ``accepting`` 的节点算数（口径见 R7）。

        cordon / drain / 租约过期 / 已移除的节点都不计入，但计数单独给出，
        便于页面解释“为什么容量比节点数少”。
        """

        nodes = self.registry.list_nodes()
        accepting = [node for node in nodes if node.get("accepting")]

        return {
            "nodes_total": len(nodes),
            "nodes_accepting": len(accepting),
            "nodes_cordoned": sum(1 for n in nodes if n.get("cordoned")),
            "nodes_draining": sum(1 for n in nodes if n.get("draining")),
            "nodes_expired": sum(1 for n in nodes if n.get("expired")),
            "slots_accepting": sum(int(n["capacity"]) for n in accepting),
            "unit": self.capacity_unit,
        }

    # ------------------------------------------------------------------
    # 一次 tick
    # ------------------------------------------------------------------

    def tick(self, *, now: float | None = None) -> dict[str, Any]:
        queue = self._queue_snapshot()
        running = int(queue.get("active", 0))
        queued = int(queue.get("queued", 0))

        before = self.capacity()
        current = before["nodes_accepting"]

        decision = self.scaler.observe(
            running=running,
            queued=queued,
            current=current,
            now=now,
        )

        applied = False
        blocked: str | None = None
        event: dict[str, Any] | None = None

        if is_change(decision.action):
            if not self.apply:
                blocked = MODE_COMPUTE_ONLY
            else:
                applied, blocked = self._apply(decision)

                if applied:
                    event = self._record_event(
                        decision,
                        running=running,
                        queued=queued,
                        before=current,
                    )
                else:
                    self._heartbeat_owned()
        else:
            self._heartbeat_owned()

        after = self.capacity()["nodes_accepting"] if applied else current

        payload: dict[str, Any] = {
            "at": decision.at,
            "action": decision.action,
            "reason": decision.reason,
            "desired_capacity": decision.desired,
            "actual_capacity": current,
            "actual_capacity_after": after,
            "capacity_unit": self.capacity_unit,
            "running": running,
            "queued": queued,
            "applied": applied,
            "blocked": blocked,
            "controller_mode": self.mode,
            "data_source": SOURCE_LIVE,
            "event_id": event["id"] if event else None,
            **describe(decision.action, decision.reason),
        }

        self._decisions.append(payload)
        self.last_decision = payload

        return payload

    # ------------------------------------------------------------------
    # 执行（只有 apply=True 才会走到）
    # ------------------------------------------------------------------

    def _apply(self, decision) -> tuple[bool, str | None]:
        if decision.action == ACTION_SCALE_UP:
            return self._apply_up()
        if decision.action == ACTION_SCALE_DOWN:
            return self._apply_down()

        return False, "not_a_change"

    def _apply_up(self) -> tuple[bool, str | None]:
        try:
            node = self.registry.register(capacity=self.policy.capacity_per_node)
        except Exception as exc:  # noqa: BLE001 - 扩容失败要落到决策里，不能吞
            logger.exception("扩容失败：注册节点出错")
            return False, type(exc).__name__

        self._owned[node["node_id"]] = {
            "generation": int(node["generation"]),
            "capacity": int(node["capacity"]),
        }

        return True, None

    def _apply_down(self) -> tuple[bool, str | None]:
        node_id = self._pick_idle_owned()

        if node_id is None:
            return False, "no_owned_idle_node"

        try:
            self.registry.drain(node_id)
            self.registry.remove(node_id)
        except NodeBusy as exc:
            logger.info("缩容受阻：%s", exc)
            return False, type(exc).__name__
        except InvalidNodeState as exc:
            logger.info("缩容受阻：%s", exc)
            self._owned.pop(node_id, None)
            return False, type(exc).__name__
        except Exception as exc:  # noqa: BLE001
            logger.exception("缩容失败：摘除 %s 出错", node_id)
            return False, type(exc).__name__

        self._owned.pop(node_id, None)

        return True, None

    def _pick_idle_owned(self) -> str | None:
        from src.platform.sandbox_service import SandboxService

        service = SandboxService()

        for node_id in list(self._owned):
            try:
                view = self.registry.get(node_id)
            except NodeNotFound:
                self._owned.pop(node_id, None)
                continue

            if not view.get("accepting"):
                self._owned.pop(node_id, None)
                continue

            if service.count_active(node_id) == 0:
                return node_id

        return None

    def _record_event(self, decision, *, running: int, queued: int, before: int):
        self._run_seq += 1
        after = self.capacity()["nodes_accepting"]

        return self.store.record(
            action=decision.action,
            reason=decision.reason,
            before_capacity=before,
            after_capacity=after,
            running=running,
            queued=queued,
            capacity_unit=self.capacity_unit,
            source=SOURCE_LIVE,
            controller_run_id=uuid.uuid4().hex[:12],
            at=decision.at,
        )

    def _heartbeat_owned(self) -> None:
        """自己注册的节点由控制器续租，否则 3 个心跳周期后会被判 not_ready。"""

        for node_id, meta in list(self._owned.items()):
            try:
                view = self.registry.heartbeat(
                    node_id, generation=meta["generation"]
                )
                meta["generation"] = int(view["generation"])
            except StaleGeneration:
                try:
                    meta["generation"] = int(self.registry.get(node_id)["generation"])
                except NodeNotFound:
                    self._owned.pop(node_id, None)
            except NodeNotFound:
                self._owned.pop(node_id, None)
            except Exception:  # noqa: BLE001 - 续租失败不该打断 tick
                logger.exception("续租失败：%s", node_id)

    # ------------------------------------------------------------------
    # 只读快照
    # ------------------------------------------------------------------

    def snapshot(self, *, limit: int = 20) -> dict[str, Any]:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "controller_mode": self.mode,
            "data_source": SOURCE_LIVE,
            "capacity": self.capacity(),
            "policy": asdict(self.policy),
            "last_decision": self.last_decision,
            "recent_decisions": list(self._decisions)[-5:],
            "events": self.store.recent(limit),
            "event_count": self.store.count(),
        }


class ElasticTicker(threading.Thread):
    """定时 tick；daemon，随应用关闭一起停。"""

    def __init__(
        self,
        controller: ElasticController,
        interval_seconds: float | None = None,
    ):
        super().__init__(name="platform-elastic", daemon=True)
        self.controller = controller
        self.interval_seconds = (
            interval_seconds
            if interval_seconds is not None
            else tick_interval_seconds()
        )
        # 不能叫 _stop：threading.Thread 内部有同名方法，覆盖后线程退出时
        # is_alive() 会把 Event 当函数调用，抛 TypeError。
        self._stopped = threading.Event()

    def run(self) -> None:  # noqa: D102
        while not self._stopped.wait(self.interval_seconds):
            try:
                self.controller.tick()
            except Exception:  # noqa: BLE001 - 一次失败不能让线程死掉
                logger.exception("elastic tick 失败")

    def stop(self) -> None:
        self._stopped.set()


_instance: ElasticTicker | None = None
_controller: ElasticController | None = None


def get_controller() -> ElasticController | None:
    return _controller


def start_controller(
    controller: ElasticController | None = None,
) -> ElasticTicker | None:
    """按环境开关启动控制器；默认不启动，测试不需要等真实时间。"""

    global _instance, _controller

    if controller is None and not enabled():
        return None

    if _instance is not None and _instance.is_alive():
        return _instance

    _controller = controller or ElasticController()
    _instance = ElasticTicker(_controller)
    _instance.start()

    return _instance


def stop_controller() -> None:
    global _instance, _controller

    if _instance is not None:
        _instance.stop()
        _instance = None

    _controller = None
