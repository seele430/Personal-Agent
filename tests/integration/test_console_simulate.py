"""控制台 What-if 接口：默认取真实状态、模拟不改真实状态（V2 §8.2）。"""

from __future__ import annotations

import unittest

from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from src.auth_api import get_current_user
from src.main import app
from src.platform.audit import AuditLog
from src.platform.node_registry import NodeRegistry
from src.platform.scale_events import ScaleEventStore
from tests.helper import isolate_paths


class ConsoleSimulateTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        isolate_paths(self, self.temp)

        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

        app.dependency_overrides[get_current_user] = lambda: {
            "id": "u1",
            "username": "tester",
            "role": "owner",
        }

    def tearDown(self):
        app.dependency_overrides.pop(get_current_user, None)

    def simulate(self, **body):
        return self.client.post("/console/simulate", json=body)

    def test_requires_authentication(self):
        app.dependency_overrides.pop(get_current_user, None)

        response = self.client.post("/console/simulate", json={})

        self.assertEqual(response.status_code, 401, response.text)

    def test_defaults_come_from_real_state(self):
        body = self.simulate().json()

        self.assertEqual(body["data_source"], "simulation")
        self.assertFalse(body["applied"])
        self.assertEqual(body["inputs"]["running"], 0)
        self.assertEqual(body["inputs"]["queued"], 0)
        self.assertEqual(body["inputs"]["current_capacity"], 0)
        self.assertEqual(body["desired_capacity"], 1)
        self.assertEqual(body["action"], "scale_up")

    def test_explicit_inputs_are_respected(self):
        body = self.simulate(
            running=9,
            queued=3,
            current_capacity=1,
            policy={"capacity_per_node": 4, "headroom": 0.0, "max_nodes": 8},
        ).json()

        # ceil((9+3)/4) = 3
        self.assertEqual(body["desired_capacity"], 3)
        self.assertEqual(body["action"], "scale_up")
        self.assertEqual(body["policy"]["max_nodes"], 8)

    def test_invalid_policy_is_400(self):
        response = self.simulate(policy={"max_node": 3})

        self.assertEqual(response.status_code, 400, response.text)

    def test_simulation_does_not_touch_real_state(self):
        node = NodeRegistry().register(capacity=8)

        def stable_view():
            return [
                (
                    item["node_id"],
                    item["state"],
                    item["capacity"],
                    item["cordoned"],
                    item["draining"],
                    item["removed"],
                )
                for item in NodeRegistry().list_nodes()
            ]

        before = stable_view()
        events_before = ScaleEventStore().count()
        audit_before = len(AuditLog().list(limit=50))

        body = self.simulate(
            running=40,
            queued=10,
            current_capacity=1,
            policy={"capacity_per_node": 4, "headroom": 0.0, "max_nodes": 8},
        ).json()

        self.assertEqual(body["desired_capacity"], 8)
        self.assertEqual(stable_view(), before)
        self.assertEqual(ScaleEventStore().count(), events_before)
        self.assertEqual(len(AuditLog().list(limit=50)), audit_before)
        self.assertEqual(node["capacity"], 8)

    def test_two_calls_get_distinct_simulation_ids(self):
        first = self.simulate(running=1).json()
        second = self.simulate(running=1).json()

        self.assertNotEqual(first["simulation_id"], second["simulation_id"])


if __name__ == "__main__":
    unittest.main()
