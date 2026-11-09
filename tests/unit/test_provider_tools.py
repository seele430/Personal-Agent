"""`external_task` 工具测试：注册、参数与路径安全、成功路径、错误（全 mock）。"""

import json
import tempfile
import unittest
import unittest.mock

from pathlib import Path
from types import SimpleNamespace


def provider(*, configured=True, submit=None):
    return SimpleNamespace(configured=configured, submit=submit)
from unittest.mock import patch

import src.tools.provider_tools as tools


def call(**arguments):
    return SimpleNamespace(
        function=SimpleNamespace(
            name="external_task", arguments=json.dumps(arguments)
        )
    )


class RegistryCase(unittest.TestCase):
    def test_definition_is_ask(self):
        schema, policy, handler = tools.TOOL_DEFINITIONS[0]

        self.assertEqual(schema["function"]["name"], "external_task")
        self.assertEqual(policy, "ask")
        self.assertTrue(callable(handler))

    def test_registered_in_agent(self):
        source = Path("src/agent.py").read_text(encoding="utf-8")

        self.assertIn("provider_tools", source)
        self.assertIn("provider_tools.TOOL_DEFINITIONS", source)


class NameCase(unittest.TestCase):
    def test_default(self):
        self.assertEqual(tools.safe_artifact_name(""), "external-report.md")

    def test_allows_simple_names(self):
        self.assertEqual(tools.safe_artifact_name("report-2.md"), "report-2.md")

    def test_rejects_separators(self):
        for bad in ("../x.md", "a/b.md", "C:\\x.md", "x\\y.md", ".hidden"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    tools.safe_artifact_name(bad)


class HandlerCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.workspace = Path(self._temporary.name)

    def run_handler(self, **arguments):
        with patch("src.tools.provider_tools.Path.cwd", return_value=self.workspace):
            return tools.external_task(call(**arguments))

    def test_success_writes_artifact(self):
        def fake_submit(prompt, *, artifact_name, out_dir):
            out_dir.mkdir(parents=True, exist_ok=True)
            (Path(out_dir) / artifact_name).write_text("正文", encoding="utf-8")

            return {
                "task_id": "t1",
                "model": "deepseek-flash",
                "duration_s": 1.2,
                "usage": {"prompt": 10, "completion": 20, "total": 30},
            }

        with patch(
            "src.tools.provider_tools.build_provider",
            return_value=provider(submit=fake_submit),
        ):
            result = self.run_handler(prompt="写点什么")

        self.assertTrue(result["success"])
        self.assertEqual(result["path"], "external-artifacts/external-report.md")
        self.assertEqual(result["usage"]["total"], 30)
        self.assertTrue(
            (self.workspace / "external-artifacts" / "external-report.md").is_file()
        )

    def test_missing_key_refuses_without_request(self):
        submit = unittest.mock.Mock()

        with patch(
            "src.tools.provider_tools.build_provider",
            return_value=provider(configured=False, submit=submit),
        ):
            with self.assertRaises(RuntimeError) as caught:
                self.run_handler(prompt="x")

        self.assertIn("OPENAI_API_KEY", str(caught.exception))
        submit.assert_not_called()

    def test_empty_prompt_rejected(self):
        submit = unittest.mock.Mock()

        with patch(
            "src.tools.provider_tools.build_provider",
            return_value=provider(submit=submit),
        ):
            with self.assertRaises(ValueError):
                self.run_handler(prompt="   ")

        submit.assert_not_called()

    def test_provider_error_becomes_runtime_error(self):
        from src.collab.providers import ProviderError

        def boom(*args, **kwargs):
            raise ProviderError("Provider 返回 HTTP 401")

        with patch(
            "src.tools.provider_tools.build_provider",
            return_value=provider(submit=boom),
        ):
            with self.assertRaises(RuntimeError) as caught:
                self.run_handler(prompt="x")

        self.assertIn("HTTP 401", str(caught.exception))

    def test_cancel_token_checked(self):
        class Token:
            def check(self):
                raise RuntimeError("已取消")

        with patch(
            "src.tools.provider_tools.build_provider",
            return_value=provider(),
        ):
            with self.assertRaises(RuntimeError):
                tools.external_task(call(prompt="x"), cancel_token=Token())

    def test_bad_json_arguments(self):
        bad = SimpleNamespace(
            function=SimpleNamespace(name="external_task", arguments="{not json")
        )

        with self.assertRaises(ValueError):
            tools.external_task(bad)


if __name__ == "__main__":
    unittest.main()
