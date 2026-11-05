"""控制台总览接口：鉴权与聚合字段。"""

from __future__ import annotations

import unittest

from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from src.auth_api import get_current_user
from src.main import app
from tests.helper import isolate_paths


class ConsoleOverviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        isolate_paths(self, self.temp)

        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def tearDown(self):
        app.dependency_overrides.pop(get_current_user, None)

    def test_requires_authentication(self):
        response = self.client.get("/console/overview")

        self.assertEqual(response.status_code, 401, response.text)

    def test_returns_overview_for_authenticated_user(self):
        app.dependency_overrides[get_current_user] = lambda: {
            "id": "u1",
            "username": "tester",
            "role": "owner",
        }

        response = self.client.get("/console/overview")

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()

        for key in (
            "generated_at",
            "routes",
            "queue",
            "nodes",
            "sandboxes",
            "execs",
        ):
            self.assertIn(key, body)

        self.assertEqual(body["viewer"], "tester")
        self.assertIn("active", body["queue"])
        self.assertIn("queued", body["queue"])


if __name__ == "__main__":
    unittest.main()
