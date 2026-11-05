"""期望容量与扩缩容控制器。"""

from __future__ import annotations

import unittest

from src.platform.autoscale import (
    AutoscalePolicy,
    Autoscaler,
    desired_capacity,
)


class DesiredCapacityTest(unittest.TestCase):
    def test_idle_keeps_minimum(self):
        policy = AutoscalePolicy(min_nodes=1, capacity_per_node=8, headroom=0.2)

        self.assertEqual(desired_capacity(0, 0, policy), 1)

    def test_grows_with_queue(self):
        policy = AutoscalePolicy(min_nodes=1, capacity_per_node=4, headroom=0.0)

        self.assertEqual(desired_capacity(6, 0, policy), 2)
        self.assertEqual(desired_capacity(6, 2, policy), 2)
        self.assertEqual(desired_capacity(8, 1, policy), 3)

    def test_headroom_adds_capacity(self):
        policy = AutoscalePolicy(
            min_nodes=1,
            max_nodes=8,
            capacity_per_node=10,
            headroom=0.2,
        )

        self.assertEqual(desired_capacity(8, 0, policy), 1)
        self.assertEqual(desired_capacity(9, 0, policy), 2)

    def test_clamped_to_max(self):
        policy = AutoscalePolicy(
            min_nodes=1,
            max_nodes=3,
            capacity_per_node=2,
            headroom=0.0,
        )

        self.assertEqual(desired_capacity(100, 50, policy), 3)


class AutoscalerTest(unittest.TestCase):
    def make(self, **kwargs):
        policy = AutoscalePolicy(
            min_nodes=1,
            max_nodes=4,
            capacity_per_node=4,
            headroom=0.0,
            scale_up_after=2,
            scale_down_cooldown_seconds=60.0,
            **kwargs,
        )
        return Autoscaler(policy, now_fn=lambda: 0.0)

    def test_scale_up_needs_consecutive_samples(self):
        scaler = self.make()

        first = scaler.observe(running=9, queued=0, current=1, now=0)
        second = scaler.observe(running=9, queued=0, current=1, now=10)

        self.assertEqual(first.action, "none")
        self.assertEqual(first.reason, "scale_up_pending:1")
        self.assertEqual(second.action, "scale_up")
        self.assertEqual(second.desired, 3)

    def test_scale_down_respects_cooldown(self):
        scaler = self.make()

        scaler.observe(running=9, queued=0, current=1, now=0)
        up = scaler.observe(running=9, queued=0, current=1, now=10)
        self.assertEqual(up.action, "scale_up")

        blocked = scaler.observe(running=0, queued=0, current=3, now=30)
        self.assertEqual(blocked.action, "none")
        self.assertEqual(blocked.reason, "scale_down_cooldown")

        allowed = scaler.observe(running=0, queued=0, current=3, now=80)
        self.assertEqual(allowed.action, "scale_down")
        self.assertEqual(allowed.desired, 1)

    def test_steady_state_no_event(self):
        scaler = self.make()

        decision = scaler.observe(running=2, queued=0, current=1, now=0)

        self.assertEqual(decision.action, "none")
        self.assertEqual(decision.reason, "steady")
        self.assertEqual(scaler.events, [])

    def test_events_recorded(self):
        scaler = self.make()

        scaler.observe(running=9, queued=0, current=1, now=0)
        scaler.observe(running=9, queued=0, current=1, now=10)

        self.assertEqual(len(scaler.events), 1)
        self.assertEqual(scaler.events[0].action, "scale_up")
        self.assertEqual(scaler.events[0].current, 1)
        self.assertEqual(scaler.events[0].desired, 3)


if __name__ == "__main__":
    unittest.main()
