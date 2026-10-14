"""已认证提醒身份及通知渠道测试。

全部使用临时 reminders.db。
不调用模型，不发送 ntfy，不修改真实提醒。
"""

import sqlite3
import unittest

from contextlib import closing
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.helper import isolate_paths
from src.core import reminder_identity
from src.core.reminder_identity import (
    ReminderDeliveryUnavailable,
    bind_authenticated_reminder,
    cancel_reminder_for_current_user,
    create_reminder_for_current_user,
    list_reminders_for_current_user,
    update_reminder_for_current_user,
)
from src.core.reminder_store import (
    ReminderConflict,
    ReminderStore,
)


class TestReminderIdentity(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()

        temporary = isolate_paths(self, self.temp)
        self.path = temporary.reminders_db

        self.delivery_patch = patch.object(
            reminder_identity,
            "IN_APP_DELIVERY_ENABLED",
            False,
        )
        self.delivery_patch.start()

        ReminderStore(self.path).init_db()

    def tearDown(self):
        self.delivery_patch.stop()
        self.temp.cleanup()

    def counts(self):
        with closing(
            sqlite3.connect(self.path)
        ) as conn:

            old_count = conn.execute(
                "SELECT COUNT(*) FROM reminders"
            ).fetchone()[0]

            new_count = conn.execute(
                "SELECT COUNT(*) FROM reminder_requests"
            ).fetchone()[0]

        return old_count, new_count

    # 1. 未认证调用绝不能创建提醒

    def test_anonymous_creation_is_rejected(self):
        with self.assertRaises(PermissionError):
            create_reminder_for_current_user(
                "喝水",
                5,
            )

        self.assertEqual(self.counts(), (0, 0))

    # 2. 站长继续使用原有 ntfy 表

    def test_owner_creates_ntfy_reminder(self):
        with bind_authenticated_reminder(
            user_id="owner_001",
            request_id="request_owner_001",
            role="owner",
        ):
            result = create_reminder_for_current_user(
                "喝水",
                5,
            )

        self.assertTrue(result["created"])
        self.assertEqual(
            result["delivery"],
            "ntfy",
        )
        self.assertIsNotNone(
            result["legacy_reminder_id"]
        )

        self.assertEqual(self.counts(), (1, 1))

    # 3. 通知中心未完成时成员不能创建提醒

    def test_member_blocked_before_notification_center(
        self,
    ):
        with bind_authenticated_reminder(
            user_id="member_001",
            request_id="request_member_001",
            role="member",
        ):
            with self.assertRaises(
                ReminderDeliveryUnavailable
            ):
                create_reminder_for_current_user(
                    "休息",
                    5,
                )

        self.assertEqual(self.counts(), (0, 0))

    # 4. 模拟通知中心已上线：成员只写站内表

    def test_member_delivery_never_uses_owner_ntfy(
        self,
    ):
        with patch.object(
            reminder_identity,
            "IN_APP_DELIVERY_ENABLED",
            True,
        ):
            with bind_authenticated_reminder(
                user_id="member_001",
                request_id="request_member_002",
                role="member",
            ):
                result = create_reminder_for_current_user(
                    "休息",
                    5,
                )

        self.assertEqual(
            result["delivery"],
            "in_app",
        )
        self.assertIsNone(
            result["legacy_reminder_id"]
        )

        self.assertEqual(self.counts(), (0, 1))

    # 5. 同一请求重复执行不会创建第二条

    def test_duplicate_request_is_idempotent(self):
        with bind_authenticated_reminder(
            user_id="owner_001",
            request_id="request_owner_002",
            role="owner",
        ):
            first = create_reminder_for_current_user(
                "喝水",
                5,
            )

            second = create_reminder_for_current_user(
                "喝水",
                5,
            )

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])

        self.assertEqual(
            first["id"],
            second["id"],
        )

        self.assertEqual(self.counts(), (1, 1))

    # 6. 其他用户不能冒领已有提醒请求

    def test_other_user_cannot_reuse_request(self):
        with bind_authenticated_reminder(
            user_id="owner_001",
            request_id="request_shared_001",
            role="owner",
        ):
            create_reminder_for_current_user(
                "喝水",
                5,
            )

        with bind_authenticated_reminder(
            user_id="owner_002",
            request_id="request_shared_001",
            role="owner",
        ):
            with self.assertRaises(ReminderConflict):
                create_reminder_for_current_user(
                    "喝水",
                    5,
                )

        self.assertEqual(self.counts(), (1, 1))

    # 7. 上下文退出后必须清除登录身份

    def test_identity_is_cleared_after_exit(self):
        with bind_authenticated_reminder(
            user_id="owner_001",
            request_id="request_owner_003",
            role="owner",
        ):
            create_reminder_for_current_user(
                "喝水",
                5,
            )

        with self.assertRaises(PermissionError):
            create_reminder_for_current_user(
                "其他提醒",
                5,
            )

        self.assertEqual(self.counts(), (1, 1))

    # 8. 无效身份和请求 ID 不允许绑定

    def test_invalid_identity_rejected(self):
        with self.assertRaises(ValueError):
            with bind_authenticated_reminder(
                user_id="",
                request_id="request_valid_001",
                role="owner",
            ):
                pass

        with self.assertRaises(ValueError):
            with bind_authenticated_reminder(
                user_id="owner_001",
                request_id="short",
                role="owner",
            ):
                pass

        with self.assertRaises(ValueError):
            with bind_authenticated_reminder(
                user_id="owner_001",
                request_id="request_valid_001",
                role="unknown",
            ):
                pass

        self.assertEqual(self.counts(), (0, 0))


    # 9. 没有登录身份时，读取 / 取消 / 修改一律被拒绝

    def test_management_requires_identity(self):
        with self.assertRaises(PermissionError):
            list_reminders_for_current_user()

        with self.assertRaises(PermissionError):
            cancel_reminder_for_current_user(1)

        with self.assertRaises(PermissionError):
            update_reminder_for_current_user(
                1,
                message="新内容",
            )

    # 10. 列表只包含自己的提醒

    def test_list_only_returns_own(self):
        with patch.object(
            reminder_identity,
            "IN_APP_DELIVERY_ENABLED",
            True,
        ):
            with bind_authenticated_reminder(
                user_id="member_a",
                request_id="request_member_a_001",
                role="member",
            ):
                create_reminder_for_current_user(
                    "A 的提醒",
                    5,
                )

            with bind_authenticated_reminder(
                user_id="member_b",
                request_id="request_member_b_001",
                role="member",
            ):
                create_reminder_for_current_user(
                    "B 的提醒",
                    5,
                )

                mine = (
                    list_reminders_for_current_user()
                )

        self.assertEqual(
            [item["message"] for item in mine],
            ["B 的提醒"],
        )

    # 11. 不能取消别人的提醒

    def test_cannot_cancel_other_users_reminder(self):
        with patch.object(
            reminder_identity,
            "IN_APP_DELIVERY_ENABLED",
            True,
        ):
            with bind_authenticated_reminder(
                user_id="member_a",
                request_id="request_member_a_002",
                role="member",
            ):
                created = create_reminder_for_current_user(
                    "A 的提醒",
                    5,
                )

            with bind_authenticated_reminder(
                user_id="member_b",
                request_id="request_member_b_002",
                role="member",
            ):
                with self.assertRaises(ValueError):
                    cancel_reminder_for_current_user(
                        created["id"]
                    )

                with self.assertRaises(ValueError):
                    update_reminder_for_current_user(
                        created["id"],
                        message="改别人的提醒",
                    )

        # A 的提醒没有被改动。
        self.assertEqual(
            self.counts(),
            (0, 1),
        )

        with closing(
            sqlite3.connect(self.path)
        ) as conn:
            status, message = conn.execute(
                """
                SELECT status, message
                FROM reminder_requests
                """
            ).fetchone()

        self.assertEqual(status, "pending")
        self.assertEqual(message, "A 的提醒")


if __name__ == "__main__":
    unittest.main()