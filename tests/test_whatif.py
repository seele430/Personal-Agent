"""What-if 模拟：与真实决策一致、策略覆盖、非法输入（V2 §8.2 / §8.3）。"""

from __future__ import annotations

import unittest

from src.platform.autoscale import AutoscalePolicy, desired_capacity
from src.platform.whatif import simulate


class ParityTest(unittest.TestCase):
    def test_desired_matches_real_formula_for_many_inputs(self):
        policy = AutoscalePolicy(
            min_nodes=1, max_nodes=4, capacity_per_node=8, headroom=0.2
        )

        for running in (0, 1, 7, 8, 9, 20, 40):
            for queued in (0, 3, 12):
                result = simulate(
                    running=running,
                    queued=queued,
                    current_capacity=2,
                    policy=policy,
                )

                self.assertEqual(
                    result["desired_capacity"],
                    desired_capacity(running, queued, policy),
                    f"running={running} queued={queued}",
                )

    def test_actions_follow_capacity_direction(self):
        policy = AutoscalePolicy(
            min_nodes=1, max_nodes=8, capacity_per_node=4, headroom=0.0
        )

        up = simulate(running=12, queued=0, current_capacity=1, policy=policy)
        self.assertEqual(up["action"], "scale_up")
        self.assertEqual(up["label"], "scale_out")
        self.assertEqual(up["delta"], 2)

        down = simulate(running=0, queued=0, current_capacity=3, policy=policy)
        self.assertEqual(down["action"], "scale_down")
        self.assertEqual(down["label"], "scale_in")

        steady = simulate(running=4, queued=0, current_capacity=1, policy=policy)
        self.assertEqual(steady["action"], "none")
        self.assertEqual(steady["label"], "hold")


class PolicyOverrideTest(unittest.TestCase):
    def test_overrides_are_applied_and_clamped(self):
        result = simulate(
            running=100,
            queued=0,
            current_capacity=1,
            policy_overrides={
                "capacity_per_node": 4,
                "max_nodes": 3,
                "headroom": 0.0,
            },
        )

        self.assertEqual(result["policy"]["max_nodes"], 3)
        self.assertEqual(result["desired_capacity"], 3)

    def test_unknown_field_is_rejected(self):
        with self.assertRaises(ValueError):
            simulate(
                running=1,
                queued=0,
                current_capacity=1,
                policy_overrides={"max_node": 3},
            )

    def test_invalid_range_is_rejected(self):
        with self.assertRaises(ValueError):
            simulate(
                running=1,
                queued=0,
                current_capacity=1,
                policy_overrides={"min_nodes": 5, "max_nodes": 2},
            )


class SafetyTest(unittest.TestCase):
    def test_result_declares_itself_simulation(self):
        result = simulate(running=9, queued=1, current_capacity=1)

        self.assertEqual(result["data_source"], "simulation")
        self.assertFalse(result["applied"])
        self.assertTrue(result["simulation_id"].startswith("sim-"))
        self.assertIn("仅模拟", result["message"])

    def test_negative_inputs_are_rejected(self):
        for kwargs in (
            {"running": -1, "queued": 0, "current_capacity": 0},
            {"running": 0, "queued": -1, "current_capacity": 0},
            {"running": 0, "queued": 0, "current_capacity": -1},
        ):
            with self.assertRaises(ValueError):
                simulate(**kwargs)

    def test_stabilization_and_cooldown_are_reported_not_applied(self):
        result = simulate(running=12, queued=0, current_capacity=1)

        self.assertEqual(
            result["stabilization"]["required_samples"],
            result["policy"]["scale_up_after"],
        )
        self.assertIn("假设已满足", result["stabilization"]["note"])
        self.assertIn("冷却期", result["cooldown"]["note"])


if __name__ == "__main__":
    unittest.main()
