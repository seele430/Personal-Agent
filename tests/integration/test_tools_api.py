"""ComfyUI 端点测试：鉴权、参数校验、503/502 语义、成功返回。"""

import unittest

from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.tools_api as tools_api

from src.auth_api import get_current_user


class ToolsApiCase(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(tools_api.router)
        self.app.dependency_overrides[get_current_user] = lambda: {"id": "u1"}
        self.client = TestClient(self.app)

    def post(self, payload):
        return self.client.post("/api/tools/comfy/img2img", json=payload)


class TestAuth(ToolsApiCase):
    def test_requires_login(self):
        self.app.dependency_overrides.pop(get_current_user, None)

        response = self.post({"image_path": "a.png", "prompt": "x"})

        self.assertIn(response.status_code, (401, 403))


class TestValidation(ToolsApiCase):
    def test_empty_prompt(self):
        response = self.post({"image_path": "a.png", "prompt": "  "})

        self.assertEqual(response.status_code, 400)

    def test_empty_image(self):
        response = self.post({"image_path": "", "prompt": "x"})

        self.assertEqual(response.status_code, 400)

    def test_value_error_maps_to_400(self):
        with patch.object(
            tools_api.comfy_tools,
            "comfy_img2img",
            side_effect=ValueError("steps 必须在 1..150"),
        ):
            response = self.post({"image_path": "a.png", "prompt": "x", "steps": 999})

        self.assertEqual(response.status_code, 400)
        self.assertIn("steps", response.json()["detail"])


class TestServiceStates(ToolsApiCase):
    def test_service_down_is_503(self):
        with patch.object(
            tools_api.comfy_tools,
            "comfy_img2img",
            side_effect=RuntimeError("ComfyUI 未运行：连不上 http://127.0.0.1:8188"),
        ):
            response = self.post({"image_path": "a.png", "prompt": "x"})

        self.assertEqual(response.status_code, 503)
        self.assertIn("未运行", response.json()["detail"])

    def test_execution_error_is_502(self):
        with patch.object(
            tools_api.comfy_tools,
            "comfy_img2img",
            side_effect=RuntimeError("ComfyUI 执行失败"),
        ):
            response = self.post({"image_path": "a.png", "prompt": "x"})

        self.assertEqual(response.status_code, 502)


class TestSuccess(ToolsApiCase):
    def test_returns_images(self):
        with patch.object(
            tools_api.comfy_tools,
            "comfy_img2img",
            return_value={
                "success": True,
                "prompt_id": "p1",
                "images": ["out.png"],
                "path": "out.png",
            },
        ):
            response = self.post({"image_path": "a.png", "prompt": "a cat"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertTrue(payload["success"])
        self.assertEqual(payload["images"], ["out.png"])
        self.assertEqual(payload["prompt_id"], "p1")

    def test_never_returns_200_on_error(self):
        with patch.object(
            tools_api.comfy_tools,
            "comfy_img2img",
            side_effect=RuntimeError("超时"),
        ):
            response = self.post({"image_path": "a.png", "prompt": "x"})

        self.assertNotEqual(response.status_code, 200)

    def test_low_vram_is_503(self):
        with patch.object(
            tools_api.comfy_tools,
            "comfy_img2img",
            side_effect=RuntimeError(
                "显存不足：空闲 1200 MB < 2500 MB，先关掉游戏/浏览器"
            ),
        ):
            response = self.post({"image_path": "a.png", "prompt": "x"})

        self.assertEqual(response.status_code, 503)
        self.assertIn("显存不足", response.json()["detail"])

    def test_concurrent_run_is_503(self):
        with patch.object(
            tools_api.comfy_tools,
            "comfy_img2img",
            side_effect=RuntimeError("已有出图任务在跑，请等它结束（禁止串行连跑）"),
        ):
            response = self.post({"image_path": "a.png", "prompt": "x"})

        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
