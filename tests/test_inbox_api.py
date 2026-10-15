"""统一收件箱的聚合与确认路由测试。

使用临时数据库与模拟登录身份：
- 监控通知写在临时任务库；
- 到点提醒写在临时提醒库；
不联网、不推送。
"""

import sqlite3
import unittest

from contextlib import closing
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from tests.helper import isolate_paths
from src.auth_api import get_current_user
from src.core.reminder_store import ReminderStore
from src.core.task_engine import init_task_db
from src.inbox_api import router


class TestInboxAPI(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        temporary = isolate_paths(self, self.temp)

        self.tasks_db = temporary.tasks_db
        self.reminders_db = temporary.reminders_db

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

    # ---------- 造数据 ----------

    def queue_monitor_notification(
        self, *, owner_id, message
    ) -> int:
        with closing(
            sqlite3.connect(self.tasks_db)
        ) as conn, conn:
            cursor = conn.execute(
                """
                INSERT INTO notification_outbox
                (monitor_id, item_id, message,
                 status, owner_id)
                VALUES ('m1', ?, ?, 'in_app', ?)
                """,
                (f"item-{message}", message, owner_id),
            )

            return cursor.lastrowid

    def create_due_reminder(
        self, *, user_id, message, minutes_ago=10
    ) -> int:
        store = ReminderStore(self.reminders_db)

        # request_id 有 [A-Za-z0-9_-]{8,128} 的格式约束，
        # 所以这里不能用中文消息去拼。
        self._seq = getattr(self, "_seq", 0) + 1

        created = store.create(
            user_id=user_id,
            request_id=f"req_{user_id}_{self._seq:04d}",
            role="member",
            message=message,
            minutes=5,
            now=(
                datetime.now(timezone.utc)
                - timedelta(minutes=minutes_ago)
            ),
        )

        return created["id"]

    def items(self, token="alice-token"):
        return self.client.get(
            "/inbox",
            headers=self.headers(token),
        ).json()["items"]

    # ---------- 聚合 ----------

    def test_aggregates_both_sources(self):
        notification_id = self.queue_monitor_notification(
            owner_id="alice", message="监控有新内容"
        )
        reminder_id = self.create_due_reminder(
            user_id="alice", message="该喝水了"
        )

        response = self.client.get(
            "/inbox",
            headers=self.headers(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["cache-control"],
            "no-store",
        )

        items = response.json()["items"]

        self.assertEqual(len(items), 2)

        by_kind = {item["kind"]: item for item in items}

        self.assertEqual(
            set(by_kind), {"monitor", "reminder"}
        )

        self.assertEqual(
            by_kind["monitor"]["id"], notification_id
        )
        self.assertEqual(
            by_kind["monitor"]["message"], "监控有新内容"
        )
        self.assertEqual(
            by_kind["reminder"]["id"], reminder_id
        )

    def test_only_own_items_are_listed(self):
        self.queue_monitor_notification(
            owner_id="bob", message="别人的监控"
        )
        self.create_due_reminder(
            user_id="bob", message="别人的提醒"
        )

        self.assertEqual(self.items(), [])

    def test_works_before_any_reminder_exists(self):
        # 全新部署：一条提醒都没建过，提醒表可能还不存在。
        # 收件箱必须照常工作，而不是 500。
        self.queue_monitor_notification(
            owner_id="alice", message="只有监控通知"
        )

        items = self.items()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "monitor")

    def test_future_reminder_is_not_in_inbox(self):
        # 还没到期的提醒不算"待确认"
        ReminderStore(self.reminders_db).create(
            user_id="alice",
            request_id="req_future_001",
            role="member",
            message="以后再说",
            minutes=60,
            now=datetime.now(timezone.utc),
        )

        self.assertEqual(self.items(), [])

    # ---------- 确认 ----------

    def test_acknowledge_monitor_notification(self):
        notification_id = self.queue_monitor_notification(
            owner_id="alice", message="监控有新内容"
        )

        response = self.client.post(
            f"/inbox/monitor/{notification_id}/ack",
            headers=self.headers(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["changed"])
        self.assertEqual(
            response.json()["status"], "read"
        )

        self.assertEqual(self.items(), [])

    def test_acknowledge_reminder(self):
        reminder_id = self.create_due_reminder(
            user_id="alice", message="该喝水了"
        )

        response = self.client.post(
            f"/inbox/reminder/{reminder_id}/ack",
            headers=self.headers(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["changed"])
        self.assertEqual(
            response.json()["status"], "sent"
        )

        self.assertEqual(self.items(), [])

    def test_cannot_acknowledge_other_users_item(self):
        notification_id = self.queue_monitor_notification(
            owner_id="bob", message="别人的监控"
        )
        reminder_id = self.create_due_reminder(
            user_id="bob", message="别人的提醒"
        )

        for kind, item_id in (
            ("monitor", notification_id),
            ("reminder", reminder_id),
        ):
            with self.subTest(kind=kind):
                response = self.client.post(
                    f"/inbox/{kind}/{item_id}/ack",
                    headers=self.headers(),
                )

                self.assertEqual(
                    response.status_code, 404
                )

        # 别人的东西没有被改动
        self.assertEqual(self.items(), [])

    def test_unknown_item_returns_404(self):
        response = self.client.post(
            "/inbox/monitor/99999/ack",
            headers=self.headers(),
        )

        self.assertEqual(response.status_code, 404)

        response = self.client.post(
            "/inbox/reminder/99999/ack",
            headers=self.headers(),
        )

        self.assertEqual(response.status_code, 404)

    def test_unknown_kind_is_rejected(self):
        response = self.client.post(
            "/inbox/ghost/1/ack",
            headers=self.headers(),
        )

        self.assertEqual(response.status_code, 422)

    def test_requires_login(self):
        self.assertEqual(
            self.client.get("/inbox").status_code,
            401,
        )


if __name__ == "__main__":
    unittest.main()
