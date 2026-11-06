"""弹性控制器：真实容量口径、决策与事件分离、apply 模式（W3 复核 R1 / R7 / R8）。"""

from __future__ import annotations

import unittest

from tempfile import TemporaryDirectory

from src.platform.autoscale import AutoscalePolicy
from src.platform.elastic_controller import (
    MODE_APPLY,
    MODE_COMPUTE_ONLY,
    ElasticController,
    ElasticTicker,
)
from src.platform.node_registry import NodeRegistry
from src.platform.scale_events import ScaleEventStore
from tests.helper import isolate_paths


class FakeQueue:
    """替身队列：只需要 ``snapshot()``。"""

    def __init__(self, active: int = 0, queued: int = 0):
        self.active = active
        self.queued = queued

    def snapshot(self) -> dict[str, int]:
        return {
            "active": self.active,
            "queued": self.queued,
            "max_active": 8,
            "max_queued": 64,
            "max_wait_seconds": 120.0,
        }


def make_policy(**overrides) -> AutoscalePolicy:
    base = {
        "min_nodes": 1,
        "max_nodes": 4,
        "capacity_per_node": 4,
        "headroom": 0.0,
        "scale_up_after": 1,
        "scale_down_cooldown_seconds": 0.0,
    }
    base.update(overrides)

    return AutoscalePolicy(**base)


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_paths(self, self.temp)

        self.registry = NodeRegistry()
        self.store = ScaleEventStore()
        self.queue = FakeQueue()

    def make(self, *, policy=None, apply=False) -> ElasticController:
        return ElasticController(
            policy=policy or make_policy(),
            registry=self.registry,
            store=self.store,
            queue=self.queue,
            apply=apply,
            wall_fn=lambda: 1_700_000_000.0,
            now_fn=lambda: 0.0,
        )

    # ------------------------------------------------------------------
    # 决策 / 事件分离（R8）
    # ------------------------------------------------------------------

    def test_compute_only_records_no_events(self):
        controller = self.make(apply=False)
        self.queue.active = 2

        decision = controller.tick(now=0.0)

        self.assertEqual(decision["action"], "scale_up")
        self.assertFalse(decision["applied"])
        self.assertEqual(decision["blocked"], MODE_COMPUTE_ONLY)
        self.assertIsNone(decision["event_id"])
        self.assertEqual(self.store.count(), 0)

    def test_pending_is_hold_not_event(self):
        controller = self.make(apply=False, policy=make_policy(scale_up_after=2))
        self.queue.active = 2

        first = controller.tick(now=0.0)
        second = controller.tick(now=1.0)

        self.assertEqual(first["action"], "none")
        self.assertEqual(first["label"], "hold")
        self.assertEqual(first["detail"], "pending")
        self.assertEqual(second["action"], "scale_up")
        self.assertEqual(second["desired_capacity"], 1)

    def test_steady_ticks_do_not_add_events(self):
        controller = self.make(apply=True)
        self.queue.active = 2

        controller.tick(now=0.0)
        before = len(self.store.recent(10))

        steady = controller.tick(now=1.0)

        self.assertEqual(steady["action"], "none")
        self.assertEqual(steady["detail"], "steady")
        self.assertEqual(len(self.store.recent(10)), before)

    # ------------------------------------------------------------------
    # 真实容量口径（R7）
    # ------------------------------------------------------------------

    def test_cordoned_node_is_not_actual_capacity(self):
        node = self.registry.register(capacity=8)
        self.registry.cordon(node["node_id"])

        capacity = self.make().capacity()

        self.assertEqual(capacity["nodes_total"], 1)
        self.assertEqual(capacity["nodes_accepting"], 0)
        self.assertEqual(capacity["nodes_cordoned"], 1)
        self.assertEqual(capacity["slots_accepting"], 0)

    # ------------------------------------------------------------------
    # apply 模式（R1）
    # ------------------------------------------------------------------

    def test_apply_registers_node_and_records_event(self):
        controller = self.make(apply=True)
        self.queue.active = 2

        decision = controller.tick(now=0.0)
        events = controller.store.recent(5)

        self.assertTrue(decision["applied"])
        self.assertEqual(decision["controller_mode"], MODE_APPLY)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["action"], "scale_up")
        self.assertEqual(events[0]["before_capacity"], 0)
        self.assertEqual(events[0]["after_capacity"], 1)
        self.assertEqual(events[0]["id"], decision["event_id"])
        self.assertEqual(controller.capacity()["nodes_accepting"], 1)

    def test_scale_down_drains_and_removes_owned_node(self):
        controller = self.make(apply=True)
        self.queue.active = 9

        for tick in range(3):
            controller.tick(now=float(tick))

        self.assertEqual(controller.capacity()["nodes_accepting"], 3)

        self.queue.active = 0
        decision = controller.tick(now=10.0)

        self.assertEqual(decision["action"], "scale_down")
        self.assertTrue(decision["applied"])
        self.assertEqual(controller.capacity()["nodes_accepting"], 2)

        events = self.store.recent(10)
        downs = [event for event in events if event["action"] == "scale_down"]

        self.assertEqual(len(downs), 1)
        self.assertEqual(downs[0]["before_capacity"], 3)
        self.assertEqual(downs[0]["after_capacity"], 2)

    def test_scale_down_without_owned_node_is_blocked(self):
        for _ in range(3):
            self.registry.register(capacity=4)

        controller = self.make(apply=True)
        self.queue.active = 0

        decision = controller.tick(now=0.0)

        self.assertEqual(decision["action"], "scale_down")
        self.assertFalse(decision["applied"])
        self.assertEqual(decision["blocked"], "no_owned_idle_node")
        self.assertEqual(self.store.count(), 0)

    # ------------------------------------------------------------------
    # 快照
    # ------------------------------------------------------------------

    def test_snapshot_exposes_policy_capacity_and_events(self):
        controller = self.make(apply=True)
        self.queue.active = 2

        controller.tick(now=0.0)
        snapshot = controller.snapshot(limit=5)

        self.assertEqual(snapshot["controller_mode"], MODE_APPLY)
        self.assertEqual(snapshot["capacity"]["unit"], "nodes")
        self.assertIn("scale_up_after", snapshot["policy"])
        self.assertEqual(snapshot["event_count"], 1)
        self.assertEqual(snapshot["last_decision"]["action"], "scale_up")
        self.assertEqual(len(snapshot["events"]), 1)

    # ------------------------------------------------------------------
    # 后台线程
    # ------------------------------------------------------------------

    def test_ticker_runs_and_stops_cleanly(self):
        """回归：`_stop` 不能覆盖 Thread._stop，否则 is_alive() 抛 TypeError。"""

        import time as _time

        controller = self.make(apply=False)
        self.queue.active = 2

        ticker = ElasticTicker(controller, interval_seconds=0.05)
        ticker.start()

        try:
            deadline = _time.monotonic() + 2.0

            while controller.last_decision is None and _time.monotonic() < deadline:
                _time.sleep(0.01)

            self.assertIsNotNone(controller.last_decision)
        finally:
            ticker.stop()
            ticker.join(timeout=2.0)

        self.assertFalse(ticker.is_alive())


if __name__ == "__main__":
    unittest.main()
