"""规则路由：intent、风险、clarify、事件结构。"""

from __future__ import annotations

import unittest

from src.routing import Router, normalize


class IntentRouterTest(unittest.TestCase):
    def setUp(self):
        self.router = Router()

    def decide(self, message: str):
        return self.router.decide(message)

    def test_five_intents(self):
        cases = {
            "分析这个 CSV 的缺失值，并生成一张图": "data.analysis",
            "帮我跑一下这个 python 脚本": "code.execute",
            "把这些结果写入 report.md": "file.operate",
            "看一下队列现在有多长": "platform.inspect",
            "明天早上八点提醒我开会": "reminder.manage",
        }

        for message, expected in cases.items():
            with self.subTest(message=message):
                decision = self.decide(message)
                self.assertEqual(decision.decision, "route")
                self.assertEqual(decision.intent, expected)
                self.assertGreaterEqual(decision.confidence, 0.55)

    def test_general_chat_fallback(self):
        decision = self.decide("你好呀，今天怎么样？")

        self.assertEqual(decision.decision, "route")
        self.assertEqual(decision.intent, "general.chat")
        self.assertEqual(decision.risk, "low")

    def test_ambiguous_requests_clarify(self):
        decision = self.decide("分析并运行这个脚本")

        self.assertEqual(decision.decision, "clarify")
        self.assertIsNone(decision.intent)
        self.assertEqual(decision.source, "clarify")
        self.assertGreaterEqual(len(decision.candidates), 2)

    def test_high_risk_requires_approval(self):
        decision = self.decide("删掉 data 目录")

        self.assertEqual(decision.decision, "route")
        self.assertEqual(decision.risk, "high")
        self.assertTrue(decision.requires_approval)

    def test_medium_risk_write(self):
        decision = self.decide("把结果写入 report.md")

        self.assertEqual(decision.intent, "file.operate")
        self.assertEqual(decision.risk, "medium")
        self.assertFalse(decision.requires_approval)

    def test_data_analysis_constraints(self):
        decision = self.decide("分析这个 CSV 的缺失值，并生成一张图")

        self.assertEqual(decision.capability, "sandbox_exec")
        self.assertEqual(decision.execution_type, "local_sandbox")
        self.assertTrue(decision.constraints["artifact_required"])
        self.assertIn("python", decision.constraints["allowed_tools"])

    def test_router_never_selects_nodes(self):
        event = self.decide("分析这个 CSV").to_event()

        for forbidden in (
            "node_id",
            "sandbox_id",
            "queue_position",
            # 成本/模型档位未实现，字段已移除；避免看起来像生效的开关。
            "model_policy",
        ):
            self.assertNotIn(forbidden, event)

        self.assertEqual(event["event"], "route_decided")
        self.assertEqual(event["intent"], "data.analysis")

    def test_deterministic(self):
        first = self.decide("分析这个 CSV 的缺失值")
        second = self.decide("分析这个 CSV 的缺失值")

        self.assertEqual(first, second)

    def test_normalize(self):
        self.assertEqual(normalize("  你好   世界 "), "你好 世界")


if __name__ == "__main__":
    unittest.main()
