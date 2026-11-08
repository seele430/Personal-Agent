"""ComfyUI 工具测试：注册、参数校验、成功路径、错误与超时（全打桩）。"""

import json
import subprocess
import unittest

from types import SimpleNamespace
from unittest.mock import patch

import src.tools.comfy_tools as comfy


def call(arguments):
    return SimpleNamespace(
        function=SimpleNamespace(name="comfy_img2img", arguments=json.dumps(arguments))
    )


class RegistryCase(unittest.TestCase):
    def test_definition_shape(self):
        schema, policy, handler = comfy.TOOL_DEFINITIONS[0]

        self.assertEqual(schema["function"]["name"], "comfy_img2img")
        self.assertEqual(policy, "ask")
        self.assertTrue(callable(handler))
        self.assertEqual(
            sorted(schema["function"]["parameters"]["required"]),
            ["image_path", "prompt"],
        )

    def test_registered_in_agent(self):
        # 只做静态检查：导入 agent 会拉起模型依赖，太重
        source = (
            __import__("pathlib").Path("src/agent.py").read_text(encoding="utf-8")
        )

        self.assertIn("comfy_tools", source)
        self.assertIn("comfy_tools.TOOL_DEFINITIONS", source)


class BuildCase(unittest.TestCase):
    def test_injects_parameters(self):
        workflow = comfy.build_workflow(
            image_name="input.png", prompt="a cat", steps=8, denoise=0.4, seed=7
        )

        self.assertEqual(workflow["6"]["inputs"]["text"], "a cat")
        self.assertEqual(workflow["8"]["inputs"]["image"], "input.png")
        self.assertEqual(workflow["3"]["inputs"]["steps"], 8)
        self.assertEqual(workflow["3"]["inputs"]["denoise"], 0.4)
        self.assertEqual(workflow["3"]["inputs"]["seed"], 7)

    def test_empty_image(self):
        with self.assertRaises(ValueError):
            comfy.build_workflow(image_name="", prompt="x")

    def test_empty_prompt(self):
        with self.assertRaises(ValueError):
            comfy.build_workflow(image_name="a.png", prompt="")

    def test_steps_bounds(self):
        for steps in (0, 151):
            with self.subTest(steps=steps):
                with self.assertRaises(ValueError):
                    comfy.build_workflow(image_name="a.png", prompt="x", steps=steps)

    def test_denoise_bounds(self):
        with self.assertRaises(ValueError):
            comfy.build_workflow(image_name="a.png", prompt="x", denoise=0.0)


class HandlerCase(unittest.TestCase):
    def test_success(self):
        history = {"p1": {"outputs": {"10": {"images": [{"filename": "out.png"}]}}}}

        with patch.object(comfy, "_request", side_effect=[{"prompt_id": "p1"}, history]):
            result = comfy.comfy_img2img(call({"image_path": "a.png", "prompt": "x"}))

        self.assertTrue(result["success"])
        self.assertEqual(result["path"], "out.png")
        self.assertEqual(result["prompt_id"], "p1")

    def test_missing_prompt_id(self):
        with patch.object(comfy, "_request", return_value={}):
            with self.assertRaises(RuntimeError):
                comfy.comfy_img2img(call({"image_path": "a.png", "prompt": "x"}))

    def test_execution_error(self):
        history = {"p1": {"outputs": {}, "status": {"status_str": "error"}}}

        with patch.object(comfy, "_request", side_effect=[{"prompt_id": "p1"}, history]):
            with self.assertRaises(RuntimeError):
                comfy.comfy_img2img(call({"image_path": "a.png", "prompt": "x"}))

    def test_timeout_message_is_chinese(self):
        with patch.object(comfy, "_request", side_effect=[{"prompt_id": "p1"}, {"p1": {}}]), patch.object(
            comfy, "timeout_seconds", return_value=0.0
        ):
            with self.assertRaises(RuntimeError) as caught:
                comfy.comfy_img2img(call({"image_path": "a.png", "prompt": "x"}))

        self.assertIn("超时", str(caught.exception))

    def test_cancel_token_checked(self):
        class Token:
            def check(self):
                raise RuntimeError("已取消")

        with self.assertRaises(RuntimeError):
            comfy.comfy_img2img(
                call({"image_path": "a.png", "prompt": "x"}), cancel_token=Token()
            )

    def test_bad_arguments_json(self):
        bad = SimpleNamespace(
            function=SimpleNamespace(name="comfy_img2img", arguments="{not json")
        )

        with self.assertRaises(ValueError):
            comfy.comfy_img2img(bad)


if __name__ == "__main__":
    unittest.main()
