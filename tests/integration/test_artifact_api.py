"""产物 API 测试：列表 / 元数据 / 预览 / 下载 / reveal / 安全边界。

用一个最小 FastAPI 应用挂载真实 router，鉴权用 dependency_overrides 换人，
产物库指向临时目录。
"""

import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.artifacts_api as artifacts_api

from src.artifacts.registry import ArtifactRegistry
from src.auth_api import get_current_user


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        self.base = Path(self.temp.name)
        self.root = self.base / "artifacts"
        self.root.mkdir()

        self.registry = ArtifactRegistry(
            self.base / "artifacts.db", root=self.root
        )

        self.app = FastAPI()
        self.app.include_router(artifacts_api.router)
        self.user = {"id": "u1", "username": "alice", "role": "member"}
        self.app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(self.app)

        self._patcher = patch.object(
            artifacts_api, "get_registry", lambda: self.registry
        )
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

        artifacts_api.reveal_opener = None
        self.addCleanup(setattr, artifacts_api, "reveal_opener", None)

    def write(self, name, content=b"data"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

        return path

    def register(self, name="a.txt", content=b"hello", **kwargs):
        return self.registry.register("u1", self.write(name, content), **kwargs)


class TestListingAndDetail(ApiTestCase):
    def test_empty_list(self):
        response = self.client.get("/api/artifacts")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"items": []})

    def test_register_then_list(self):
        self.write("a.txt", b"hello")

        response = self.client.post(
            "/api/artifacts/register", json={"path": "a.txt"}
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertTrue(payload["id"].startswith("art_"))
        self.assertEqual(payload["kind"], "text")

        listed = self.client.get("/api/artifacts").json()["items"]

        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], payload["id"])

    def test_register_requires_path(self):
        response = self.client.post("/api/artifacts/register", json={})

        self.assertEqual(response.status_code, 400)

    def test_detail_fields(self):
        artifact = self.register("report.md", b"# title")

        response = self.client.get(f"/api/artifacts/{artifact.id}")
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["kind"], "markdown")
        self.assertTrue(payload["previewable"])
        self.assertEqual(payload["size_bytes"], 7)

    def test_unknown_id_is_404(self):
        self.assertEqual(
            self.client.get("/api/artifacts/art_nope").status_code, 404
        )


class TestPreviewAndContent(ApiTestCase):
    def test_text_preview(self):
        artifact = self.register("run.log", b"line1\nline2")

        response = self.client.get(f"/api/artifacts/{artifact.id}/preview")
        payload = response.json()

        self.assertEqual(payload["mode"], "text")
        self.assertIn("line1", payload["text"])
        self.assertFalse(payload["truncated"])

    def test_markdown_preview_keeps_mode(self):
        artifact = self.register("doc.md", b"# hi")

        payload = self.client.get(f"/api/artifacts/{artifact.id}/preview").json()

        self.assertEqual(payload["mode"], "markdown")

    def test_image_preview_has_inline_url(self):
        artifact = self.register("shot.png", b"\x89PNG")

        payload = self.client.get(f"/api/artifacts/{artifact.id}/preview").json()

        self.assertEqual(payload["mode"], "inline")
        self.assertIn(f"/api/artifacts/{artifact.id}/content", payload["url"])

    def test_other_kind_is_download_only(self):
        artifact = self.register("blob.bin", b"\x00")

        payload = self.client.get(f"/api/artifacts/{artifact.id}/preview").json()

        self.assertFalse(payload["previewable"])
        self.assertEqual(payload["mode"], "download")
        self.assertIn("不支持预览", payload["note"])

    def test_content_download_headers(self):
        artifact = self.register("a.txt", b"payload")

        response = self.client.get(f"/api/artifacts/{artifact.id}/content")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"payload")
        self.assertIn("attachment", response.headers["content-disposition"])

    def test_content_inline(self):
        artifact = self.register("a.txt", b"payload")

        response = self.client.get(
            f"/api/artifacts/{artifact.id}/content?inline=1"
        )

        self.assertIn("inline", response.headers["content-disposition"])

    def test_limits_endpoint(self):
        payload = self.client.get("/api/artifacts/_limits/max-bytes").json()

        self.assertGreater(payload["max_bytes"], 0)


class TestSecurity(ApiTestCase):
    def test_other_owner_is_forbidden(self):
        artifact = self.register()

        self.user = {"id": "u2", "username": "bob", "role": "member"}

        response = self.client.get(f"/api/artifacts/{artifact.id}")

        self.assertEqual(response.status_code, 403)

    def test_register_outside_root_is_400_or_403(self):
        outside = self.base / "outside.txt"
        outside.write_bytes(b"x")

        response = self.client.post(
            "/api/artifacts/register", json={"path": str(outside)}
        )

        self.assertIn(response.status_code, (400, 403))

    def test_register_missing_file_is_404(self):
        response = self.client.post(
            "/api/artifacts/register", json={"path": "nope.txt"}
        )

        self.assertEqual(response.status_code, 404)

    def test_reveal_from_non_loopback_is_refused(self):
        artifact = self.register()
        called = []

        artifacts_api.reveal_opener = lambda path, art: called.append(path)

        response = self.client.post(f"/api/artifacts/{artifact.id}/reveal")
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(payload["revealed"])
        self.assertEqual(called, [])

    def test_reveal_without_opener_is_refused(self):
        artifact = self.register()

        payload = self.client.post(
            f"/api/artifacts/{artifact.id}/reveal"
        ).json()

        self.assertFalse(payload["revealed"])

    def test_can_reveal_only_loopback(self):
        for host in ("127.0.0.1", "::1", "localhost", "127.0.0.1:8000"):
            self.assertTrue(artifacts_api.can_reveal(host), host)

        for host in ("10.0.0.5", "192.168.1.2", "", None):
            self.assertFalse(artifacts_api.can_reveal(host), host)


if __name__ == "__main__":
    unittest.main()
