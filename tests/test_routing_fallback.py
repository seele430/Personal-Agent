"""降级不降风险：provider 切换与决策保留。"""

from __future__ import annotations

import unittest

from src.routing import Router
from src.routing.fallback import apply_fallback, plan_fallback


class FallbackPlanTest(unittest.TestCase):
    def setUp(self):
        self.router = Router()

    def test_falls_back_to_local_when_primary_down(self):
        decision = self.router.decide("分析这个 CSV 的缺失值")

        plan = plan_fallback(
            decision,
            failed_provider="deepseek",
            available_providers=("deepseek", "local"),
        )

        self.assertEqual(plan.provider, "local")
        self.assertEqual(plan.model_policy, "local")
        self.assertEqual(plan.risk, decision.risk)
        self.assertEqual(plan.requires_approval, decision.requires_approval)
        self.assertEqual(plan.constraints, decision.constraints)
        self.assertEqual(plan.reason, "fallback_after:deepseek")

    def test_high_risk_stays_high_risk(self):
        decision = self.router.decide("删掉 data 目录")

        plan = plan_fallback(
            decision,
            failed_provider="deepseek",
            available_providers=("deepseek", "local"),
        )

        self.assertEqual(plan.risk, "high")
        self.assertTrue(plan.requires_approval)

    def test_no_provider_left(self):
        decision = self.router.decide("分析这个 CSV")

        plan = plan_fallback(
            decision,
            failed_provider="deepseek",
            available_providers=("deepseek",),
        )

        self.assertEqual(plan.provider, "")
        self.assertEqual(plan.reason, "no_available_provider")

    def test_apply_fallback_keeps_risk_and_approval(self):
        decision = self.router.decide("删掉 data 目录")

        plan = plan_fallback(
            decision,
            failed_provider="deepseek",
            available_providers=("deepseek", "local"),
        )
        applied = apply_fallback(decision, plan)

        self.assertEqual(applied.risk, decision.risk)
        self.assertEqual(applied.requires_approval, decision.requires_approval)
        self.assertEqual(applied.model_policy, "local")
        self.assertIn("fallback_after:deepseek", applied.reason)

    def test_apply_without_provider_raises(self):
        decision = self.router.decide("分析这个 CSV")
        plan = plan_fallback(
            decision,
            failed_provider="deepseek",
            available_providers=("deepseek",),
        )

        with self.assertRaises(RuntimeError):
            apply_fallback(decision, plan)


if __name__ == "__main__":
    unittest.main()
