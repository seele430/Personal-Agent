"""Qwen Agent Provider 测试：全 mock 子进程，不真的启动 qwen。"""

import subprocess
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from src.collab.providers.openai_compat import ProviderError
from src.collab.providers.qwen_agent import QwenAgentProvider


class FakeProcess:
    def __init__(self, *, workspace=None, artifact="acceptance.md", code=0, output="DONE"):
        self.pid = 4242
        self.returncode = code
        self._workspace = workspace
        self._artifact = artifact
        self._output = output
        self.killed = False

    def communicate(self, timeout=None):
        if self._workspace is not None and self._artifact:
            target = Path(self._workspace) / self._artifact
            target.write_text("由 agent 写入的正文", encoding="utf-8")

        return self._output, None

    def kill(self):
        self.killed = True


class ProviderCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.out = Path(self._temporary.name)

    def provider(self, process, **kwargs):
        return QwenAgentProvider(qwen_path="C:/fake/qwen.cmd", popen=lambda *a, **k: process, **kwargs)


class TestSuccess(ProviderCase):
    def test_writes_artifact_and_reports_digest(self):
        holder = {}

        def factory(*args, **kwargs):
            holder["process"] = FakeProcess(workspace=kwargs["cwd"])
            return holder["process"]

        provider = QwenAgentProvider(qwen_path="C:/fake/qwen.cmd", popen=factory)
        result = provider.submit("写一段说明", artifact_name="acceptance.md", out_dir=self.out)

        self.assertEqual(result["provider"], "qwen-agent")
        self.assertTrue((self.out / "acceptance.md").is_file())
        self.assertEqual(len(result["sha256"]), 64)
        self.assertIn("DONE", holder["process"]._output)

    def test_prompt_not_echoed_in_result(self):
        def factory(*args, **kwargs):
            return FakeProcess(workspace=kwargs["cwd"], artifact="a.md")

        provider = QwenAgentProvider(qwen_path="C:/fake/qwen.cmd", popen=factory)
        result = provider.submit("敏感内容ABC", artifact_name="a.md", out_dir=self.out)

        self.assertNotIn("敏感内容ABC", str(result))
        self.assertEqual(result["prompt_chars"], len("敏感内容ABC"))

    def test_env_is_allowlisted(self):
        captured = {}

        def factory(args, **kwargs):
            captured["env"] = kwargs["env"]
            return FakeProcess(workspace=kwargs["cwd"], artifact="a.md")

        provider = QwenAgentProvider(qwen_path="C:/fake/qwen.cmd", popen=factory)

        with patch.dict("os.environ", {"SECRET_TOKEN": "should-not-leak"}, clear=False):
            provider.submit("x", artifact_name="a.md", out_dir=self.out)

        self.assertNotIn("SECRET_TOKEN", captured["env"])
        self.assertIn("PATH", captured["env"])


class TestFailures(ProviderCase):
    def test_missing_qwen_binary(self):
        with patch("src.collab.providers.qwen_agent.shutil.which", return_value=None), \
             patch(
                 "src.collab.providers.qwen_agent.QWEN_DEFAULT_PATHS",
                 ("Z:/nope/qwen.cmd",),
             ):
            provider = QwenAgentProvider()

            with self.assertRaises(ProviderError) as caught:
                provider.submit("x", artifact_name="a.md", out_dir=self.out)

        self.assertIn("qwen", str(caught.exception))

    def test_agent_produced_nothing(self):
        def factory(*args, **kwargs):
            return FakeProcess(workspace=kwargs["cwd"], artifact=None)

        provider = QwenAgentProvider(qwen_path="C:/fake/qwen.cmd", popen=factory)

        with self.assertRaises(ProviderError) as caught:
            provider.submit("x", artifact_name="a.md", out_dir=self.out)

        self.assertIn("没有产出", str(caught.exception))

    def test_timeout_kills_tree(self):
        class Slow(FakeProcess):
            def communicate(self, timeout=None):
                if timeout:
                    raise subprocess.TimeoutExpired("qwen", timeout)
                return "", None

        with patch.object(QwenAgentProvider, "_kill_tree") as killer:
            provider = QwenAgentProvider(
                qwen_path="C:/fake/qwen.cmd", popen=lambda *a, **k: Slow()
            )

            with self.assertRaises(ProviderError) as caught:
                provider.submit("x", artifact_name="a.md", out_dir=self.out)

        self.assertIn("超时", str(caught.exception))
        killer.assert_called_once()

    def test_file_too_large_rejected(self):
        def factory(*args, **kwargs):
            workspace = Path(kwargs["cwd"])
            (workspace / "big.bin").write_bytes(b"x" * 2000)
            return FakeProcess(workspace=str(workspace))

        provider = QwenAgentProvider(
            qwen_path="C:/fake/qwen.cmd", popen=factory, max_file_bytes=1000
        )

        with self.assertRaises(ProviderError) as caught:
            provider.submit("x", artifact_name="big.bin", out_dir=self.out)

        self.assertIn("产物过大", str(caught.exception))

    def test_too_many_files_rejected(self):
        def factory(*args, **kwargs):
            workspace = Path(kwargs["cwd"])

            for index in range(5):
                (workspace / f"f{index}.txt").write_text("x", encoding="utf-8")

            return FakeProcess(workspace=str(workspace), artifact="f0.txt")

        provider = QwenAgentProvider(
            qwen_path="C:/fake/qwen.cmd", popen=factory, max_files=3
        )

        with self.assertRaises(ProviderError) as caught:
            provider.submit("x", artifact_name="f0.txt", out_dir=self.out)

        self.assertIn("产物过多", str(caught.exception))

    def test_empty_prompt_rejected(self):
        provider = QwenAgentProvider(qwen_path="C:/fake/qwen.cmd", popen=lambda *a, **k: None)

        with self.assertRaises(ProviderError):
            provider.submit("   ", artifact_name="a.md", out_dir=self.out)



class TestCleanup(ProviderCase):
    def test_success_leaves_no_workspace(self):
        def factory(*args, **kwargs):
            return FakeProcess(workspace=kwargs["cwd"], artifact="a.md")

        provider = QwenAgentProvider(qwen_path="C:/fake/qwen.cmd", popen=factory)
        provider.submit("x", artifact_name="a.md", out_dir=self.out)

        self.assertEqual(list(self.out.glob("qwen-workspace-*")), [])
        self.assertTrue((self.out / "a.md").is_file())

    def test_failure_leaves_no_workspace(self):
        def factory(*args, **kwargs):
            return FakeProcess(workspace=kwargs["cwd"], artifact=None)

        provider = QwenAgentProvider(qwen_path="C:/fake/qwen.cmd", popen=factory)

        with self.assertRaises(ProviderError):
            provider.submit("x", artifact_name="a.md", out_dir=self.out)

        self.assertEqual(list(self.out.glob("qwen-workspace-*")), [])


if __name__ == "__main__":
    unittest.main()
