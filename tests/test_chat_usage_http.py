
"""正式 HTTP 聊天接口的模型用量归属测试。

不访问真实账户或聊天数据库，不调用付费模型。
"""

import unittest
import uuid

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
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


class TestChatUsageHTTP(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()

        root = Path(self.temp.name)

        self.usage_store = AIUsageStore(
            root / "usage_test.db"
        )

        isolate_paths(self, self.temp)

        self.init_patch = patch.object(
            agent,
            "init_db",
        )
        self.init_patch.start()

        self.usage_patch = patch.object(
            agent,
            "_get_usage_store",
            return_value=self.usage_store,
        )
        self.usage_patch.start()

        self.old_overrides = (
            app.dependency_overrides.copy()
        )

        def fake_user(request: Request) -> dict:
            authorization = request.headers.get(
                "authorization",
                "",
            )

            if authorization == "Bearer owner-token":
                return {
                    "id": "owner-test-id",
                    "username": "owner",
                    "role": "owner",
                    "active": True,
                }

            if authorization == "Bearer member-token":
                return {
                    "id": "member-test-id",
                    "username": "member",
                    "role": "member",
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

    def tearDown(self):
        self.client.__exit__(
            None,
            None,
            None,
        )

        app.dependency_overrides.clear()
        app.dependency_overrides.update(
            self.old_overrides
        )

        self.usage_patch.stop()
        self.init_patch.stop()
        self.usage_patch.stop()

        self.temp.cleanup()

    @staticmethod
    def new_id():
        return "usage_" + uuid.uuid4().hex

    @staticmethod
    def response(
        content="模拟 AI 回复",
        tool_calls=None,
        usage=True,
    ):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=content,
                        tool_calls=tool_calls,
                    )
                )
            ],
            usage=(
                SimpleNamespace(
                    prompt_tokens=10,
                    completion_tokens=5,
                    total_tokens=15,
                )
                if usage
                else None
            ),
        )

    def send(
        self,
        request_id,
        *,
        user="owner",
        message="测试消息",
    ):
        return self.client.post(
            "/chat",
            headers={
                "Authorization":
                    f"Bearer {user}-token",
            },
            json={
                "request_id": request_id,
                "message": message,
            },
        )

    def summary(self, user="owner"):
        return self.usage_store.user_summary(
            f"{user}-test-id"
        )

    # ======================================
    # 1. 普通聊天记录一次，重复请求不重复计费
    # ======================================

    def test_normal_chat_and_duplicate_record_once(
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
            self.summary()["calls"],
            1,
        )

        self.assertEqual(
            self.summary()["observed_total_tokens"],
            15,
        )

    # ======================================
    # 2. 提醒流程的两次模型调用分别记录
    # ======================================

    def test_reminder_records_two_model_calls(
        self,
    ):
        tool_call = SimpleNamespace(
            id="tool_test_1",
            function=SimpleNamespace(
                name="create_reminder",
                arguments=(
                    '{"message":"喝水","minutes":5}'
                ),
            ),
        )

        first = self.response(
            content=None,
            tool_calls=[tool_call],
        )

        second = self.response(
            content="已经设置提醒",
        )

        with patch.object(
            agent.client.chat.completions,
            "create",
            side_effect=[first, second],
        ) as model:

            with patch.object(
                agent,
                "create_reminder",
                return_value=(
                    "reminder_test_1",
                    datetime(
                        2026, 10, 12,
                        tzinfo=timezone.utc,
                    ),
                ),
            ) as reminder:

                result = self.send(
                    self.new_id(),
                    message="5分钟后提醒我喝水",
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
            self.summary()["calls"],
            2,
        )

        self.assertEqual(
            self.summary()["observed_total_tokens"],
            30,
        )

    # ======================================
    # 3. 两个用户的用量不能混在一起
    # ======================================

    def test_users_have_separate_usage(
        self,
    ):
        owner_id = self.new_id()
        member_id = self.new_id()

        with patch.object(
            agent.client.chat.completions,
            "create",
            side_effect=[
                self.response("站长回复"),
                self.response("成员回复"),
            ],
        ) as model:

            owner = self.send(owner_id)

            member = self.send(
                member_id,
                user="member",
            )

        self.assertEqual(owner.status_code, 200)
        self.assertEqual(member.status_code, 200)

        self.assertEqual(model.call_count, 2)

        self.assertEqual(
            self.summary("owner")["calls"],
            1,
        )

        self.assertEqual(
            self.summary("member")["calls"],
            1,
        )

        # 成员不能读取站长的聊天结果。
        foreign_read = self.client.get(
            f"/chat/requests/{owner_id}",
            headers={
                "Authorization": "Bearer member-token",
            },
        )

        self.assertEqual(
            foreign_read.status_code,
            404,
        )

    # ======================================
    # 4. 提供商没返回 Token 用量不能记成免费
    # ======================================

    def test_missing_usage_is_unreported(
        self,
    ):
        with patch.object(
            agent.client.chat.completions,
            "create",
            return_value=self.response(
                usage=False,
            ),
        ):
            result = self.send(
                self.new_id()
            )

        self.assertEqual(result.status_code, 200)

        self.assertEqual(
            self.summary()["calls"],
            1,
        )

        self.assertEqual(
            self.summary()["unreported_calls"],
            1,
        )

        self.assertEqual(
            self.summary()["observed_total_tokens"],
            0,
        )

    # ======================================
    # 5. 模型超时不伪造用量
    # ======================================

    def test_model_timeout_is_unknown_not_free(
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
                result = self.send(
                    request_id
                )

        self.assertEqual(
            result.status_code,
            502,
        )

        self.assertEqual(
            result.json()["status"],
            "unknown",
        )

        model.assert_called_once()

        # 没收到模型响应，因此本地没有已知 Token 数据。
        # 这不意味着提供商实际没有收费。
        self.assertEqual(
            self.summary()["calls"],
            0,
        )

        with closing(api.connect_db()) as conn:
            row = conn.execute(
                """
                SELECT status
                FROM chat_requests
                WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()

        self.assertEqual(
            row["status"],
            "unknown",
        )


if __name__ == "__main__":
    unittest.main()