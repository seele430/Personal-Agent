"""个人站内提醒 HTTP 查询测试。

只使用临时数据库和模拟身份。
不调用真实模型，不发送通知。
"""

import unittest

from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from tests.helper import isolate_paths
from src.auth_api import get_current_user
from src.core import reminder_identity
from src.core.reminder_store import ReminderStore
from src.reminder_api import router


class TestReminderAPI(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        temporary = isolate_paths(self, self.temp)
        self.db_path = temporary.reminders_db

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


class TestReminderManagementAPI(unittest.TestCase):
    """提醒的列表、取消、修改和确认送达接口。"""

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        temporary = isolate_paths(self, self.temp)
        self.db_path = temporary.reminders_db

        self.store = ReminderStore(self.db_path)

        self.app = FastAPI()
        self.app.include_router(router)

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

    def headers(self, token="alice-token"):
        return {
            "Authorization": f"Bearer {token}",
        }

    def seed(
        self,
        *,
        user_id="alice",
        request_id="alice_request_001",
        message="喝水",
        minutes=5,
        role="member",
        now=None,
    ):
        return self.store.create(
            user_id=user_id,
            request_id=request_id,
            role=role,
            message=message,
            minutes=minutes,
            now=(
                datetime.now(timezone.utc)
                if now is None
                else now
            ),
        )

    # 1. 列表只返回自己的提醒。

    def test_list_returns_only_own_reminders(self):
        self.seed()
        self.seed(
            user_id="bob",
            request_id="bob_request_001",
            message="别人的提醒",
        )

        result = self.client.get(
            "/reminders",
            headers=self.headers(),
        )

        self.assertEqual(result.status_code, 200)

        reminders = result.json()["reminders"]

        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0]["message"], "喝水")
        self.assertEqual(reminders[0]["status"], "pending")

    # 2. 未登录不能读取。

    def test_list_requires_authentication(self):
        result = self.client.get("/reminders")

        self.assertEqual(result.status_code, 401)

    # 3. 非法状态过滤值返回 422。

    def test_list_rejects_invalid_status_filter(self):
        result = self.client.get(
            "/reminders",
            headers=self.headers(),
            params={"status": "unknown"},
        )

        self.assertEqual(result.status_code, 422)

    # 4. 私人提醒不能被缓存。

    def test_list_is_not_cached(self):
        result = self.client.get(
            "/reminders",
            headers=self.headers(),
        )

        self.assertEqual(
            result.headers["cache-control"],
            "no-store",
        )

    # 5. 取消自己的提醒。

    def test_cancel_own_reminder(self):
        created = self.seed()

        result = self.client.delete(
            f"/reminders/{created['id']}",
            headers=self.headers(),
        )

        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()["changed"])
        self.assertEqual(
            result.json()["status"],
            "cancelled",
        )

        listed = self.client.get(
            "/reminders",
            headers=self.headers(),
        ).json()["reminders"]

        self.assertEqual(listed[0]["status"], "cancelled")

    # 6. 不能操作别人的提醒，且与不存在同样返回 404。

    def test_cannot_touch_other_users_reminder(self):
        created = self.seed()

        result = self.client.delete(
            f"/reminders/{created['id']}",
            headers=self.headers("bob-token"),
        )

        self.assertEqual(result.status_code, 404)

        patched = self.client.patch(
            f"/reminders/{created['id']}",
            headers=self.headers("bob-token"),
            json={"message": "改别人的提醒"},
        )

        self.assertEqual(patched.status_code, 404)

        acked = self.client.post(
            f"/reminders/{created['id']}/ack",
            headers=self.headers("bob-token"),
        )

        self.assertEqual(acked.status_code, 404)

        # 站长身份同样不能越过归属校验。
        owner = self.client.delete(
            f"/reminders/{created['id']}",
            headers=self.headers("owner-token"),
        )

        self.assertEqual(owner.status_code, 404)

        # 原记录没有被改动。
        listed = self.client.get(
            "/reminders",
            headers=self.headers(),
        ).json()["reminders"]

        self.assertEqual(listed[0]["status"], "pending")

    # 7. 修改提醒内容。

    def test_patch_updates_message(self):
        created = self.seed()

        result = self.client.patch(
            f"/reminders/{created['id']}",
            headers=self.headers(),
            json={"message": "改成喝水"},
        )

        self.assertEqual(result.status_code, 200)
        self.assertEqual(
            result.json()["message"],
            "改成喝水",
        )

        listed = self.client.get(
            "/reminders",
            headers=self.headers(),
        ).json()["reminders"]

        self.assertEqual(
            listed[0]["message"],
            "改成喝水",
        )

    # 8. 一个字段都不传时返回 422。

    def test_patch_requires_a_field(self):
        created = self.seed()

        result = self.client.patch(
            f"/reminders/{created['id']}",
            headers=self.headers(),
            json={},
        )

        self.assertEqual(result.status_code, 422)

    # 9. 已取消的提醒不能再修改。

    def test_patch_on_cancelled_reminder_returns_409(self):
        created = self.seed()

        self.client.delete(
            f"/reminders/{created['id']}",
            headers=self.headers(),
        )

        result = self.client.patch(
            f"/reminders/{created['id']}",
            headers=self.headers(),
            json={"message": "改不动的提醒"},
        )

        self.assertEqual(result.status_code, 409)

    # 10. 确认送达之后，到期列表不再重复返回。

    def test_ack_removes_reminder_from_due_list(self):
        created = self.seed(
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
            before = self.client.get(
                "/reminders/in-app/due",
                headers=self.headers(),
            )

            acked = self.client.post(
                f"/reminders/{created['id']}/ack",
                headers=self.headers(),
            )

            after = self.client.get(
                "/reminders/in-app/due",
                headers=self.headers(),
            )

        self.assertEqual(
            len(before.json()["reminders"]),
            1,
        )

        self.assertEqual(acked.status_code, 200)
        self.assertTrue(acked.json()["changed"])

        self.assertEqual(
            after.json()["reminders"],
            [],
        )

    # 11. 未到期的提醒不能被提前确认。

    def test_ack_future_reminder_is_not_changed(self):
        created = self.seed(minutes=60)

        result = self.client.post(
            f"/reminders/{created['id']}/ack",
            headers=self.headers(),
        )

        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json()["changed"])
        self.assertEqual(
            result.json()["status"],
            "pending",
        )

    # 12. 路径参数类型不对时由 FastAPI 拒绝。

    def test_invalid_reminder_id_is_rejected(self):
        for path in (
            "/reminders/abc",
            "/reminders/0",
            "/reminders/-1",
        ):
            result = self.client.delete(
                path,
                headers=self.headers(),
            )

            self.assertEqual(result.status_code, 422)


if __name__ == "__main__":
    unittest.main()