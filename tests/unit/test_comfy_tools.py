"""ComfyUI 工具测试：注册、参数校验、GPU 保护、成功/错误/超时（全打桩）。"""

import json
import struct
import tempfile
import threading
import unittest

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import src.tools.comfy_tools as comfy


def call(arguments):
    return SimpleNamespace(
        function=SimpleNamespace(name="comfy_img2img", arguments=json.dumps(arguments))
    )


def png_bytes(width, height):
    header = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"

    return header + struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"


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
        source = Path("src/agent.py").read_text(encoding="utf-8")

        self.assertIn("comfy_tools", source)
        self.assertIn("comfy_tools.TOOL_DEFINITIONS", source)


class BuildCase(unittest.TestCase):
    def test_injects_parameters(self):
        workflow = comfy.build_workflow(
            image_name="input.png",
            prompt="a cat",
            steps=8,
            denoise=0.4,
            seed=7,
            downscale=False,
        )

        self.assertEqual(workflow["6"]["inputs"]["text"], "a cat")
        self.assertEqual(workflow["8"]["inputs"]["image"], "input.png")
        self.assertEqual(workflow["3"]["inputs"]["steps"], 8)
        self.assertEqual(workflow["3"]["inputs"]["denoise"], 0.4)
        self.assertEqual(workflow["3"]["inputs"]["seed"], 7)
        self.assertNotIn("11", workflow)

    def test_downscale_inserts_scale_node(self):
        workflow = comfy.build_workflow(
            image_name="big.png", prompt="x", downscale=True
        )

        self.assertEqual(workflow["11"]["class_type"], "ImageScale")
        self.assertEqual(workflow["11"]["inputs"]["width"], 512)
        self.assertEqual(workflow["5"]["inputs"]["pixels"], ["11", 0])

    def test_empty_image(self):
        with self.assertRaises(ValueError):
            comfy.build_workflow(image_name="", prompt="x")

    def test_empty_prompt(self):
        with self.assertRaises(ValueError):
            comfy.build_workflow(image_name="a.png", prompt="")

    def test_steps_hard_cap(self):
        for steps in (0, 31, 80):
            with self.subTest(steps=steps):
                with self.assertRaises(ValueError) as caught:
                    comfy.build_workflow(
                        image_name="a.png", prompt="x", steps=steps
                    )

                self.assertIn("1..30", str(caught.exception))

    def test_denoise_bounds(self):
        with self.assertRaises(ValueError):
            comfy.build_workflow(image_name="a.png", prompt="x", denoise=0.0)

    def test_batch_is_single(self):
        workflow = comfy.build_workflow(image_name="a.png", prompt="x")

        for node in workflow.values():
            self.assertNotIn("batch_size", node.get("inputs", {}))


class ImageSizeCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def test_png_size(self):
        path = self.root / "a.png"
        path.write_bytes(png_bytes(1024, 768))

        self.assertEqual(comfy.image_size(path), (1024, 768))

    def test_unknown_returns_none(self):
        path = self.root / "a.bin"
        path.write_bytes(b"nothing")

        self.assertIsNone(comfy.image_size(path))

    def test_needs_downscale_unknown_dir(self):
        with patch.object(comfy, "input_dir", return_value=None):
            self.assertTrue(comfy.needs_downscale("a.png"))

    def test_needs_downscale_by_side(self):
        (self.root / "big.png").write_bytes(png_bytes(1200, 800))
        (self.root / "ok.png").write_bytes(png_bytes(640, 480))

        with patch.object(comfy, "input_dir", return_value=self.root):
            self.assertTrue(comfy.needs_downscale("big.png"))
            self.assertFalse(comfy.needs_downscale("ok.png"))


class HandlerCase(unittest.TestCase):
    def test_success(self):
        history = {"p1": {"outputs": {"10": {"images": [{"filename": "out.png"}]}}}}

        with patch.object(comfy, "free_vram_mb", return_value=5000), patch.object(
            comfy, "_request", side_effect=[{"prompt_id": "p1"}, history]
        ):
            result = comfy.comfy_img2img(
                call({"image_path": "a.png", "prompt": "x", "steps": 8})
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["path"], "out.png")

    def test_low_vram_refuses_before_prompt(self):
        with patch.object(comfy, "free_vram_mb", return_value=1200), patch.object(
            comfy, "_request"
        ) as request:
            with self.assertRaises(RuntimeError) as caught:
                comfy.comfy_img2img(call({"image_path": "a.png", "prompt": "x"}))

        self.assertIn("显存不足", str(caught.exception))
        request.assert_not_called()

    def test_missing_prompt_id(self):
        with patch.object(comfy, "free_vram_mb", return_value=5000), patch.object(
            comfy, "_request", return_value={}
        ):
            with self.assertRaises(RuntimeError):
                comfy.comfy_img2img(call({"image_path": "a.png", "prompt": "x"}))

    def test_execution_error(self):
        history = {"p1": {"outputs": {}, "status": {"status_str": "error"}}}

        with patch.object(comfy, "free_vram_mb", return_value=5000), patch.object(
            comfy, "_request", side_effect=[{"prompt_id": "p1"}, history]
        ):
            with self.assertRaises(RuntimeError):
                comfy.comfy_img2img(call({"image_path": "a.png", "prompt": "x"}))

    def test_timeout_message_is_chinese(self):
        with patch.object(comfy, "free_vram_mb", return_value=5000), patch.object(
            comfy, "_request", side_effect=[{"prompt_id": "p1"}, {"p1": {}}]
        ), patch.object(comfy, "timeout_seconds", return_value=0.0):
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

    def test_concurrent_call_rejected(self):
        self.assertTrue(comfy._GPU_LOCK.acquire(blocking=False))

        try:
            with self.assertRaises(RuntimeError) as caught:
                comfy.comfy_img2img(call({"image_path": "a.png", "prompt": "x"}))

            self.assertIn("已有出图任务", str(caught.exception))

        finally:
            comfy._GPU_LOCK.release()

    def test_lock_released_after_failure(self):
        with patch.object(comfy, "free_vram_mb", return_value=5000), patch.object(
            comfy, "_request", side_effect=RuntimeError("ComfyUI 未运行：连不上")
        ):
            with self.assertRaises(RuntimeError):
                comfy.comfy_img2img(call({"image_path": "a.png", "prompt": "x"}))

        self.assertTrue(comfy._GPU_LOCK.acquire(blocking=False))
        comfy._GPU_LOCK.release()

    def test_timeout_default_is_120(self):
        import os

        previous = os.environ.pop("PA_COMFY_TIMEOUT", None)

        try:
            self.assertEqual(comfy.timeout_seconds(), 120.0)

        finally:
            if previous is not None:
                os.environ["PA_COMFY_TIMEOUT"] = previous


class LockThreadCase(unittest.TestCase):
    def test_other_thread_blocked(self):
        acquired = threading.Event()
        release = threading.Event()

        def holder():
            with comfy._GPU_LOCK:
                acquired.set()
                release.wait(2)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        acquired.wait(2)

        try:
            with self.assertRaises(RuntimeError):
                comfy.comfy_img2img(call({"image_path": "a.png", "prompt": "x"}))

        finally:
            release.set()
            thread.join(2)


if __name__ == "__main__":
    unittest.main()
