
"""正式 HTTP 聊天接口的预算结算测试。

使用临时聊天数据库、临时用量数据库、
模拟认证和模拟模型响应。

不访问正式账户，不产生真实 API 费用。
"""

import unittest
import uuid

from contextlib import closing
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from tests.helper import isolate_paths
from src import agent
from src import api
from src.auth_api import get_current_user
from src.core.ai_usage import AIUsageStore
from src.main import app


class TestChatReconciliationHTTP(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()

        temporary = isolate_paths(self, self.temp)

        self.chat_path = temporary.chat_requests_db
        self.usage_path = temporary.ai_usage_db

        self.usage_store = AIUsageStore(
            self.usage_path
        )

        self.usage_store_patch = patch.object(
            agent,
            "_get_usage_store",
            return_value=self.usage_store,
        )
        self.usage_store_patch.start()

        self.reminder_init_patch = patch.object(
            agent,
            "init_db",
        )
        self.reminder_init_patch.start()

        self.old_overrides = (
            app.dependency_overrides.copy()
        )

        def fake_user(request: Request):
            if request.headers.get(
                "authorization"
            ) != "Bearer reconciliation-test-token":

                raise HTTPException(
                    status_code=401,
                    detail="请先登录",
                )

            return {
                "id": "reconciliation-test-user",
                "username": "testuser",
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
            None, None, None
        )

        app.dependency_overrides.clear()
        app.dependency_overrides.update(
            self.old_overrides
        )

        self.reminder_init_patch.stop()
        self.usage_store_patch.stop()

        self.temp.cleanup()

    @staticmethod
    def new_id():
        return "reconcile_" + uuid.uuid4().hex

    @staticmethod
    def response(
        text="模拟 AI 回复",
        *,
        tool_calls=None,
        finish_reason="stop",
        usage=True,
    ):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason=finish_reason,
                    message=SimpleNamespace(
                        content=text,
                        tool_calls=tool_calls,
                    ),
                ),
            ],
            usage=(
                SimpleNamespace(
                    prompt_tokens=1000,
                    completion_tokens=500,
                    total_tokens=1500,
                )
                if usage
                else None
            ),
        )

    def send(
        self,
        request_id,
        message="测试消息",
    ):
        return self.client.post(
            "/chat",
            headers={
                "Authorization":
                    "Bearer reconciliation-test-token",
            },
            json={
                "request_id": request_id,
                "message": message,
            },
        )

    def budget_row(self, request_id):
        with closing(api.connect_db()) as conn:
            return conn.execute(
                """
                SELECT
                    state,
                    reserved_nano_usd,
                    charged_nano_usd
                FROM ai_budget_reservations
                WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()

    def usage_summary(self):
        return self.usage_store.user_summary(
            "reconciliation-test-user"
        )

    # ======================================
    # 1. 正常聊天按完整 Token 结算
    # ======================================

    def test_normal_chat_settles_observed_cost(self):
        request_id = self.new_id()

        with patch.object(
            agent.client.chat.completions,
            "create",
            return_value=self.response(),
        ) as model:

            result = self.send(request_id)

        self.assertEqual(result.status_code, 200)
        model.assert_called_once()

        self.assertEqual(
            self.usage_summary()["calls"],
            1,
        )

        row = self.budget_row(request_id)

        self.assertEqual(row[0], "settled")
        self.assertEqual(row[1], 50_000_000)

        # 1000输入 * $0.30/百万
        # 500输出 * $1.20/百万
        # 总费用 $0.0009
        self.assertEqual(row[2], 900_000)

    # ======================================
    # 2. 提醒的两次模型调用全部计入
    # ======================================

    def test_reminder_settles_two_calls(self):
        request_id = self.new_id()

        tool_call = SimpleNamespace(
            id="tool_reconcile_test",
            function=SimpleNamespace(
                name="create_reminder",
                arguments=(
                    '{"message":"喝水","minutes":5}'
                ),
            ),
        )

        first_response = self.response(
            text=None,
            tool_calls=[tool_call],
            finish_reason="tool_calls",
        )

        second_response = self.response(
            text="已经设置提醒",
        )

        with patch.object(
            agent.client.chat.completions,
            "create",
            side_effect=[
                first_response,
                second_response,
            ],
        ) as model:

            with patch.object(
                agent,
                "create_reminder",
                return_value=(
                    "reminder_test",
                    datetime(
                        2026, 10, 12,
                        tzinfo=timezone.utc,
                    ),
                ),
            ) as reminder:

                result = self.send(
                    request_id,
                    "5分钟后提醒我喝水",
                )

        self.assertEqual(result.status_code, 200)

        self.assertEqual(
            result.json()["reply"],
            "已经设置提醒",
        )

        self.assertEqual(model.call_count, 2)

        reminder.assert_called_once_with(
            "喝水",
            5,
        )

        self.assertEqual(
            self.usage_summary()["calls"],
            2,
        )

        row = self.budget_row(request_id)

        self.assertEqual(row[0], "settled")
        self.assertEqual(row[2], 1_800_000)

    # ======================================
    # 3. 重复请求不会再次计费或调用模型
    # ======================================

    def test_duplicate_keeps_original_settlement(
        self,
    ):
        request_id = self.new_id()

        with patch.object(
            agent.client.chat.completions,
            "create",
            return_value=self.response(),
        ) as model:

            first = self.send(request_id)
            duplicate = self.send(request_id)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(duplicate.status_code, 200)

        self.assertEqual(
            first.json(),
            duplicate.json(),
        )

        model.assert_called_once()

        self.assertEqual(
            self.usage_summary()["calls"],
            1,
        )

        row = self.budget_row(request_id)

        self.assertEqual(row[0], "settled")
        self.assertEqual(row[2], 900_000)

    # ======================================
    # 4. 缺少 usage 不允许释放差额
    # ======================================

    def test_missing_usage_keeps_full_reserve(
        self,
    ):
        request_id = self.new_id()

        with patch.object(
            agent.client.chat.completions,
            "create",
            return_value=self.response(
                usage=False,
            ),
        ):
            result = self.send(request_id)

        self.assertEqual(result.status_code, 200)

        row = self.budget_row(request_id)

        self.assertEqual(row[0], "uncertain")
        self.assertEqual(row[1], 50_000_000)
        self.assertEqual(row[2], 50_000_000)

        self.assertEqual(
            self.usage_summary()["unreported_calls"],
            1,
        )

    # ======================================
    # 5. 用量保存失败不能当成免费
    # ======================================

    def test_usage_database_failure_keeps_reserve(
        self,
    ):
        request_id = self.new_id()

        with patch.object(
            agent.client.chat.completions,
            "create",
            return_value=self.response(),
        ):

            with patch.object(
                agent,
                "_get_usage_store",
                side_effect=OSError(
                    "模拟用量数据库故障"
                ),
            ):

                with patch.object(
                    agent.logger,
                    "exception",
                ):

                    result = self.send(request_id)

        # 模型确实返回了结果，
        # 不应该因为计量失败而丢弃回复。
        self.assertEqual(result.status_code, 200)

        self.assertEqual(
            self.usage_summary()["calls"],
            0,
        )

        row = self.budget_row(request_id)

        self.assertEqual(row[0], "uncertain")
        self.assertEqual(row[2], 50_000_000)

    # ======================================
    # 6. 模型超时不能释放预留额
    # ======================================

    def test_model_timeout_keeps_full_reserve(
        self,
    ):
        request_id = self.new_id()

        with patch.object(
            agent.client.chat.completions,
            "create",
            side_effect=TimeoutError(
                "模拟模型超时"
            ),
        ) as model:

            with patch.object(
                api.logger,
                "exception",
            ):

                result = self.send(request_id)

        self.assertEqual(result.status_code, 502)

        self.assertEqual(
            result.json()["status"],
            "unknown",
        )

        model.assert_called_once()

        row = self.budget_row(request_id)

        self.assertEqual(row[0], "uncertain")
        self.assertEqual(row[2], 50_000_000)

        # 本地没收到 usage，不能推断平台没有收费。
        self.assertEqual(
            self.usage_summary()["calls"],
            0,
        )


if __name__ == "__main__":
    unittest.main()