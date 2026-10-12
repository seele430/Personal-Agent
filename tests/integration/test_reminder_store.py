"""多用户提醒、历史兼容及幂等测试。"""

import sqlite3
import unittest

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from src.core.reminder_store import (
    ReminderConflict,
    ReminderStore,
)


class TestReminderStore(unittest.TestCase):

    NOW = datetime(
        2026, 10, 12, 12,
        tzinfo=timezone.utc,
    )

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / "reminders.db"

        self.store = ReminderStore(self.path)
        self.store.init_db()

    def tearDown(self):
        self.temp.cleanup()

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def create(
        self,
        request_id="chat_request_001",
        user_id="member_001",
        role="member",
        message="喝水",
        minutes=5,
        now=None,
    ):
        return self.store.create(
            user_id=user_id,
            request_id=request_id,
            role=role,
            message=message,
            minutes=minutes,
            now=self.NOW if now is None else now,
        )

    def counts(self):
        with closing(self.connect()) as conn:
            legacy = conn.execute(
                "SELECT COUNT(*) FROM reminders"
            ).fetchone()[0]

            owned = conn.execute(
                "SELECT COUNT(*) FROM reminder_requests"
            ).fetchone()[0]

        return legacy, owned

    def test_history_is_preserved(self):
        with closing(self.connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO reminders
                    (message, remind_at, status)
                VALUES (?, ?, 'pending')
                """,
                ("原来的提醒", self.NOW.isoformat()),
            )

        self.store.init_db()

        with closing(self.connect()) as conn:
            row = conn.execute(
                """
                SELECT message, status
                FROM reminders
                """
            ).fetchone()

        self.assertEqual(
            row,
            ("原来的提醒", "pending"),
        )
        self.assertEqual(self.counts(), (1, 0))

    def test_owner_uses_existing_ntfy_table(self):
        result = self.create(
            user_id="owner_001",
            role="owner",
        )

        self.assertEqual(result["delivery"], "ntfy")
        self.assertIsNotNone(
            result["legacy_reminder_id"]
        )
        self.assertEqual(self.counts(), (1, 1))

    def test_member_never_enters_ntfy_table(self):
        result = self.create()

        self.assertEqual(result["delivery"], "in_app")
        self.assertIsNone(
            result["legacy_reminder_id"]
        )
        self.assertEqual(self.counts(), (0, 1))

    def test_other_roles_are_in_app_only(self):
        for role in ("admin", "trusted", "member"):
            self.create(
                request_id=f"request_{role}_001",
                user_id=f"user_{role}",
                role=role,
            )

        self.assertEqual(self.counts(), (0, 3))

    def test_duplicate_request_does_not_insert(self):
        first = self.create()

        second = self.create(
            now=self.NOW + timedelta(minutes=1)
        )

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.counts(), (0, 1))

    def test_cross_user_and_changed_message_rejected(self):
        self.create()

        with self.assertRaises(ReminderConflict):
            self.create(user_id="another_user")

        with self.assertRaises(ReminderConflict):
            self.create(message="不同的内容")

        self.assertEqual(self.counts(), (0, 1))

    def test_in_app_results_are_user_isolated(self):
        self.create(
            request_id="request_alice_001",
            user_id="alice",
            message="Alice 的提醒",
        )

        self.create(
            request_id="request_bob_001",
            user_id="bob",
            message="Bob 的提醒",
        )

        future = self.NOW + timedelta(minutes=6)

        alice = self.store.due_in_app(
            user_id="alice",
            now=future,
        )

        bob = self.store.due_in_app(
            user_id="bob",
            now=future,
        )

        self.assertEqual(len(alice), 1)
        self.assertEqual(len(bob), 1)
        self.assertEqual(
            alice[0]["message"],
            "Alice 的提醒",
        )
        self.assertEqual(
            bob[0]["message"],
            "Bob 的提醒",
        )

        self.assertEqual(
            self.store.due_in_app(
                user_id="alice",
                now=self.NOW,
            ),
            [],
        )

    def test_concurrent_duplicates_create_only_one(self):
        def attempt(_):
            return self.create(
                request_id="concurrent_request_001",
                user_id="owner_001",
                role="owner",
            )

        with ThreadPoolExecutor(
            max_workers=5,
        ) as executor:
            results = list(
                executor.map(attempt, range(5))
            )

        self.assertEqual(
            sum(item["created"] for item in results),
            1,
        )
        self.assertEqual(self.counts(), (1, 1))

    def test_invalid_role_or_minutes_rejected(self):
        with self.assertRaises(ValueError):
            self.create(role="unknown")

        with self.assertRaises(ValueError):
            self.create(minutes=0)

        with self.assertRaises(ValueError):
            self.create(minutes=True)

        self.assertEqual(self.counts(), (0, 0))


if __name__ == "__main__":
    unittest.main()