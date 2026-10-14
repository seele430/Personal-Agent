
"""验证正式聊天 HTTP 接口的额度限制。

使用临时 SQLite 数据库和模拟模型。
不读取真实账户，不消耗 API 费用。
"""

import time
import unittest
import uuid

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from tests.helper import isolate_paths
from src import api
from src.auth_api import get_current_user
from src.core import chat_quota
from src.main import app


class TestChatQuotaHTTP(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()

        isolate_paths(self, self.temp)

        self.old_overrides = (
            app.dependency_overrides.copy()
        )

        # 模拟已认证用户身份。
        # 不使用真实 Token 和密码。
        def fake_user(request: Request) -> dict:
            token = request.headers.get(
                "authorization", ""
            )

            if token == "Bearer owner-test-token":
                return {
                    "id": "owner-test-id",
                    "role": "owner",
                    "username": "owner",
                    "active": True,
                }

            if token == "Bearer member-test-token":
                return {
                    "id": "member-test-id",
                    "role": "member",
                    "username": "member",
                    "active": True,
                }

            raise HTTPException(
                status_code=401,
                detail="请先登录",
            )

        app.dependency_overrides[
            get_current_user
        ] = fake_user

        self.client = TestClient(app)
        self.client.__enter__()

        # 只在测试期间将额度降低到两次。
        # 不改变实际运行配置。
        self.limit_patch = patch.dict(
            chat_quota.DAILY_REQUEST_LIMITS,
            {
                "member": 2,
                "owner": 2,
            },
        )
        self.limit_patch.start()

    def tearDown(self):
        self.limit_patch.stop()

        self.client.__exit__(
            None, None, None
        )

        app.dependency_overrides.clear()
        app.dependency_overrides.update(
            self.old_overrides
        )

        self.temp.cleanup()

    @staticmethod
    def new_id():
        return "quota_" + uuid.uuid4().hex

    def send(
        self,
        request_id,
        user="owner",
        message="测试消息",
    ):
        return self.client.post(
            "/chat",
            headers={
                "Authorization":
                    f"Bearer {user}-test-token"
            },
            json={
                "request_id": request_id,
                "message": message,
            },
        )

    # ======================================
    # 1. 超额返回 429，不调用第三次模型
    # ======================================

    def test_daily_quota_returns_429_without_model_cost(
        self,
    ):
        with patch.object(
            api,
            "chat",
            return_value="测试回复",
        ) as model:

            first = self.send(self.new_id())
            second = self.send(self.new_id())

            denied_id = self.new_id()
            third = self.send(denied_id)

        self.assertEqual(
            [
                first.status_code,
                second.status_code,
            ],
            [200, 200],
        )

        self.assertEqual(
            third.status_code,
            429,
        )

        self.assertEqual(
            third.json()["status"],
            "rejected",
        )

        self.assertEqual(
            third.json()["code"],
            "daily_quota_exceeded",
        )

        self.assertEqual(
            third.headers["cache-control"],
            "no-store",
        )

        self.assertEqual(
            model.call_count,
            2,
        )

        # 被拒绝的请求没有插入记录
        with closing(api.connect_db()) as conn:
            row = conn.execute(
                """
                SELECT request_id
                FROM chat_requests
                WHERE request_id = ?
                """,
                (denied_id,),
            ).fetchone()

        self.assertIsNone(row)

    # ======================================
    # 2. 额度用完后重复原请求仍返回缓存
    # ======================================

    def test_duplicate_after_limit_returns_cached_result(
        self,
    ):
        first_id = self.new_id()

        with patch.object(
            api,
            "chat",
            return_value="缓存结果",
        ) as model:

            first = self.send(first_id)

            self.send(self.new_id())

            repeat = self.send(first_id)

        self.assertEqual(
            first.status_code,
            200,
        )

        self.assertEqual(
            repeat.status_code,
            200,
        )

        self.assertEqual(
            first.json(),
            repeat.json(),
        )

        self.assertEqual(
            model.call_count,
            2,
        )

    # ======================================
    # 3. 不同用户分别计算额度
    # ======================================

    def test_users_have_separate_daily_limits(
        self,
    ):
        with patch.object(
            api,
            "chat",
            return_value="模拟回复",
        ) as model:

            first = self.send(
                self.new_id(),
                "owner",
            )

            second = self.send(
                self.new_id(),
                "owner",
            )

            member = self.send(
                self.new_id(),
                "member",
            )

            rejected = self.send(
                self.new_id(),
                "owner",
            )

        self.assertEqual(
            first.status_code,
            200,
        )

        self.assertEqual(
            second.status_code,
            200,
        )

        self.assertEqual(
            member.status_code,
            200,
        )

        self.assertEqual(
            rejected.status_code,
            429,
        )

        self.assertEqual(
            model.call_count,
            3,
        )

    # ======================================
    # 4. 模型超时仍计入已领取请求
    # ======================================

    def test_unknown_result_still_counts_toward_daily_limit(
        self,
    ):
        with patch.object(
            api,
            "chat",
            side_effect=[
                TimeoutError("模拟超时"),
                "成功",
            ],
        ) as model:

            first = self.send(self.new_id())
            second = self.send(self.new_id())
            third = self.send(self.new_id())

        self.assertEqual(
            first.status_code,
            502,
        )

        self.assertEqual(
            second.status_code,
            200,
        )

        self.assertEqual(
            third.status_code,
            429,
        )

        self.assertEqual(
            model.call_count,
            2,
        )

    # ======================================
    # 5. 并发限制：第三次请求不能调用模型
    # ======================================

    def test_concurrent_requests_hit_user_limit_before_model(
        self,
    ):
        # 暂时提高日次数，只测试并发限制。
        with patch.dict(
            chat_quota.DAILY_REQUEST_LIMITS,
            {"owner": 10},
        ):
            release = Event()
            started = Event()

            def blocking_chat(_message):
                started.set()

                if not release.wait(timeout=8):
                    raise TimeoutError(
                        "模拟等待超时"
                    )

                return "finished"

            with patch.object(
                api,
                "chat",
                side_effect=blocking_chat,
            ) as model:

                with ThreadPoolExecutor(
                    max_workers=3
                ) as executor:

                    first = executor.submit(
                        self.send,
                        self.new_id(),
                    )

                    second = executor.submit(
                        self.send,
                        self.new_id(),
                    )

                    try:
                        self.assertTrue(
                            started.wait(timeout=5)
                        )

                        deadline = (
                            time.monotonic() + 5
                        )

                        active = 0

                        # 等待两个请求都进入 processing
                        while (
                            time.monotonic() < deadline
                        ):
                            with closing(
                                api.connect_db()
                            ) as conn:

                                active = conn.execute(
                                    """
                                    SELECT COUNT(*)
                                    FROM chat_requests
                                    WHERE status = 'processing'
                                    """
                                ).fetchone()[0]

                            if active == 2:
                                break

                            time.sleep(0.025)

                        self.assertEqual(active, 2)

                        denied_id = self.new_id()

                        denied = self.send(
                            denied_id
                        )

                        self.assertEqual(
                            denied.status_code,
                            429,
                        )

                        self.assertEqual(
                            denied.json()["code"],
                            "concurrency_limit_exceeded",
                        )

                    finally:
                        # 即使断言失败，也放行等待中的线程
                        release.set()

                    self.assertEqual(
                        first.result(
                            timeout=8
                        ).status_code,
                        200,
                    )

                    self.assertEqual(
                        second.result(
                            timeout=8
                        ).status_code,
                        200,
                    )

                self.assertEqual(
                    model.call_count,
                    2,
                )


class TestUsageSummaryHTTP(unittest.TestCase):
    """只读额度接口：身份、数字与缓存头。"""

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        isolate_paths(self, self.temp)

        self.old_overrides = (
            app.dependency_overrides.copy()
        )

        self.addCleanup(self.restore_overrides)

        def fake_user(request: Request) -> dict:
            token = request.headers.get(
                "authorization", ""
            )

            if token == "Bearer member-test-token":
                return {
                    "id": "member-test-id",
                    "role": "member",
                    "username": "member",
                    "active": True,
                }

            # 角色不在额度表里：接口必须拒绝，
            # 而不是猜一个上限。
            if token == "Bearer ghost-test-token":
                return {
                    "id": "ghost-test-id",
                    "role": "ghost",
                    "username": "ghost",
                    "active": True,
                }

            raise HTTPException(
                status_code=401,
                detail="请先登录",
            )

        app.dependency_overrides[
            get_current_user
        ] = fake_user

        self.client = TestClient(app)
        self.client.__enter__()

        self.addCleanup(
            lambda: self.client.__exit__(
                None, None, None
            )
        )

    def restore_overrides(self):
        app.dependency_overrides.clear()
        app.dependency_overrides.update(
            self.old_overrides
        )

    def headers(self, token="member-test-token"):
        return {
            "Authorization": f"Bearer {token}",
        }

    def add_request(self, *, user_id, when):
        with closing(api.connect_db()) as conn, conn:
            conn.execute(
                """
                INSERT INTO chat_requests (
                    request_id,
                    message_hash,
                    status,
                    reply,
                    created_at,
                    updated_at,
                    user_id
                )
                VALUES (?, 'hash', 'completed', 'ok', ?, ?, ?)
                """,
                (
                    f"req_{user_id}_{when.timestamp()}",
                    when.isoformat(),
                    when.isoformat(),
                    user_id,
                ),
            )

    # 1. 未登录不能读额度

    def test_summary_requires_login(self):
        result = self.client.get("/usage/summary")

        self.assertEqual(result.status_code, 401)

    # 2. 空账本返回角色上限，且不允许缓存

    def test_summary_starts_empty_with_role_limits(self):
        result = self.client.get(
            "/usage/summary",
            headers=self.headers(),
        )

        self.assertEqual(result.status_code, 200)

        self.assertEqual(
            result.headers["cache-control"],
            "no-store",
        )

        body = result.json()

        self.assertEqual(body["window_hours"], 24)
        self.assertEqual(body["role"], "member")

        self.assertEqual(
            body["requests"],
            {
                "used": 0,
                "limit": 10,
                "remaining": 10,
            },
        )

        self.assertEqual(
            body["budget"]["limit_usd"],
            "0.075",
        )
        self.assertEqual(
            body["budget"]["used_usd"],
            "0",
        )

    # 3. 只统计自己的、最近 24 小时的请求

    def test_summary_counts_recent_own_requests(self):
        now = datetime.now(timezone.utc)

        self.add_request(
            user_id="member-test-id",
            when=now - timedelta(hours=1),
        )
        self.add_request(
            user_id="member-test-id",
            when=now - timedelta(hours=2),
        )
        self.add_request(
            user_id="member-test-id",
            when=now - timedelta(hours=30),
        )
        self.add_request(
            user_id="someone-else",
            when=now - timedelta(hours=1),
        )

        body = self.client.get(
            "/usage/summary",
            headers=self.headers(),
        ).json()

        self.assertEqual(body["requests"]["used"], 2)
        self.assertEqual(
            body["requests"]["remaining"],
            8,
        )

    # 4. 角色不在额度表里时明确拒绝

    def test_unknown_role_is_rejected(self):
        result = self.client.get(
            "/usage/summary",
            headers=self.headers("ghost-test-token"),
        )

        self.assertEqual(result.status_code, 403)


if __name__ == "__main__":
    unittest.main()