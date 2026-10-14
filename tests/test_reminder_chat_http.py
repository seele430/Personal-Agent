"""AI 聊天提醒的 HTTP 身份隔离测试。

全部使用临时数据库、模拟登录和模拟模型。
不调用真实 DeepSeek，不发送 ntfy 通知。
"""

import json
import sqlite3
import unittest
import uuid

from contextlib import closing
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from tests.helper import isolate_paths
from src import agent
from src import api
from src.auth_api import get_current_user
from src.core import reminder_identity
from src.core.ai_usage import AIUsageStore
from src.core.reminder_store import ReminderStore
from src.main import app


class TestReminderChatHTTP(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        temporary = isolate_paths(self, self.temp)

        self.chat_path = temporary.chat_requests_db
        self.reminder_path = temporary.reminders_db
        self.usage_path = temporary.ai_usage_db

        self.usage_store = AIUsageStore(
            self.usage_path
        )

        # 模型用量库与旧提醒初始化都换成测试替身。
        patches = [
            patch.object(
                agent,
                "_get_usage_store",
                return_value=self.usage_store,
            ),
            patch.object(
                agent,
                "init_db",
            ),
        ]

        for item in patches:
            item.start()
            self.addCleanup(item.stop)

        previous_overrides = (
            app.dependency_overrides.copy()
        )

        def restore_overrides():
            app.dependency_overrides.clear()
            app.dependency_overrides.update(
                previous_overrides
            )

        self.addCleanup(restore_overrides)

        # 模拟已经由后端验证的登录身份。
        # 身份由 Authorization 决定，
        # 而不是从聊天消息里读取。
        def fake_user(request: Request):
            token = request.headers.get(
                "authorization",
                "",
            )

            if token == "Bearer owner-test-token":
                return {
                    "id": "owner_test_001",
                    "username": "testowner",
                    "role": "owner",
                    "active": True,
                }

            if token == "Bearer member-test-token":
                return {
                    "id": "member_test_001",
                    "username": "testmember",
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

        self.addCleanup(
            lambda: self.client.__exit__(
                None, None, None
            )
        )

    @staticmethod
    def new_id():
        return "reminder_" + uuid.uuid4().hex

    @staticmethod
    def tool_call():
        return SimpleNamespace(
            id="tool_create_reminder_001",
            function=SimpleNamespace(
                name="create_reminder",
                arguments=json.dumps(
                    {
                        "message": "喝水",
                        "minutes": 5,
                    },
                    ensure_ascii=False,
                ),
            ),
        )

    @staticmethod
    def model_response(
        text="模拟回复",
        *,
        tool_calls=None,
    ):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason=(
                        "tool_calls"
                        if tool_calls
                        else "stop"
                    ),
                    message=SimpleNamespace(
                        content=text,
                        tool_calls=tool_calls,
                    ),
                ),
            ],
            usage=SimpleNamespace(
                prompt_tokens=1000,
                completion_tokens=500,
                total_tokens=1500,
            ),
        )

    def model_replies(self, final_text):
        return [
            self.model_response(
                text=None,
                tool_calls=[
                    self.tool_call()
                ],
            ),
            self.model_response(
                text=final_text,
            ),
        ]

    def send(
        self,
        request_id,
        *,
        token="owner-test-token",
        message="5分钟后提醒我喝水",
    ):
        return self.client.post(
            "/chat",
            headers={
                "Authorization": (
                    f"Bearer {token}"
                ),
            },
            json={
                "request_id": request_id,
                "message": message,
            },
        )

    def reminder_counts(self):
        if not self.reminder_path.is_file():
            return (0, 0)

        with closing(
            sqlite3.connect(self.reminder_path)
        ) as conn:

            legacy = conn.execute(
                """
                SELECT COUNT(*)
                FROM reminders
                """
            ).fetchone()[0]

            owned = conn.execute(
                """
                SELECT COUNT(*)
                FROM reminder_requests
                """
            ).fetchone()[0]

        return legacy, owned

    # ======================================
    # 工具测试工具函数
    # ======================================

    @staticmethod
    def tool_call_for(name, **arguments):
        """构造一次任意工具的调用。"""

        return SimpleNamespace(
            id=f"tool_{name}_001",
            function=SimpleNamespace(
                name=name,
                arguments=json.dumps(
                    arguments,
                    ensure_ascii=False,
                ),
            ),
        )

    def model_replies_with(
        self,
        tool_calls,
        final_text,
    ):
        return [
            self.model_response(
                text=None,
                tool_calls=tool_calls,
            ),
            self.model_response(text=final_text),
        ]

    def tool_result(self, model):
        """取出第二次模型调用看到的工具结果。"""

        messages = (
            model.call_args_list[1].kwargs["messages"]
        )

        return json.loads(messages[-1]["content"])

    def stored_status(self, reminder_id):
        with closing(
            sqlite3.connect(self.reminder_path)
        ) as conn:
            return conn.execute(
                """
                SELECT status
                FROM reminder_requests
                WHERE id = ?
                """,
                (reminder_id,),
            ).fetchone()[0]

    def seed_reminder(
        self,
        *,
        user_id,
        message,
        minutes=5,
    ):
        store = ReminderStore(self.reminder_path)

        store.init_db()

        return store.create(
            user_id=user_id,
            request_id="seed_" + uuid.uuid4().hex,
            role="member",
            message=message,
            minutes=minutes,
        )

    # ======================================
    # 1. 站长提醒写入原 ntfy 队列
    # ======================================

    def test_owner_reminder_uses_ntfy_queue(self):
        request_id = self.new_id()

        with patch.object(
            agent.client.chat.completions,
            "create",
            side_effect=self.model_replies(
                "提醒已保存"
            ),
        ) as model:

            response = self.send(request_id)

        self.assertEqual(
            response.status_code,
            200,
        )

        self.assertEqual(
            model.call_count,
            2,
        )

        self.assertEqual(
            self.reminder_counts(),
            (1, 1),
        )

        with closing(
            sqlite3.connect(self.reminder_path)
        ) as conn:

            row = conn.execute(
                """
                SELECT
                    request_id,
                    user_id,
                    delivery,
                    legacy_reminder_id
                FROM reminder_requests
                """
            ).fetchone()

            old_row = conn.execute(
                """
                SELECT message, status
                FROM reminders
                """
            ).fetchone()

        self.assertEqual(row[0], request_id)
        self.assertEqual(row[1], "owner_test_001")
        self.assertEqual(row[2], "ntfy")
        self.assertIsNotNone(row[3])

        self.assertEqual(
            old_row,
            ("喝水", "pending"),
        )

    # ======================================
    # 2. 普通成员只进站内提醒，不进站长 ntfy 队列
    # ======================================

    def test_member_reminder_goes_to_in_app_only(self):
        request_id = self.new_id()

        with patch.object(
            agent.client.chat.completions,
            "create",
            side_effect=self.model_replies(
                "提醒已创建，将在站内提醒中心显示"
            ),
        ) as model:

            response = self.send(
                request_id,
                token="member-test-token",
            )

        self.assertEqual(
            response.status_code,
            200,
        )

        self.assertEqual(
            model.call_count,
            2,
        )

        # 检查工具实际创建成功，
        # 不是只依赖模型回复文字。
        second_messages = (
            model.call_args_list[1]
            .kwargs["messages"]
        )

        tool_result = json.loads(
            second_messages[-1]["content"]
        )

        self.assertTrue(
            tool_result["success"]
        )

        # 成员提醒只能走站内投递。
        self.assertEqual(
            tool_result["delivery"],
            "in_app",
        )

        # 旧表（站长 ntfy 队列）必须为空，
        # 新表恰好一条。
        self.assertEqual(
            self.reminder_counts(),
            (0, 1),
        )

        with closing(
            sqlite3.connect(self.reminder_path)
        ) as conn:
            row = conn.execute(
                """
                SELECT user_id, delivery, status
                FROM reminder_requests
                """
            ).fetchone()

        # 身份取自登录会话，不取自聊天内容。
        self.assertEqual(row[0], "member_test_001")
        self.assertEqual(row[1], "in_app")
        self.assertEqual(row[2], "pending")

    # ======================================
    # 2b. 关闭站内投递时，成员仍然不能创建提醒
    # ======================================

    def test_member_reminder_is_rejected_when_disabled(self):
        request_id = self.new_id()

        with patch.object(
            reminder_identity,
            "IN_APP_DELIVERY_ENABLED",
            False,
        ):
            with patch.object(
                agent.client.chat.completions,
                "create",
                side_effect=self.model_replies(
                    "个人提醒功能尚未开放"
                ),
            ) as model:

                response = self.send(
                    request_id,
                    token="member-test-token",
                )

        self.assertEqual(
            response.status_code,
            200,
        )

        self.assertEqual(
            model.call_count,
            2,
        )

        second_messages = (
            model.call_args_list[1]
            .kwargs["messages"]
        )

        tool_result = json.loads(
            second_messages[-1]["content"]
        )

        self.assertFalse(
            tool_result["success"]
        )

        self.assertIn(
            "站内通知中心",
            tool_result["error"],
        )

        # 开关关闭时什么都不写。
        self.assertEqual(
            self.reminder_counts(),
            (0, 0),
        )

    # ======================================
    # 3. 重复请求不得再次创建提醒
    # ======================================

    def test_duplicate_chat_creates_once(self):
        request_id = self.new_id()

        with patch.object(
            agent.client.chat.completions,
            "create",
            side_effect=self.model_replies(
                "提醒已保存"
            ),
        ) as model:

            first = self.send(request_id)

            duplicate = self.send(request_id)

        self.assertEqual(
            first.status_code,
            200,
        )

        self.assertEqual(
            duplicate.status_code,
            200,
        )

        self.assertEqual(
            first.json(),
            duplicate.json(),
        )

        # 两次调用是同一条聊天内部的
        # 两次模型响应，不是两次创建提醒。
        self.assertEqual(
            model.call_count,
            2,
        )

        self.assertEqual(
            self.reminder_counts(),
            (1, 1),
        )

    # ======================================
    # 4. 其他用户不能冒用原请求
    # ======================================

    def test_other_user_cannot_replay_request(self):
        request_id = self.new_id()

        with patch.object(
            agent.client.chat.completions,
            "create",
            side_effect=self.model_replies(
                "提醒已保存"
            ),
        ) as model:

            first = self.send(request_id)

            stolen = self.send(
                request_id,
                token="member-test-token",
            )

        self.assertEqual(
            first.status_code,
            200,
        )

        self.assertEqual(
            stolen.status_code,
            404,
        )

        self.assertEqual(
            model.call_count,
            2,
        )

        self.assertEqual(
            self.reminder_counts(),
            (1, 1),
        )

    # ======================================
    # 5. 未登录用户不能创建提醒
    # ======================================

    def test_unauthenticated_user_is_rejected(self):
        request_id = self.new_id()

        with patch.object(
            agent.client.chat.completions,
            "create",
        ) as model:

            response = self.send(
                request_id,
                token="invalid-token",
            )

        self.assertEqual(
            response.status_code,
            401,
        )

        model.assert_not_called()

        self.assertEqual(
            self.reminder_counts(),
            (0, 0),
        )

    # ======================================
    # 6. 模型异常后必须清理身份上下文
    # ======================================

    def test_timeout_clears_reminder_identity(self):
        request_id = self.new_id()

        with patch.object(
            agent.client.chat.completions,
            "create",
            side_effect=TimeoutError(
                "模拟模型超时"
            ),
        ):

            with patch.object(
                api.logger,
                "exception",
            ):

                response = self.send(request_id)

        self.assertEqual(
            response.status_code,
            502,
        )

        # 模型结束后，不得保留上一次用户身份。
        with self.assertRaises(PermissionError):
            reminder_identity.create_reminder_for_current_user(
                "喝水",
                5,
            )

        self.assertEqual(
            self.reminder_counts(),
            (0, 0),
        )


    # ======================================
    # 6. 暴露给模型的工具集合
    # ======================================

    def test_all_reminder_tools_are_exposed(self):
        request_id = self.new_id()

        with patch.object(
            agent.client.chat.completions,
            "create",
            side_effect=self.model_replies_with(
                [self.tool_call_for("list_reminders")],
                "好的",
            ),
        ) as model:

            self.send(
                request_id,
                token="member-test-token",
            )

        names = [
            item["function"]["name"]
            for item in (
                model.call_args_list[0]
                .kwargs["tools"]
            )
        ]

        self.assertEqual(
            names,
            [
                "create_reminder",
                "list_reminders",
                "cancel_reminder",
                "update_reminder",
            ],
        )

    # ======================================
    # 7. 列出提醒只返回自己的
    # ======================================

    def test_list_reminders_returns_only_own(self):
        request_id = self.new_id()

        self.seed_reminder(
            user_id="member_test_001",
            message="自己的提醒",
        )
        self.seed_reminder(
            user_id="other_member_001",
            message="别人的提醒",
        )

        with patch.object(
            agent.client.chat.completions,
            "create",
            side_effect=self.model_replies_with(
                [self.tool_call_for("list_reminders")],
                "这是你的提醒",
            ),
        ) as model:

            response = self.send(
                request_id,
                token="member-test-token",
            )

        self.assertEqual(response.status_code, 200)

        result = self.tool_result(model)

        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(
            [
                item["message"]
                for item in result["reminders"]
            ],
            ["自己的提醒"],
        )

        # 结果里不带其他用户的字段。
        self.assertNotIn(
            "user_id",
            result["reminders"][0],
        )

    # ======================================
    # 8. 取消提醒
    # ======================================

    def test_cancel_reminder_cancels_own_pending(self):
        request_id = self.new_id()

        created = self.seed_reminder(
            user_id="member_test_001",
            message="要取消的提醒",
        )

        with patch.object(
            agent.client.chat.completions,
            "create",
            side_effect=self.model_replies_with(
                [
                    self.tool_call_for(
                        "cancel_reminder",
                        reminder_id=created["id"],
                    )
                ],
                "已经取消",
            ),
        ) as model:

            response = self.send(
                request_id,
                token="member-test-token",
            )

        self.assertEqual(response.status_code, 200)

        result = self.tool_result(model)

        self.assertTrue(result["success"])
        self.assertTrue(result["changed"])
        self.assertEqual(
            result["status"],
            "cancelled",
        )

        self.assertEqual(
            self.stored_status(created["id"]),
            "cancelled",
        )

    def test_cancel_other_users_reminder_is_rejected(self):
        request_id = self.new_id()

        other = self.seed_reminder(
            user_id="other_member_001",
            message="别人的提醒",
        )

        with patch.object(
            agent.client.chat.completions,
            "create",
            side_effect=self.model_replies_with(
                [
                    self.tool_call_for(
                        "cancel_reminder",
                        reminder_id=other["id"],
                    )
                ],
                "无法取消",
            ),
        ) as model:

            self.send(
                request_id,
                token="member-test-token",
            )

        result = self.tool_result(model)

        self.assertFalse(result["success"])
        self.assertIn("无权访问", result["error"])

        # 别人的提醒没有被改动。
        self.assertEqual(
            self.stored_status(other["id"]),
            "pending",
        )

    # ======================================
    # 9. 修改提醒
    # ======================================

    def test_update_reminder_changes_time(self):
        request_id = self.new_id()

        created = self.seed_reminder(
            user_id="member_test_001",
            message="要改期的提醒",
        )

        with patch.object(
            agent.client.chat.completions,
            "create",
            side_effect=self.model_replies_with(
                [
                    self.tool_call_for(
                        "update_reminder",
                        reminder_id=created["id"],
                        minutes=30,
                    )
                ],
                "已经改到 30 分钟后",
            ),
        ) as model:

            self.send(
                request_id,
                token="member-test-token",
            )

        result = self.tool_result(model)

        self.assertTrue(result["success"])
        self.assertEqual(result["minutes"], 30)
        self.assertNotEqual(
            result["remind_at"],
            created["remind_at"],
        )

    def test_update_reminder_requires_a_field(self):
        request_id = self.new_id()

        created = self.seed_reminder(
            user_id="member_test_001",
            message="什么都没改",
        )

        with patch.object(
            agent.client.chat.completions,
            "create",
            side_effect=self.model_replies_with(
                [
                    self.tool_call_for(
                        "update_reminder",
                        reminder_id=created["id"],
                    )
                ],
                "需要更多信息",
            ),
        ) as model:

            self.send(
                request_id,
                token="member-test-token",
            )

        result = self.tool_result(model)

        self.assertFalse(result["success"])
        self.assertIn(
            "新的提醒内容或新的提醒时间",
            result["error"],
        )

    # ======================================
    # 10. 未知工具不得被执行
    # ======================================

    def test_unknown_tool_is_rejected(self):
        request_id = self.new_id()

        with patch.object(
            agent.client.chat.completions,
            "create",
            side_effect=self.model_replies_with(
                [
                    self.tool_call_for(
                        "delete_everything",
                        reminder_id=1,
                    )
                ],
                "无法执行",
            ),
        ) as model:

            self.send(
                request_id,
                token="member-test-token",
            )

        result = self.tool_result(model)

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "未知工具")

        self.assertEqual(
            self.reminder_counts(),
            (0, 0),
        )


if __name__ == "__main__":
    unittest.main()