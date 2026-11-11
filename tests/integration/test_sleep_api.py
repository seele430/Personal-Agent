"""Sleep 记录 API 测试。"""

import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.auth_api import get_current_user
from src.main import app
from src.sleep_store import SleepRunStore


class SleepApiCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.store = SleepRunStore(Path(self._temporary.name) / "sleep_runs.db")
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
        with patch("src.sleep_api.SleepRunStore", return_value=self.store):
            return self.client.request(method, path, **kwargs)

    def test_unauthenticated_401(self):
        app.dependency_overrides.pop(get_current_user, None)
        response = self.client.get("/api/sleep/runs")
        self.assertEqual(response.status_code, 401)

    def test_list_own_runs(self):
        self.store.add_run(owner_id="u1", mode="plan")
        self.store.add_run(owner_id="u2", mode="plan")
        response = self._request("GET", "/api/sleep/runs")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], 1)

    def test_filters(self):
        self.store.add_run(owner_id="u1", mode="plan")
        self.store.add_run(owner_id="u1", mode="ab", gate_status="failed")
        response = self._request("GET", "/api/sleep/runs?mode=ab")
        self.assertEqual(response.json()["total"], 1)
        response = self._request("GET", "/api/sleep/runs?mode=bad")
        self.assertEqual(response.status_code, 422)

    def test_detail_isolation_and_shape(self):
        own = self.store.add_run(owner_id="u1", mode="plan", ops=[
            {"op": "expire", "reason": "exact_dedupe", "item_id": 1,
             "item_key": "k1", "evidence_ids": [2], "evidence_keys": ["k1"]},
        ])
        other = self.store.add_run(owner_id="u2", mode="plan")
        response = self._request("GET", f"/api/sleep/runs/{own}")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertNotIn("owner_id", body)
        self.assertEqual(body["ops"][0]["item_key"], "k1")
        self.assertEqual(self._request("GET", f"/api/sleep/runs/{other}").status_code, 404)
        self.assertEqual(self._request("GET", "/api/sleep/runs/999999").status_code, 404)

    def test_no_store_header(self):
        response = self._request("GET", "/api/sleep/runs")
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")


if __name__ == "__main__":
    unittest.main()
