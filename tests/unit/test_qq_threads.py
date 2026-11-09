"""QQ 短上下文存储测试。"""

import tempfile
import unittest

from pathlib import Path

from src.qq.threads import ThreadStore


class ThreadStoreCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.store = ThreadStore(Path(self._temporary.name) / "threads.db")

    def test_append_and_latest(self):
        self.store.append_turn(sender=7, question="3+3", answer="6", message_id="m1")

        turn = self.store.latest_turn(7)

        self.assertEqual(turn["question"], "3+3")
        self.assertEqual(turn["answer"], "6")

    def test_senders_isolated(self):
        self.store.append_turn(sender=7, question="a", answer="b", message_id="m1")
        self.store.append_turn(sender=8, question="c", answer="d", message_id="m2")

        self.assertEqual(self.store.latest_turn(7)["question"], "a")
        self.assertEqual(self.store.latest_turn(8)["question"], "c")

    def test_reset_only_current_sender_and_idempotent(self):
        self.store.append_turn(sender=7, question="a", answer="b", message_id="m1")
        self.store.append_turn(sender=8, question="c", answer="d", message_id="m2")

        self.assertEqual(self.store.reset(7), 2)
        self.assertEqual(self.store.reset(7), 0)
        self.assertIsNone(self.store.latest_turn(7))
        self.assertIsNotNone(self.store.latest_turn(8))

    def test_keep_at_most_two_turns(self):
        for index in range(3):
            self.store.append_turn(sender=7, question=f"q{index}", answer=f"a{index}", message_id=f"m{index}")

        self.assertEqual(self.store.count(7), 4)
        self.assertEqual(self.store.latest_turn(7)["question"], "q2")

    def test_missing_sender_returns_none(self):
        self.assertIsNone(self.store.latest_turn(999))


if __name__ == "__main__":
    unittest.main()
