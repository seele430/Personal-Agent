"""正式聊天 HTTP 预算准入测试。

使用临时数据库、模拟认证及模拟 AI。
不会访问真实账户，也不会调用付费模型。
"""

import sqlite3
import unittest
import uuid

from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from src import agent
from src import api
from src.auth_api import get_current_user
from src.core import chat_admission
from src.main import app


class TestChatBudgetHTTP(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()

        self.chat_path = (
            Path(self.temp.name) / "chat_test.db"
        )

        self.db_patch = patch.object(
            api,
            "CHAT_DB_PATH",
            self.chat_path,
        )
        self.db_patch.start()

        self.old_overrides = (
            app.dependency_overrides.copy()
        )

        def fake_user(request: Request):
            authorization = request.headers.get(
                "authorization",
                "",
            )

            if authorization != "Bearer test-token":
                raise HTTPException(
                    status_code=401,
                    detail="请先登录",
                )

            return {
                "id": "budget-test-user",
                "username": "testowner",
                "role": "owner",
                "active": True,
            }

        app.dependency_overrides[
            get_current_user
        ] = fake_user

        self.client = TestClient(app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(
            None, None, None,
        )

        app.dependency_overrides.clear()
        app.dependency_overrides.update(
            self.old_overrides
        )

        self.db_patch.stop()
        self.temp.cleanup()

    @staticmethod
    def new_id():
        return "budget_" + uuid.uuid4().hex

    def send(self, request_id, message="测试消息"):
        return self.client.post(
            "/chat",
            headers={
                "Authorization": "Bearer test-token",
            },
            json={
                "request_id": request_id,
                "message": message,
            },
        )

    def db_row(self, request_id):
        with closing(api.connect_db()) as conn:
            return conn.execute(
                """
                SELECT
                    c.status,
                    b.reserved_nano_usd,
                    b.charged_nano_usd,
                    b.state
                FROM chat_requests AS c
                LEFT JOIN ai_budget_reservations AS b
                  ON b.request_id = c.request_id
                WHERE c.request_id = ?
                """,
                (request_id,),
            ).fetchone()

    def record_counts(self):
        with closing(api.connect_db()) as conn:
            chats = conn.execute(
                "SELECT COUNT(*) FROM chat_requests"
            ).fetchone()[0]

            budgets = conn.execute(
                """
                SELECT COUNT(*)
                FROM ai_budget_reservations
                """
            ).fetchone()[0]

        return chats, budgets

    # ======================================
    # 1. 成功请求同时建立聊天和预算记录
    # ======================================

    def test_success_creates_budget_reservation(self):
        request_id = self.new_id()

        with patch.object(
            api,
            "chat",
            return_value="模拟回复",
        ) as model:
            response = self.send(request_id)

        self.assertEqual(response.status_code, 200)
        model.assert_called_once()

        row = self.db_row(request_id)

        self.assertEqual(row[0], "completed")
        self.assertEqual(row[1], 50_000_000)

        # 实际费用尚未核实，保留全部预留额度。
        self.assertEqual(row[2], 50_000_000)
        self.assertEqual(row[3], "uncertain")

        self.assertEqual(self.record_counts(), (1, 1))

    # ======================================
    # 2. 重复请求不重复调用模型或预留预算
    # ======================================

    def test_duplicate_does_not_reserve_twice(self):
        request_id = self.new_id()

        with patch.object(
            api,
            "chat",
            return_value="缓存回复",
        ) as model:
            first = self.send(request_id)
            repeat = self.send(request_id)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(repeat.status_code, 200)

        self.assertEqual(
            first.json(),
            repeat.json(),
        )

        model.assert_called_once()
        self.assertEqual(self.record_counts(), (1, 1))

    # ======================================
    # 3. 全站 US$1 预算拒绝第 21 条新请求
    # ======================================

    def test_global_budget_blocks_twenty_first_request(
        self,
    ):
        # 假模型不返回 usage，每次锁定 US$0.05。
        # 临时把 owner 个人上限调到全站预算的 2 倍，
        # 确保拦下第 21 次的是全站预算，而不是个人预算。
        relaxed_owner = {
            "owner": 2 * chat_admission.GLOBAL_BUDGET_UNITS,
        }

        with patch.dict(
            chat_admission.USER_BUDGET_LIMIT_UNITS,
            relaxed_owner,
        ), patch.object(
            api,
            "chat",
            return_value="模拟回复",
        ) as model:

            for _ in range(20):
                accepted = self.send(self.new_id())

                self.assertEqual(
                    accepted.status_code,
                    200,
                )

            denied_id = self.new_id()
            denied = self.send(denied_id)

        self.assertEqual(denied.status_code, 429)

        self.assertEqual(
            denied.json()["status"],
            "rejected",
        )

        self.assertEqual(
            denied.json()["code"],
            "global_budget_exceeded",
        )

        self.assertEqual(model.call_count, 20)
        self.assertEqual(self.record_counts(), (20, 20))

        # 被拒绝的请求没有占用聊天记录。
        self.assertIsNone(self.db_row(denied_id))

    # ======================================
    # 4. 未核实价格的模型不能产生调用
    # ======================================

    def test_unknown_model_is_rejected_before_call(
        self,
    ):
        with patch.object(
            agent,
            "MODEL",
            "unverified-model",
        ):
            with patch.object(api, "chat") as model:
                response = self.send(self.new_id())

        self.assertEqual(response.status_code, 429)
        self.assertEqual(
            response.json()["code"],
            "pricing_unavailable",
        )

        model.assert_not_called()
        self.assertEqual(self.record_counts(), (0, 0))

    # ======================================
    # 5. 输入过长时不能占用预算
    # ======================================

    def test_input_validation_precedes_reservation(
        self,
    ):
        # 临时降低测试上限，无需发送巨大消息。
        with patch.object(
            agent,
            "MAX_USER_MESSAGE_BYTES",
            3,
        ):
            with patch.object(api, "chat") as model:
                response = self.send(
                    self.new_id(),
                    message="abcd",
                )

        self.assertEqual(response.status_code, 413)

        model.assert_not_called()
        self.assertEqual(self.record_counts(), (0, 0))

    # ======================================
    # 6. 模型超时不能退还预留预算
    # ======================================

    def test_timeout_keeps_reserved_budget(self):
        request_id = self.new_id()

        with patch.object(
            api,
            "chat",
            side_effect=TimeoutError(
                "模拟网络超时"
            ),
        ) as model:

            with patch.object(
                api.logger,
                "exception",
            ):
                response = self.send(request_id)

        self.assertEqual(response.status_code, 502)

        self.assertEqual(
            response.json()["status"],
            "unknown",
        )

        model.assert_called_once()

        row = self.db_row(request_id)

        self.assertEqual(row[0], "unknown")
        self.assertEqual(row[1], 50_000_000)
        self.assertEqual(row[2], 50_000_000)
        self.assertEqual(row[3], "uncertain")


    # ======================================
    # 7. 个人预算不足返回 user_budget_exceeded
    # ======================================

    def test_user_budget_returns_distinct_code(self):
        # owner 个人上限临时设为正好一次预留：
        # 第 1 次通过，第 2 次被个人预算拦下。
        one_reserve = {
            "owner": chat_admission.RESERVE_UNITS,
        }

        with patch.dict(
            chat_admission.USER_BUDGET_LIMIT_UNITS,
            one_reserve,
        ), patch.object(
            api,
            "chat",
            return_value="模拟回复",
        ) as model:

            accepted = self.send(self.new_id())
            self.assertEqual(accepted.status_code, 200)

            denied_id = self.new_id()
            denied = self.send(denied_id)

        self.assertEqual(denied.status_code, 429)

        self.assertEqual(
            denied.json()["status"],
            "rejected",
        )

        self.assertEqual(
            denied.json()["code"],
            "user_budget_exceeded",
        )

        self.assertEqual(model.call_count, 1)
        self.assertEqual(self.record_counts(), (1, 1))
        self.assertIsNone(self.db_row(denied_id))

if __name__ == "__main__":
    unittest.main()