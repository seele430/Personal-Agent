"""external_task 模型 API 故障转移测试。"""

import os
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from src.collab.providers.openai_compat import ProviderError
from src.tools import provider_tools


class FakeProvider:
    def __init__(self, name="fake", error=None):
        self.name = name
        self.error = error
        self.timeout = 30.0
        self.configured = True
        self.calls = 0

    def submit(self, prompt, *, artifact_name, out_dir):
        self.calls += 1

        if self.error is not None:
            raise self.error

        path = Path(out_dir) / artifact_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("result", encoding="utf-8")

        return {
            "task_id": "t1",
            "model": self.name,
            "artifact": str(path),
            "duration_s": 0.1,
            "usage": {"total_tokens": 1},
        }


class ToolCall:
    class Function:
        arguments = '{"prompt": "写一句话", "artifact_name": "out.md"}'

    function = Function()


class FailoverCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.cwd = Path(self._temporary.name)
        self.env = patch.dict(os.environ, {"PA_RELAY_CHAIN": ""}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_tool(self, providers, chain="ark,deepseek"):
        os.environ["PA_RELAY_CHAIN"] = chain

        with patch.object(provider_tools, "build_providers", return_value=providers), patch(
            "os.getcwd", return_value=str(self.cwd)
        ):
            return provider_tools.external_task(ToolCall())

    def test_empty_chain_uses_single_provider(self):
        provider = FakeProvider("single")

        with patch.object(provider_tools, "build_provider", return_value=provider), patch(
            "os.getcwd", return_value=str(self.cwd)
        ):
            result = provider_tools.external_task(ToolCall())

        self.assertEqual(result["provider"], "single")
        self.assertEqual(result["attempts"], 1)

    def test_retryable_failover(self):
        first = FakeProvider("ark", error=ProviderError("timeout"))
        second = FakeProvider("deepseek")

        result = self.run_tool({"ark": first, "deepseek": second})

        self.assertEqual(result["provider"], "deepseek")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(first.calls, 1)
        self.assertEqual(second.calls, 1)

    def test_fatal_does_not_failover(self):
        error = ProviderError("401 unauthorized")
        first = FakeProvider("ark", error=error)
        second = FakeProvider("deepseek")

        with self.assertRaises(RuntimeError):
            self.run_tool({"ark": first, "deepseek": second})

        self.assertEqual(second.calls, 0)

    def test_invalid_chain_rejected_before_providers(self):
        for chain in ("qwen", "ark,deepseek,qwen", "unknown", "ark,,deepseek"):
            with self.subTest(chain=chain):
                with self.assertRaises(RuntimeError):
                    self.run_tool({"ark": FakeProvider(), "deepseek": FakeProvider()}, chain=chain)

    def test_chain_exhausted(self):
        first = FakeProvider("ark", error=ProviderError("timeout"))
        second = FakeProvider("deepseek", error=ProviderError("429"))

        with self.assertRaises(RuntimeError):
            self.run_tool({"ark": first, "deepseek": second})



    def test_duration_is_total_across_attempts(self):
        first = FakeProvider("ark", error=ProviderError("timeout"))
        second = FakeProvider("deepseek")
        clock = iter([0.0, 0.0, 10.0, 10.0, 16.0, 16.0, 16.0])

        with patch.object(provider_tools.time, "monotonic", side_effect=lambda: next(clock, 16.0)):
            result = self.run_tool({"ark": first, "deepseek": second})

        self.assertGreaterEqual(result["duration_s"], 10.0)

    def test_cancel_checked_after_last_retryable(self):
        class Token:
            def __init__(self):
                self.checks = 0

            def check(self):
                self.checks += 1

        token = Token()
        provider = FakeProvider("ark", error=ProviderError("timeout"))
        os.environ["PA_RELAY_CHAIN"] = "ark"

        with patch.object(provider_tools, "build_providers", return_value={"ark": provider}), patch(
            "os.getcwd", return_value=str(self.cwd)
        ):
            with self.assertRaises(RuntimeError):
                provider_tools.external_task(ToolCall(), cancel_token=token)

        self.assertGreaterEqual(token.checks, 2)

    def test_first_success_stops_chain(self):
        first = FakeProvider("ark")
        second = FakeProvider("deepseek")

        result = self.run_tool({"ark": first, "deepseek": second})

        self.assertEqual(result["provider"], "ark")
        self.assertEqual(second.calls, 0)

        artifact = self.cwd / result["path"]
        self.assertTrue(artifact.exists())
        import hashlib

        self.assertEqual(
            hashlib.sha256(artifact.read_bytes()).hexdigest(),
            hashlib.sha256(b"result").hexdigest(),
        )

    def test_duplicate_chain_deduped(self):
        provider = FakeProvider("ark")
        os.environ["PA_RELAY_CHAIN"] = "ark,ark"

        with patch.object(provider_tools, "build_providers", return_value={"ark": provider}), patch(
            "os.getcwd", return_value=str(self.cwd)
        ):
            result = provider_tools.external_task(ToolCall())

        self.assertEqual(provider.calls, 1)
        self.assertEqual(result["attempts"], 1)


if __name__ == "__main__":
    unittest.main()
