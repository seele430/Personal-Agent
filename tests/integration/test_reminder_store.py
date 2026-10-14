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
    ReminderNotEditable,
    ReminderNotFound,
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


class TestReminderStoreManagement(unittest.TestCase):
    """提醒的查询、取消、修改与确认送达。"""

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

    def legacy_row(self, legacy_id):
        with closing(self.connect()) as conn:
            row = conn.execute(
                """
                SELECT message, remind_at, status
                FROM reminders
                WHERE id = ?
                """,
                (legacy_id,),
            ).fetchone()

        if row is None:
            return None

        return {
            "message": row[0],
            "remind_at": row[1],
            "status": row[2],
        }

    # 1. 只能看到自己的提醒。

    def test_list_returns_only_own_reminders(self):
        self.create(
            request_id="request_alice_001",
            user_id="alice",
        )
        self.create(
            request_id="request_bob_001",
            user_id="bob",
        )

        mine = self.store.list_for_user(user_id="alice")

        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["message"], "喝水")

        # 内部字段不外泄。
        self.assertNotIn("user_id", mine[0])
        self.assertNotIn("request_id", mine[0])

    # 2. 可以按状态过滤，待发提醒排在最前。

    def test_list_filters_status_and_orders_pending_first(
        self,
    ):
        first = self.create(
            request_id="request_alpha_001",
        )
        second = self.create(
            request_id="request_beta_002",
        )

        self.store.cancel(
            user_id="member_001",
            reminder_id=first["id"],
        )

        everything = self.store.list_for_user(
            user_id="member_001",
        )

        self.assertEqual(
            [item["status"] for item in everything],
            ["pending", "cancelled"],
        )

        pending = self.store.list_for_user(
            user_id="member_001",
            status="pending",
        )

        self.assertEqual(
            [item["id"] for item in pending],
            [second["id"]],
        )

    # 3. 参数非法时直接拒绝。

    def test_list_rejects_invalid_arguments(self):
        for kwargs in (
            {"user_id": ""},
            {"user_id": "member_001", "status": "unknown"},
            {"user_id": "member_001", "limit": 0},
            {"user_id": "member_001", "limit": True},
        ):
            with self.assertRaises(ValueError):
                self.store.list_for_user(**kwargs)

    # 4. 取消自己的提醒，且可以重复调用。

    def test_cancel_own_pending_reminder(self):
        created = self.create()

        result = self.store.cancel(
            user_id="member_001",
            reminder_id=created["id"],
        )

        self.assertTrue(result["changed"])
        self.assertEqual(result["status"], "cancelled")

        again = self.store.cancel(
            user_id="member_001",
            reminder_id=created["id"],
        )

        self.assertFalse(again["changed"])

    # 5. 站长的旧表记录必须一起取消，否则 ntfy 仍会推送。

    def test_cancel_owner_reminder_also_cancels_legacy_row(
        self,
    ):
        created = self.create(
            user_id="owner_001",
            role="owner",
        )

        legacy_id = created["legacy_reminder_id"]
        self.assertIsNotNone(legacy_id)

        self.assertEqual(
            self.legacy_row(legacy_id)["status"],
            "pending",
        )

        self.store.cancel(
            user_id="owner_001",
            reminder_id=created["id"],
        )

        self.assertEqual(
            self.legacy_row(legacy_id)["status"],
            "cancelled",
        )

    # 6. 不能取消别人的提醒，且与"不存在"返回同一个错误。

    def test_cancel_other_users_reminder_is_not_found(
        self,
    ):
        created = self.create(user_id="alice")

        with self.assertRaises(ReminderNotFound):
            self.store.cancel(
                user_id="bob",
                reminder_id=created["id"],
            )

        with self.assertRaises(ReminderNotFound):
            self.store.cancel(
                user_id="alice",
                reminder_id=created["id"] + 999,
            )

    # 7. 已经送达的提醒不能再取消。

    def test_cancel_sent_reminder_is_not_changed(self):
        created = self.create(
            now=self.NOW - timedelta(hours=1),
        )

        acked = self.store.mark_delivered(
            user_id="member_001",
            reminder_id=created["id"],
            now=self.NOW,
        )

        self.assertTrue(acked["changed"])

        result = self.store.cancel(
            user_id="member_001",
            reminder_id=created["id"],
        )

        self.assertFalse(result["changed"])
        self.assertEqual(result["status"], "sent")

    # 8. 只改内容时，时间保持不变。

    def test_update_message_keeps_remind_at(self):
        created = self.create()

        result = self.store.update(
            user_id="member_001",
            reminder_id=created["id"],
            message="  改成喝水  ",
            now=self.NOW + timedelta(minutes=1),
        )

        self.assertEqual(result["message"], "改成喝水")
        self.assertEqual(
            result["remind_at"],
            created["remind_at"],
        )
        self.assertEqual(result["minutes"], 5)

    # 9. 改分钟数会按当前时间重算，并同步旧表。

    def test_update_minutes_recomputes_time_and_legacy_row(
        self,
    ):
        created = self.create(
            user_id="owner_001",
            role="owner",
        )

        later = self.NOW + timedelta(hours=2)

        result = self.store.update(
            user_id="owner_001",
            reminder_id=created["id"],
            minutes=30,
            now=later,
        )

        expected = (
            later + timedelta(minutes=30)
        ).isoformat()

        self.assertEqual(result["remind_at"], expected)

        legacy = self.legacy_row(
            created["legacy_reminder_id"]
        )

        self.assertEqual(legacy["remind_at"], expected)
        self.assertEqual(legacy["status"], "pending")

    # 10. 已发送或已取消的提醒不能被改回待发。

    def test_update_rejects_non_pending_reminder(self):
        created = self.create()

        self.store.cancel(
            user_id="member_001",
            reminder_id=created["id"],
        )

        with self.assertRaises(ReminderNotEditable):
            self.store.update(
                user_id="member_001",
                reminder_id=created["id"],
                message="改不动的提醒",
            )

    # 11. 修改参数校验。

    def test_update_validates_arguments(self):
        created = self.create()

        with self.assertRaises(ValueError):
            self.store.update(
                user_id="member_001",
                reminder_id=created["id"],
            )

        with self.assertRaises(ValueError):
            self.store.update(
                user_id="member_001",
                reminder_id=created["id"],
                message="   ",
            )

        with self.assertRaises(ValueError):
            self.store.update(
                user_id="member_001",
                reminder_id=created["id"],
                minutes=0,
            )

        with self.assertRaises(ReminderNotFound):
            self.store.update(
                user_id="bob",
                reminder_id=created["id"],
                message="别人的提醒",
            )

    # 12. 确认送达后，到期列表不再重复返回同一条。

    def test_mark_delivered_stops_repeat_notifications(
        self,
    ):
        created = self.create(
            now=self.NOW - timedelta(hours=1),
        )

        due = self.store.due_in_app(
            user_id="member_001",
            now=self.NOW,
        )

        self.assertEqual(len(due), 1)

        result = self.store.mark_delivered(
            user_id="member_001",
            reminder_id=created["id"],
            now=self.NOW,
        )

        self.assertTrue(result["changed"])

        self.assertEqual(
            self.store.due_in_app(
                user_id="member_001",
                now=self.NOW,
            ),
            [],
        )

    # 13. 未到期不能被提前确认。

    def test_mark_delivered_ignores_future_reminder(self):
        created = self.create(minutes=60)

        result = self.store.mark_delivered(
            user_id="member_001",
            reminder_id=created["id"],
            now=self.NOW,
        )

        self.assertFalse(result["changed"])
        self.assertEqual(result["status"], "pending")

    # 14. 站长的 ntfy 提醒不属于站内提醒。

    def test_mark_delivered_ignores_owner_ntfy_reminder(
        self,
    ):
        created = self.create(
            user_id="owner_001",
            role="owner",
            now=self.NOW - timedelta(hours=1),
        )

        result = self.store.mark_delivered(
            user_id="owner_001",
            reminder_id=created["id"],
            now=self.NOW,
        )

        self.assertFalse(result["changed"])
        self.assertEqual(result["status"], "pending")


if __name__ == "__main__":
    unittest.main()