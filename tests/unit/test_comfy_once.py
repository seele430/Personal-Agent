"""单次出图 CLI 测试：steps 护栏、参数透传、错误退出码（全打桩）。"""

import io
import json
import unittest

from contextlib import redirect_stdout
from unittest.mock import patch

import scripts.comfy_once as once


def run(argv):
    buffer = io.StringIO()

    with redirect_stdout(buffer):
        code = once.main(argv)

    return code, buffer.getvalue()


class StepsGuardCase(unittest.TestCase):
    def test_steps_too_large_exits_2_without_calling_tool(self):
        with patch.object(once.comfy_tools, "comfy_img2img") as handler:
            code, output = run(["--prompt", "x", "--steps", "31"])

        self.assertEqual(code, 2)
        self.assertIn("1..30", output)
        handler.assert_not_called()

    def test_steps_zero_exits_2(self):
        with patch.object(once.comfy_tools, "comfy_img2img") as handler:
            code, _ = run(["--prompt", "x", "--steps", "0"])

        self.assertEqual(code, 2)
        handler.assert_not_called()

    def test_empty_prompt_exits_2(self):
        with patch.object(once.comfy_tools, "comfy_img2img") as handler:
            code, output = run(["--prompt", "   "])

        self.assertEqual(code, 2)
        self.assertIn("prompt", output)
        handler.assert_not_called()


class SuccessCase(unittest.TestCase):
    def test_passes_arguments(self):
        captured = {}

        def fake(tool_call, cancel_token=None):
            captured.update(json.loads(tool_call.function.arguments))

            return {"success": True, "images": ["out.png"], "path": "out.png"}

        with patch.object(once.comfy_tools, "comfy_img2img", side_effect=fake):
            code, output = run(
                ["--prompt", "a cat", "--image", "big.png", "--steps", "8", "--seed", "7"]
            )

        self.assertEqual(code, 0)
        self.assertEqual(captured["image_path"], "big.png")
        self.assertEqual(captured["prompt"], "a cat")
        self.assertEqual(captured["steps"], 8)
        self.assertEqual(captured["seed"], 7)
        self.assertIn("out.png", output)


class FailureCase(unittest.TestCase):
    def test_runtime_error_exit_1(self):
        with patch.object(
            once.comfy_tools,
            "comfy_img2img",
            side_effect=RuntimeError("显存不足：空闲 1200 MB < 2500 MB"),
        ):
            code, output = run(["--prompt", "x"])

        self.assertEqual(code, 1)
        self.assertIn("显存不足", output)

    def test_value_error_exit_2(self):
        with patch.object(
            once.comfy_tools, "comfy_img2img", side_effect=ValueError("denoise 越界")
        ):
            code, output = run(["--prompt", "x"])

        self.assertEqual(code, 2)
        self.assertIn("参数错误", output)


class ScriptWiringCase(unittest.TestCase):
    def test_wrapper_stops_in_finally(self):
        from pathlib import Path

        source = Path("scripts/comfy_once.ps1").read_text(encoding="utf-8")

        self.assertIn("finally", source)
        self.assertIn("-Stop", source)
        self.assertIn("start-comfyui-safe.ps1", source)
        self.assertLess(source.index("steps must be 1..30"), source.index("safe launcher"))


if __name__ == "__main__":
    unittest.main()
