"""记忆工具：身份绑定、写入、召回、无身份拒绝。"""

import json
import tempfile
import unittest

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.core.reminder_identity import bind_authenticated_reminder
from src.memory.store import MemoryStore
from src.tools import memory as tools


def call(name: str, arguments: dict):
    return SimpleNamespace(
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments))
    )


class MemoryToolCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.store = MemoryStore(Path(self._temporary.name) / "memory.db")
        self.patch = patch("src.memory.service.get_memory_store", return_value=self.store)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_remember_without_identity_denied(self):
        with self.assertRaises(PermissionError):
            tools.remember_tool(call("memory_remember", {"text": "x"}))

    def test_remember_and_recall_with_identity(self):
        with bind_authenticated_reminder(user_id="u1", request_id="req_test_123", role="member"):
            result = tools.remember_tool(call("memory_remember", {"text": "喜欢绿茶", "key": "drink"}))
            self.assertEqual(result["status"], "remembered")
            recalled = tools.recall_tool(call("memory_recall", {"query": "绿茶"}))
            self.assertEqual(recalled["items"][0]["text"], "喜欢绿茶")

    def test_request_owner_cannot_be_spoofed(self):
        with bind_authenticated_reminder(user_id="u1", request_id="req_test_123", role="member"):
            with self.assertRaises(ValueError):
                tools.remember_tool(call("memory_remember", {"text": "x", "owner": "u2"}))

    def test_old_json_path_cannot_write_memory(self):
        from src.agent import execute_tool_on_platform
        from src.core.tool_registry import ToolDenied

        with self.assertRaises(ToolDenied):
            execute_tool_on_platform(
                call("memory_remember", {"text": "x"}), require_allow=False
            )

    def test_recall_k_bounds(self):
        with bind_authenticated_reminder(user_id="u1", request_id="req_test_123", role="member"):
            with self.assertRaises(ValueError):
                tools.recall_tool(call("memory_recall", {"query": "x", "k": 99}))


if __name__ == "__main__":
    unittest.main()
