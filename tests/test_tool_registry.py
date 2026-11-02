"""D8 工具注册表与 allow / ask / deny 策略矩阵。"""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from src.core.run_control import CancellationToken, ChatCancelled
from src.core.tool_registry import (
    ToolApprovalRequired,
    ToolDenied,
    ToolRegistry,
)


def schema(name):
    return {"type": "function", "function": {"name": name, "parameters": {}}}


def call(name):
    return SimpleNamespace(function=SimpleNamespace(name=name))


class ToolRegistryTest(unittest.TestCase):
    def setUp(self):
        self.registry = ToolRegistry()
        self.handler = Mock(return_value={"success": True})
        for name, policy in (("read", "allow"), ("change", "ask"), ("danger", "deny")):
            self.registry.register(schema(name), policy=policy, handler=self.handler)

    def test_allow_executes_with_cancellation_context(self):
        token = CancellationToken()
        result = self.registry.execute(call("read"), cancel_token=token)
        self.assertTrue(result["success"])
        self.handler.assert_called_once_with(unittest.mock.ANY, cancel_token=token)

    def test_ask_never_executes_handler(self):
        with self.assertRaises(ToolApprovalRequired):
            self.registry.execute(call("change"))
        self.handler.assert_not_called()

    def test_deny_and_unknown_never_execute_handler(self):
        for name in ("danger", "unknown"):
            with self.assertRaises(ToolDenied):
                self.registry.execute(call(name))
        self.handler.assert_not_called()
        self.assertEqual(self.registry.policy("unknown"), "deny")

    def test_cancel_is_visible_inside_active_tool(self):
        token = CancellationToken()
        token.cancel()
        with self.assertRaises(ChatCancelled):
            self.registry.execute(call("read"), cancel_token=token)
        self.handler.assert_not_called()

    def test_duplicate_tool_and_invalid_policy_are_rejected(self):
        with self.assertRaises(ValueError):
            self.registry.register(schema("read"), policy="allow", handler=self.handler)
        with self.assertRaises(ValueError):
            self.registry.register(schema("other"), policy="perhaps", handler=self.handler)


if __name__ == "__main__":
    unittest.main()
