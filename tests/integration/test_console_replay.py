"""控制台场景回放：只读持久事件、来源隔离、时间偏移（V2 §8.1）。"""

from __future__ import annotations

import unittest

from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from src.auth_api import get_current_user
from src.main import app
from src.platform.scale_events import ScaleEventStore
from tests.helper import isolate_paths


def iso(offset_seconds: int) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    ).isoformat()


class ConsoleReplayTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        isolate_paths(self, self.temp)

        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def tearDown(self):
        app.dependency_overrides.pop(get_current_user, None)

    def authenticate(self):
        app.dependency_overrides[get_current_user] = lambda: {
            "id": "u1",
            "username": "tester",
            "role": "owner",
        }

    def fetch(self, **params):
        self.authenticate()

        return self.client.get("/console/replay", params=params)

    def seed(self):
        store = ScaleEventStore()

        store.record(
            action="scale_up",
            reason="load_above_capacity",
            before_capacity=0,
            after_capacity=1,
            running=9,
            queued=1,
            at=iso(-120),
        )
        store.record(
            action="scale_up",
            reason="load_above_capacity",
            before_capacity=1,
            after_capacity=2,
            running=9,
            queued=1,
            at=iso(-60),
        )
        store.record(
            action="scale_down",
            reason="load_below_capacity",
            before_capacity=2,
            after_capacity=1,
            running=0,
            queued=0,
            at=iso(-10),
        )
        store.record(
            action="scale_up",
            reason="load_above_capacity",
            before_capacity=1,
            after_capacity=2,
            running=9,
            queued=0,
            source="simulation",
            at=iso(-5),
        )

    # ------------------------------------------------------------------

    def test_requires_authentication(self):
        response = self.client.get("/console/replay")

        self.assertEqual(response.status_code, 401, response.text)

    def test_empty_state_points_to_demo_script(self):
        body = self.fetch().json()

        self.assertEqual(body["event_count"], 0)
        self.assertEqual(body["steps"], [])
        self.assertIn("demo_elastic_loop", body["note"])

    def test_steps_are_chronological_with_offsets(self):
        self.seed()

        body = self.fetch().json()

        self.assertEqual(body["event_count"], 3)
        self.assertEqual([step["seq"] for step in body["steps"]], [1, 2, 3])
        self.assertEqual(
            [step["action"] for step in body["steps"]],
            ["scale_up", "scale_up", "scale_down"],
        )
        self.assertEqual(body["steps"][0]["offset_seconds"], 0.0)
        self.assertAlmostEqual(body["steps"][1]["offset_seconds"], 60.0, delta=2.0)
        self.assertAlmostEqual(body["steps"][2]["since_previous_seconds"], 50.0, delta=2.0)
        self.assertEqual(body["steps"][0]["label"], "scale_out")
        self.assertEqual(body["summary"]["scale_up"], 2)
        self.assertEqual(body["summary"]["scale_down"], 1)
        self.assertEqual(body["summary"]["max_capacity"], 2)
        self.assertEqual(body["summary"]["min_capacity"], 0)

    def test_simulation_events_are_separate(self):
        self.seed()

        live = self.fetch().json()

        self.assertEqual(live["source"], "live")
        self.assertEqual(live["event_count"], 3)

        simulated = self.fetch(source="simulation").json()

        self.assertEqual(simulated["source"], "simulation")
        self.assertEqual(simulated["event_count"], 1)
        self.assertEqual(simulated["steps"][0]["source"], "simulation")

    def test_unknown_source_is_400(self):
        response = self.fetch(source="whatever")

        self.assertEqual(response.status_code, 400, response.text)


if __name__ == "__main__":
    unittest.main()
