"""站内通知接口的归属隔离测试。

使用临时数据库与模拟登录身份，不联网、不发送真实推送。
"""

import sqlite3
import unittest

from contextlib import closing
from tempfile import TemporaryDirectory

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from tests.helper import isolate_paths
from src.auth_api import get_current_user
from src.core.task_engine import init_task_db
from src.notifications_api import router


class TestNotificationsAPI(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        temporary = isolate_paths(self, self.temp)
        self.db_path = temporary.tasks_db

        init_task_db()

        self.app = FastAPI()
        self.app.include_router(router)

        def fake_user(request: Request):
            token = request.headers.get(
                "authorization", ""
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
            "Authorization": f"Bearer {token}"
        }

    def queue(
        self,
        *,
        owner_id,
        message,
        status="in_app",
    ) -> int:
        with closing(
            sqlite3.connect(self.db_path)
        ) as conn, conn:
            cursor = conn.execute(
                """
                INSERT INTO notification_outbox
                (monitor_id, item_id, message,
                 status, owner_id)
                VALUES ('m1', ?, ?, ?, ?)
                """,
                (
                    f"item-{message}",
                    message,
                    status,
                    owner_id,
                ),
            )

            return cursor.lastrowid

    def stored_status(self, notification_id):
        with closing(
            sqlite3.connect(self.db_path)
        ) as conn:
            return conn.execute(
                """
                SELECT status FROM notification_outbox
                WHERE id = ?
                """,
                (notification_id,),
            ).fetchone()[0]

    # 1. 未登录不能读

    def test_requires_login(self):
        self.assertEqual(
            self.client.get("/notifications").status_code,
            401,
        )

    # 2. 只列出自己的、且只有待确认的

    def test_lists_only_own_pending_in_app(self):
        mine = self.queue(
            owner_id="alice", message="我的通知"
        )
        self.queue(owner_id="bob", message="别人的通知")

        # 已读的、还挂在 ntfy 队列里的，都不该出现在这里
        self.queue(
            owner_id="alice",
            message="已读的",
            status="read",
        )
        self.queue(
            owner_id="alice",
            message="待推送的",
            status="pending",
        )

        response = self.client.get(
            "/notifications",
            headers=self.headers(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["cache-control"],
            "no-store",
        )

        rows = response.json()["notifications"]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], mine)
        self.assertEqual(rows[0]["message"], "我的通知")

        # 不泄露内部字段
        self.assertNotIn("status", rows[0])

    # 3. 确认自己的通知

    def test_acknowledge_own_notification(self):
        notification_id = self.queue(
            owner_id="alice", message="待确认"
        )

        response = self.client.post(
            f"/notifications/{notification_id}/ack",
            headers=self.headers(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["changed"])
        self.assertEqual(
            response.json()["status"], "read"
        )
        self.assertEqual(
            self.stored_status(notification_id), "read"
        )

        # 确认后不再出现在列表里
        listed = self.client.get(
            "/notifications",
            headers=self.headers(),
        ).json()["notifications"]

        self.assertEqual(listed, [])

    # 4. 别人的通知：看不到也动不了

    def test_cannot_touch_other_users_notification(self):
        other = self.queue(
            owner_id="bob", message="别人的"
        )

        response = self.client.post(
            f"/notifications/{other}/ack",
            headers=self.headers(),
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            self.stored_status(other), "in_app"
        )

    # 5. 不存在的通知返回 404

    def test_unknown_notification_returns_404(self):
        response = self.client.post(
            "/notifications/99999/ack",
            headers=self.headers(),
        )

        self.assertEqual(response.status_code, 404)

    # 6. 非站内状态的通知不会被"确认"改写

    def test_ack_ignores_non_in_app_notification(self):
        notification_id = self.queue(
            owner_id="alice",
            message="待推送",
            status="pending",
        )

        response = self.client.post(
            f"/notifications/{notification_id}/ack",
            headers=self.headers(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["changed"])
        self.assertEqual(response.json()["status"], "pending")

        self.assertEqual(
            self.stored_status(notification_id),
            "pending",
        )

    # 7. 非法 ID 由 FastAPI 拒绝

    def test_invalid_id_is_rejected(self):
        for path in (
            "/notifications/abc/ack",
            "/notifications/0/ack",
            "/notifications/-1/ack",
        ):
            with self.subTest(path=path):
                response = self.client.post(
                    path,
                    headers=self.headers(),
                )

                self.assertEqual(
                    response.status_code, 422
                )


if __name__ == "__main__":
    unittest.main()
