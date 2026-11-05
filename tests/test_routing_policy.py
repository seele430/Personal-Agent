"""确定性审批闸门：高危不看置信度，clarify 不放行。"""

from __future__ import annotations

import unittest

from dataclasses import replace

from src.routing import Router
from src.routing.policy import approval_gate


class ApprovalGateTest(unittest.TestCase):
    def setUp(self):
        self.router = Router()

    def test_high_risk_requires_approval(self):
        decision = self.router.decide("删掉 data 目录")
        gate = approval_gate(decision)

        self.assertEqual(decision.risk, "high")
        self.assertFalse(gate.allowed)
        self.assertTrue(gate.requires_approval)
        self.assertEqual(gate.reason, "high_risk_requires_approval")

    def test_high_risk_ignores_confidence(self):
        decision = self.router.decide("删掉 data 目录")
        confident = replace(decision, confidence=0.99)

        gate = approval_gate(confident)

        self.assertFalse(gate.allowed)
        self.assertTrue(gate.requires_approval)

    def test_high_risk_allowed_after_approval(self):
        decision = self.router.decide("删掉 data 目录")
        gate = approval_gate(decision, approved=True)

        self.assertTrue(gate.allowed)
        self.assertFalse(gate.requires_approval)
        self.assertEqual(gate.reason, "high_risk_approved")

    def test_low_risk_allowed(self):
        decision = self.router.decide("分析这个 CSV 的缺失值")
        gate = approval_gate(decision)

        self.assertTrue(gate.allowed)
        self.assertFalse(gate.requires_approval)

    def test_medium_risk_allowed_by_default(self):
        decision = self.router.decide("把结果写入 report.md")
        gate = approval_gate(decision)

        self.assertEqual(decision.risk, "medium")
        self.assertTrue(gate.allowed)

    def test_medium_risk_rejected_when_explicitly_denied(self):
        decision = self.router.decide("把结果写入 report.md")
        gate = approval_gate(decision, approved=False)

        self.assertFalse(gate.allowed)
        self.assertTrue(gate.requires_approval)

    def test_clarify_blocks_execution(self):
        decision = self.router.decide("分析并运行这个脚本")
        gate = approval_gate(decision)

        self.assertFalse(gate.allowed)
        self.assertFalse(gate.requires_approval)
        self.assertEqual(gate.reason, "clarify_first")


if __name__ == "__main__":
    unittest.main()
