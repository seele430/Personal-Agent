"""能力快照测试：unknown 一律不可用。"""

import unittest

from src.collab.capability import (
    ACTIONS,
    CapabilityError,
    CapabilitySnapshot,
    snapshot,
)


class TestThreeState(unittest.TestCase):
    def test_unknown_is_not_usable(self):
        item = snapshot("kimi")

        for action in ACTIONS:
            self.assertEqual(item.state(action), "unknown")
            self.assertFalse(item.can(action))
            self.assertIn("未验证", item.reason(action))

    def test_true_is_usable(self):
        item = snapshot("kimi", submit=True)

        self.assertTrue(item.can("submit"))
        self.assertEqual(item.reason("submit"), "")

    def test_false_is_not_usable(self):
        item = snapshot("kimi", submit=False)

        self.assertFalse(item.can("submit"))
        self.assertIn("不支持", item.reason("submit"))

    def test_mixed_capabilities(self):
        item = snapshot("dsh", submit=True, cancel=False, events=None)

        self.assertTrue(item.can("submit"))
        self.assertFalse(item.can("cancel"))
        self.assertFalse(item.can("events"))

    def test_unknown_action_rejected(self):
        item = snapshot("kimi")

        with self.assertRaises(CapabilityError):
            item.state("delete_everything")

    def test_empty_provider_rejected(self):
        with self.assertRaises(CapabilityError):
            CapabilitySnapshot(provider="  ")

    def test_non_bool_rejected(self):
        with self.assertRaises(CapabilityError):
            CapabilitySnapshot(provider="kimi", submit="yes")

    def test_serializes_with_states(self):
        payload = snapshot("kimi", submit=True).to_dict()

        self.assertEqual(payload["states"]["submit"], "verified")
        self.assertEqual(payload["states"]["cancel"], "unknown")
        self.assertEqual(payload["provider"], "kimi")

    def test_roundtrip(self):
        original = snapshot("kimi", submit=True, artifacts=False)
        restored = CapabilitySnapshot.from_dict(original.to_dict())

        self.assertTrue(restored.can("submit"))
        self.assertEqual(restored.state("artifacts"), "unsupported")

    def test_from_dict_rejects_non_object(self):
        with self.assertRaises(CapabilityError):
            CapabilitySnapshot.from_dict([])

    def test_snapshot_stamps_time(self):
        self.assertIn("T", snapshot("kimi").captured_at)

    def test_notes_preserved(self):
        item = CapabilitySnapshot(provider="kimi", notes="探测于 11-07")

        self.assertEqual(item.notes, "探测于 11-07")


if __name__ == "__main__":
    unittest.main()
