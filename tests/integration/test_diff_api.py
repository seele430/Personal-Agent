"""产物 Diff API 测试：文本差异、二进制摘要、越权、只读。"""

import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.artifacts_api as artifacts_api

from src.artifacts.registry import ArtifactRegistry
from src.auth_api import get_current_user


class DiffApiCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)

        self.base = Path(self._temporary.name)
        self.root = self.base / "artifacts"
        self.root.mkdir()

        self.registry = ArtifactRegistry(self.base / "artifacts.db", root=self.root)

        self.app = FastAPI()
        self.app.include_router(artifacts_api.router)
        self.user = {"id": "u1", "username": "alice", "role": "member"}
        self.app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(self.app)

        self.patcher = patch.object(artifacts_api, "get_registry", lambda: self.registry)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def add(self, name, content, owner="u1"):
        path = self.root / name

        if isinstance(content, bytes):
            path.write_bytes(content)

        else:
            path.write_text(content, encoding="utf-8")

        return self.registry.register(owner, name).id

    def test_text_diff(self):
        a = self.add("a.md", "第一行\n")
        b = self.add("b.md", "第一行\n第二行\n")

        response = self.client.get(f"/api/artifacts/{a}/diff/{b}")

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload["kind"], "text")
        self.assertEqual(payload["added"], 1)
        self.assertTrue(payload["changed"])

    def test_identical_diff(self):
        a = self.add("a.md", "same\n")
        b = self.add("b.md", "same\n")

        payload = self.client.get(f"/api/artifacts/{a}/diff/{b}").json()

        self.assertEqual(payload["kind"], "identical")
        self.assertFalse(payload["changed"])

    def test_binary_diff(self):
        a = self.add("a.bin", b"\x00\x01")
        b = self.add("b.bin", b"\x00\x02")

        payload = self.client.get(f"/api/artifacts/{a}/diff/{b}").json()

        self.assertEqual(payload["kind"], "binary")
        self.assertNotEqual(payload["a_digest"], payload["b_digest"])

    def test_other_owner_cannot_diff(self):
        a = self.add("a.md", "x", owner="u1")
        b = self.add("b.md", "y", owner="u2")

        response = self.client.get(f"/api/artifacts/{a}/diff/{b}")

        self.assertIn(response.status_code, (403, 404))

    def test_missing_artifact(self):
        a = self.add("a.md", "x")

        response = self.client.get(f"/api/artifacts/{a}/diff/nope")

        self.assertEqual(response.status_code, 404)

    def test_no_absolute_path_leak(self):
        a = self.add("a.md", "x")
        b = self.add("b.md", "y")

        text = self.client.get(f"/api/artifacts/{a}/diff/{b}").text

        self.assertNotIn(str(self.root), text)


if __name__ == "__main__":
    unittest.main()
