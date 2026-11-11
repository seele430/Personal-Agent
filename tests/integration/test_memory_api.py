"""记忆 API：owner 隔离、更新、历史、422。"""

import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.auth_api import get_current_user
from src.main import app
from src.memory.store import MemoryStore


class MemoryApiCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.store = MemoryStore(Path(self._temporary.name) / "memory.db")
        self._original = app.dependency_overrides.get(get_current_user)
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "role": "member"}
        self.client = TestClient(app)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._original is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = self._original

    def _request(self, method, path, **kwargs):
        with patch("src.memory.service.get_memory_store", return_value=self.store):
            return self.client.request(method, path, **kwargs)

    def test_unauthenticated_401(self):
        app.dependency_overrides.pop(get_current_user, None)
        self.assertEqual(self.client.get("/api/memory/items?query=x").status_code, 401)

    def test_create_and_recall(self):
        created = self._request("POST", "/api/memory/items", json={"text": "喜欢绿茶", "key": "drink"})
        self.assertEqual(created.status_code, 201)
        item_id = created.json()["id"]
        recalled = self._request("GET", "/api/memory/items?query=绿茶")
        self.assertEqual(recalled.status_code, 200)
        self.assertEqual(recalled.json()["items"][0]["id"], item_id)
        self.assertEqual(self._request("GET", f"/api/memory/items/{item_id}").status_code, 200)

    def test_update_and_history(self):
        old = self._request("POST", "/api/memory/items", json={"text": "旧值", "key": "k"}).json()["id"]
        new = self._request("POST", "/api/memory/items", json={"text": "新值", "key": "k"}).json()
        self.assertEqual(new["supersedes_id"], old)
        history = self._request("GET", f"/api/memory/items/{old}")
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.json()["text"], "旧值")

    def test_extra_owner_field_rejected(self):
        response = self._request("POST", "/api/memory/items", json={"text": "x", "owner": "u2"})
        self.assertEqual(response.status_code, 422)

    def test_cross_owner_404(self):
        item_id = self._request("POST", "/api/memory/items", json={"text": "x"}).json()["id"]
        app.dependency_overrides[get_current_user] = lambda: {"id": "u2", "role": "member"}
        self.assertEqual(self._request("GET", f"/api/memory/items/{item_id}").status_code, 404)



    def test_list_mode_and_fields(self):
        self._request("POST", "/api/memory/items", json={"text": "绿茶", "key": "drink"})
        self._request("POST", "/api/memory/items", json={"text": "红茶", "key": "drink"})
        response = self._request("GET", "/api/memory/items")
        body = response.json()
        self.assertEqual(body["mode"], "list")
        self.assertEqual(body["total"], 1)
        self.assertNotIn("owner_id", body["items"][0])

    def test_search_offset_rejected(self):
        response = self._request("GET", "/api/memory/items?query=x&offset=1")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], "search_pagination_unsupported")

    def test_history_endpoint(self):
        old = self._request("POST", "/api/memory/items", json={"text": "旧", "key": "k"}).json()["id"]
        new = self._request("POST", "/api/memory/items", json={"text": "新", "key": "k"}).json()["id"]
        response = self._request("GET", f"/api/memory/items/{old}/history")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["current_id"], new)
        self.assertEqual([row["id"] for row in body["items"]], [old, new])
        self.assertNotIn("owner_id", body["items"][0])



    def test_delete_endpoint(self):
        item_id = self._request("POST", "/api/memory/items", json={"text": "删我"}).json()["id"]
        self.assertEqual(self._request("DELETE", f"/api/memory/items/{item_id}").status_code, 204)
        self.assertEqual(self._request("GET", f"/api/memory/items/{item_id}").status_code, 200)
        self.assertIsNotNone(self._request("GET", f"/api/memory/items/{item_id}").json()["text"])

    def test_delete_cross_owner_404(self):
        item_id = self._request("POST", "/api/memory/items", json={"text": "x"}).json()["id"]
        app.dependency_overrides[get_current_user] = lambda: {"id": "u2", "role": "member"}
        self.assertEqual(self._request("DELETE", f"/api/memory/items/{item_id}").status_code, 404)


if __name__ == "__main__":
    unittest.main()
