"""扩缩容事件持久化 + 动作标签映射（W3 复核 R2 / R5 / R8）。"""

from __future__ import annotations

import unittest

from tempfile import TemporaryDirectory

from src.platform.scale_events import (
    ACTION_SCALE_DOWN,
    ACTION_SCALE_UP,
    ScaleEventStore,
    describe,
    is_change,
)
from tests.helper import isolate_paths


class DescribeTest(unittest.TestCase):
    def test_scale_up_is_scale_out(self):
        info = describe(ACTION_SCALE_UP, "load_above_capacity")

        self.assertEqual(info["label"], "scale_out")
        self.assertEqual(info["title"], "扩容")

    def test_scale_down_is_scale_in(self):
        info = describe(ACTION_SCALE_DOWN, "load_below_capacity")

        self.assertEqual(info["label"], "scale_in")

    def test_pending_is_hold(self):
        info = describe("none", "scale_up_pending:1")

        self.assertEqual(info["label"], "hold")
        self.assertEqual(info["detail"], "pending")

    def test_cooldown_is_hold(self):
        info = describe("none", "scale_down_cooldown")

        self.assertEqual(info["label"], "hold")
        self.assertEqual(info["detail"], "cooldown")

    def test_steady_is_hold(self):
        info = describe("none", "steady")

        self.assertEqual(info["label"], "hold")
        self.assertEqual(info["detail"], "steady")

    def test_unknown_reason_is_not_guessed_as_scaling(self):
        info = describe("none", "something_new")

        self.assertEqual(info["label"], "hold")
        self.assertEqual(info["detail"], "something_new")

        unknown = describe("explode", "boom")

        self.assertEqual(unknown["label"], "unknown")

    def test_only_capacity_actions_are_changes(self):
        self.assertTrue(is_change(ACTION_SCALE_UP))
        self.assertTrue(is_change(ACTION_SCALE_DOWN))
        self.assertFalse(is_change("none"))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_paths(self, self.temp)

        self.store = ScaleEventStore()

    def record(self, **overrides):
        payload = {
            "action": ACTION_SCALE_UP,
            "reason": "load_above_capacity",
            "before_capacity": 1,
            "after_capacity": 2,
            "running": 9,
            "queued": 1,
        }
        payload.update(overrides)

        return self.store.record(**payload)

    def test_record_and_recent(self):
        event = self.record()

        self.assertEqual(event["label"], "scale_out")
        self.assertEqual(event["before_capacity"], 1)
        self.assertEqual(event["after_capacity"], 2)

        rows = self.store.recent(5)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], event["id"])

    def test_hold_is_rejected(self):
        with self.assertRaises(ValueError):
            self.record(action="none", reason="steady")

    def test_unknown_source_is_rejected(self):
        with self.assertRaises(ValueError):
            self.record(source="whatever")

    def test_negative_capacity_is_rejected(self):
        with self.assertRaises(ValueError):
            self.record(before_capacity=-1)

    def test_recent_limit(self):
        for index in range(3):
            self.record(after_capacity=2 + index)

        self.assertEqual(len(self.store.recent(2)), 2)
        self.assertEqual(self.store.recent(0), [])

    def test_events_survive_new_store_instance(self):
        self.record()

        reopened = ScaleEventStore()

        self.assertEqual(reopened.count(), 1)

    def test_count_by_source(self):
        self.record()
        self.record(source="simulation")

        self.assertEqual(self.store.count(), 2)
        self.assertEqual(self.store.count(source="simulation"), 1)


if __name__ == "__main__":
    unittest.main()
