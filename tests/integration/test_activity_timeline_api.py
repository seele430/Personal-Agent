"""活动时间线 API 测试：鉴权 + 参数校验 + 空态（挂真实 router）。"""

import sqlite3
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.activity_api as activity_api

from src.auth_api import get_current_user
from src.config import build_paths


class TimelineApiCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)

        self.base = Path(self._temporary.name)
        self.paths = build_paths(self.base)

        self.app = FastAPI()
        self.app.include_router(activity_api.router)
        self.app.dependency_overrides[get_current_user] = lambda: {"id": "u1"}
        self.client = TestClient(self.app)

        self.patcher = patch("src.config.paths", self.paths)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def make_spans(self):
        connection = sqlite3.connect(self.base / "traces.db")

        with connection:
            connection.execute(
                "CREATE TABLE spans (span_id TEXT, name TEXT, status TEXT, "
                "attrs TEXT, created_at TEXT)"
            )
            connection.execute(
                "INSERT INTO spans VALUES ('s1','llm.generate','ok','{}','2026-11-01T10:00:00')"
            )

        connection.close()

    def test_requires_auth(self):
        self.app.dependency_overrides.pop(get_current_user, None)

        response = self.client.get("/api/activity/timeline")

        self.assertIn(response.status_code, (401, 403))

    def test_empty_state_returns_zero(self):
        response = self.client.get("/api/activity/timeline")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["events"], [])
        self.assertEqual(response.json()["count"], 0)

    def test_returns_events(self):
        self.make_spans()

        response = self.client.get("/api/activity/timeline?limit=5")

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["events"][0]["source"], "span")

    def test_rejects_bad_limit(self):
        response = self.client.get("/api/activity/timeline?limit=0")

        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
