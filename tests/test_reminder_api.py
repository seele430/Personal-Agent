"""个人站内提醒 HTTP 查询测试。

只使用临时数据库和模拟身份。
不调用真实模型，不发送通知。
"""

import unittest

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from src.auth_api import get_current_user
from src.core import reminder_identity
from src.core.reminder_store import ReminderStore
from src.reminder_api import router


class TestReminderAPI(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        self.db_path = (
            Path(self.temp.name) / "reminders.db"
        )

        db_patch = patch.object(
            reminder_identity,
            "REMINDER_DB_PATH",
            self.db_path,
        )
        db_patch.start()
        self.addCleanup(db_patch.stop)

        self.store = ReminderStore(self.db_path)

        self.app = FastAPI()
        self.app.include_router(router)

        # 模拟后端已经验证的登录会话。
        def fake_user(request: Request):
            token = request.headers.get(
                "authorization",
                "",
            )

            users = {
                "Bearer alice-token": {
                    "id": "alice",
                    "role": "member",
                },
                "Bearer bob-token": {
                    "id": "bob",
                    "role": "member",
                },
                "Bearer owner-token": {
                    "id": "owner_001",
                    "role": "owner",
                },
            }

            if token not in users:
                raise HTTPException(
                    status_code=401,
                    detail="请先登录",
                )

            return users[token]

        self.app.dependency_overrides[
            get_current_user
        ] = fake_user

        self.client = TestClient(self.app)
        self.client.__enter__()

        self.addCleanup(
            lambda: self.client.__exit__(
                None, None, None
            )
        )

    def get_reminders(
        self,
        token="alice-token",
        params=None,
    ):
        return self.client.get(
            "/reminders/in-app/due",
            headers={
                "Authorization": f"Bearer {token}",
            },
            params=params,
        )

    def seed_reminder(
        self,
        *,
        user_id,
        request_id,
        message,
        now,
        role="member",
    ):
        return self.store.create(
            user_id=user_id,
            request_id=request_id,
            role=role,
            message=message,
            minutes=5,
            now=now,
        )

    # 1. 功能未开放时，不能返回私人提醒。

    def test_disabled_feature_returns_empty(self):
        with patch.object(
            reminder_identity,
            "IN_APP_DELIVERY_ENABLED",
            False,
        ):
            result = self.get_reminders()

        self.assertEqual(result.status_code, 200)

        self.assertEqual(
            result.json(),
            {
                "enabled": False,
                "reminders": [],
            },
        )

        self.assertEqual(
            result.headers["cache-control"],
            "no-store",
        )

        # 功能关闭时不会创建数据库。
        self.assertFalse(self.db_path.exists())

    # 2. 未登录不能查询提醒。

    def test_unauthenticated_is_rejected(self):
        result = self.get_reminders(
            token="invalid-token",
        )

        self.assertEqual(result.status_code, 401)

    # 3. 第一次启用时，可以安全返回空列表。

    def test_first_enabled_visit_is_empty(self):
        with patch.object(
            reminder_identity,
            "IN_APP_DELIVERY_ENABLED",
            True,
        ):
            result = self.get_reminders()

        self.assertEqual(result.status_code, 200)

        self.assertEqual(
            result.json(),
            {
                "enabled": True,
                "reminders": [],
            },
        )

        self.assertTrue(self.db_path.is_file())

    # 4. 只返回当前用户已到期的提醒。

    def test_only_due_reminders_are_returned(self):
        now = datetime.now(timezone.utc)

        self.seed_reminder(
            user_id="alice",
            request_id="alice_due_001",
            message="已经到期的提醒",
            now=now - timedelta(hours=1),
        )

        self.seed_reminder(
            user_id="alice",
            request_id="alice_future_001",
            message="未来的提醒",
            now=now + timedelta(hours=1),
        )

        with patch.object(
            reminder_identity,
            "IN_APP_DELIVERY_ENABLED",
            True,
        ):
            result = self.get_reminders()

        self.assertEqual(result.status_code, 200)

        reminders = result.json()["reminders"]

        self.assertEqual(len(reminders), 1)

        self.assertEqual(
            reminders[0]["message"],
            "已经到期的提醒",
        )

    # 5. 不能通过 URL 参数读取别人的提醒。

    def test_users_cannot_read_each_others_data(self):
        past = (
            datetime.now(timezone.utc)
            - timedelta(hours=1)
        )

        self.seed_reminder(
            user_id="alice",
            request_id="alice_secret_001",
            message="Alice 的私人提醒",
            now=past,
        )

        self.seed_reminder(
            user_id="bob",
            request_id="bob_secret_001",
            message="Bob 的私人提醒",
            now=past,
        )

        with patch.object(
            reminder_identity,
            "IN_APP_DELIVERY_ENABLED",
            True,
        ):
            result = self.get_reminders(
                token="bob-token",
                params={"user_id": "alice"},
            )

        self.assertEqual(result.status_code, 200)

        reminders = result.json()["reminders"]

        self.assertEqual(len(reminders), 1)

        self.assertEqual(
            reminders[0]["message"],
            "Bob 的私人提醒",
        )

    # 6. 站长的 ntfy 提醒不会出现在站内列表。

    def test_owner_ntfy_is_not_in_app_reminder(self):
        self.seed_reminder(
            user_id="owner_001",
            request_id="owner_ntfy_001",
            message="站长 ntfy 提醒",
            role="owner",
            now=(
                datetime.now(timezone.utc)
                - timedelta(hours=1)
            ),
        )

        with patch.object(
            reminder_identity,
            "IN_APP_DELIVERY_ENABLED",
            True,
        ):
            result = self.get_reminders(
                token="owner-token",
            )

        self.assertEqual(result.status_code, 200)

        self.assertEqual(
            result.json()["reminders"],
            [],
        )


if __name__ == "__main__":
    unittest.main()