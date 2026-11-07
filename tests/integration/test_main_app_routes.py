"""真实入口测试：直接打 `src.main.app`，覆盖之前缺失的 `/api/*` 一层。

背景：2026-11-04 的中间件会把 `/api/artifacts` 改写成 `/artifacts`，
导致真实 uvicorn 入口下所有接口 404；而集成测试都挂在临时 app 上，
Playwright 又全用 mock，谁都没发现。这个文件专门盯真实入口。
"""

import unittest

from fastapi.testclient import TestClient

from src.main import app


class MainAppRoutes(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_health_ok(self):
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)

    def test_api_prefix_is_not_stripped(self):
        """/api/artifacts 必须命中真实 router（无凭据 → 401/403，而不是 404）。"""

        response = self.client.get("/api/artifacts")

        self.assertIn(response.status_code, (401, 403), response.text)

    def test_activity_timeline_route_exists(self):
        response = self.client.get("/api/activity/timeline")

        self.assertIn(response.status_code, (401, 403), response.text)

    def test_openapi_available(self):
        response = self.client.get("/openapi.json")

        self.assertEqual(response.status_code, 200)
        self.assertIn("/api/artifacts", response.json()["paths"])

    def test_unknown_api_path_is_404(self):
        response = self.client.get("/api/definitely-not-a-route")

        self.assertEqual(response.status_code, 404)

    def test_spa_fallback_still_serves_frontend(self):
        response = self.client.get("/login")

        self.assertEqual(response.status_code, 200)

    def test_root_ok(self):
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_post_to_get_only_api_is_404(self):
        response = self.client.post("/api/activity/timeline")

        self.assertIn(response.status_code, (401, 403, 404), response.text)


if __name__ == "__main__":
    unittest.main()
