
"""受保护聊天路由的 HTTP 回归测试。

所有数据库都建立在临时目录中；模型调用一律 mock。
绝不访问项目的真实数据库，也不会消费模型额度。
"""

import sqlite3
import unittest
import uuid

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

from fastapi.testclient import TestClient

from src import api
from src.auth_api import get_auth_store
from src.core.auth_store import AuthStore
from src.main import app


class TestSecureChatHttp(unittest.TestCase):
    PASSWORD = "TestPassword2026!"

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        root = Path(self.temp_dir.name)

        # 每项测试使用独立的临时账户数据库
        self.store = AuthStore(root / "test_auth.db")

        owner = self.store.bootstrap_owner(
            "testowner",
            self.PASSWORD,
        )

        invite = self.store.issue_invite(owner["id"])

        self.store.register(
            "testmember",
            self.PASSWORD,
            invite,
        )

        # 替换聊天数据库路径，绝不接触真实数据
        self.chat_db_patch = patch.object(
            api,
            "CHAT_DB_PATH",
            root / "test_chat.db",
        )
        self.chat_db_patch.start()

        # 正式 App 的认证依赖使用临时账户库
        self.old_overrides = app.dependency_overrides.copy()

        app.dependency_overrides[get_auth_store] = (
            lambda: self.store
        )

        # 触发正式启动流程及非破坏性迁移
        self.client = TestClient(app)
        self.client.__enter__()

        self.owner_token = self.login("testowner")
        self.member_token = self.login("testmember")

    def tearDown(self):
        self.client.__exit__(None, None, None)

        app.dependency_overrides.clear()
        app.dependency_overrides.update(
            self.old_overrides
        )

        self.chat_db_patch.stop()
        self.temp_dir.cleanup()

    def login(self, username):
        response = self.client.post(
            "/auth/login",
            json={
                "username": username,
                "password": self.PASSWORD,
            },
        )

        self.assertEqual(response.status_code, 200)
        return response.json()["token"]

    @staticmethod
    def auth(token):
        return {
            "Authorization": f"Bearer {token}"
        }

    @staticmethod
    def request_id():
        return "secure_" + uuid.uuid4().hex

    def post_chat(
        self,
        token,
        request_id,
        message="测试消息",
    ):
        return self.client.post(
            "/chat",
            headers=self.auth(token),
            json={
                "request_id": request_id,
                "message": message,
            },
        )

    def get_status(self, token, request_id):
        return self.client.get(
            f"/chat/requests/{request_id}",
            headers=self.auth(token),
        )

    # ======================================
    # 1. 未登录不能发送，也不能读取
    # ======================================

    def test_no_token_cannot_send_or_read(self):
        request_id = self.request_id()

        with patch.object(api, "chat") as model:
            sent = self.client.post(
                "/chat",
                json={
                    "request_id": request_id,
                    "message": "测试",
                },
            )

            read = self.client.get(
                f"/chat/requests/{request_id}"
            )

        self.assertEqual(sent.status_code, 401)
        self.assertEqual(read.status_code, 401)

        model.assert_not_called()

    # ======================================
    # 2. 伪造 Token 无法使用
    # ======================================

    def test_invalid_token_returns_401(self):
        with patch.object(api, "chat") as model:
            sent = self.post_chat(
                "not-a-valid-token",
                self.request_id(),
            )

            read = self.get_status(
                "not-a-valid-token",
                self.request_id(),
            )

        self.assertEqual(sent.status_code, 401)
        self.assertEqual(read.status_code, 401)

        model.assert_not_called()

    # ======================================
    # 3. 本人发送、查询与缓存去重
    # ======================================

    def test_owner_can_send_query_and_reuse_cached_reply(
        self,
    ):
        request_id = self.request_id()

        with patch.object(
            api,
            "chat",
            return_value="模拟 AI 回复",
        ) as model:

            first = self.post_chat(
                self.owner_token,
                request_id,
            )

            second = self.post_chat(
                self.owner_token,
                request_id,
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)

        self.assertEqual(
            first.json(),
            second.json(),
        )

        model.assert_called_once_with("测试消息")

        result = self.get_status(
            self.owner_token,
            request_id,
        )

        self.assertEqual(result.status_code, 200)

        self.assertEqual(
            result.json()["reply"],
            "模拟 AI 回复",
        )

    # ======================================
    # 4. 相同 ID 不能换消息内容
    # ======================================

    def test_same_request_id_different_message_returns_409(
        self,
    ):
        request_id = self.request_id()

        with patch.object(
            api,
            "chat",
            return_value="模拟回复",
        ) as model:

            first = self.post_chat(
                self.owner_token,
                request_id,
                "第一条",
            )

            second = self.post_chat(
                self.owner_token,
                request_id,
                "第二条",
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)

        model.assert_called_once()

    # ======================================
    # 5. 其他用户不能读取或冒领
    # ======================================

    def test_other_user_cannot_read_or_claim_request(
        self,
    ):
        request_id = self.request_id()

        with patch.object(
            api,
            "chat",
            return_value="站长私有回复",
        ) as model:

            created = self.post_chat(
                self.owner_token,
                request_id,
            )

            foreign_read = self.get_status(
                self.member_token,
                request_id,
            )

            foreign_post = self.post_chat(
                self.member_token,
                request_id,
            )

        self.assertEqual(created.status_code, 200)
        self.assertEqual(foreign_read.status_code, 404)
        self.assertEqual(foreign_post.status_code, 404)

        model.assert_called_once()

    # ======================================
    # 6. 不同用户各自拥有独立请求
    # ======================================

    def test_users_can_create_independent_requests(
        self,
    ):
        first_id = self.request_id()
        second_id = self.request_id()

        with patch.object(
            api,
            "chat",
            side_effect=["回复一", "回复二"],
        ) as model:

            first = self.post_chat(
                self.owner_token,
                first_id,
            )

            second = self.post_chat(
                self.member_token,
                second_id,
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)

        self.assertEqual(
            self.get_status(
                self.owner_token,
                first_id,
            ).status_code,
            200,
        )

        self.assertEqual(
            self.get_status(
                self.member_token,
                second_id,
            ).status_code,
            200,
        )

        self.assertEqual(
            self.get_status(
                self.owner_token,
                second_id,
            ).status_code,
            404,
        )

        self.assertEqual(
            self.get_status(
                self.member_token,
                first_id,
            ).status_code,
            404,
        )

        self.assertEqual(model.call_count, 2)

    # ======================================
    # 7. 模型结果不明时不能重复计费
    # ======================================

    def test_unknown_model_result_is_not_executed_again(
        self,
    ):
        request_id = self.request_id()

        with patch.object(
            api,
            "chat",
            side_effect=TimeoutError("模拟超时"),
        ) as model:

            first = self.post_chat(
                self.owner_token,
                request_id,
            )

            second = self.post_chat(
                self.owner_token,
                request_id,
            )

        self.assertEqual(first.status_code, 502)
        self.assertEqual(second.status_code, 409)

        result = self.get_status(
            self.owner_token,
            request_id,
        )

        self.assertEqual(
            result.json()["status"],
            "unknown",
        )

        model.assert_called_once()

    # ======================================
    # 8. 旧记录保留，但不能被冒领
    # ======================================

    def test_legacy_unowned_record_is_not_exposed(
        self,
    ):
        request_id = self.request_id()

        with closing(api.connect_db()) as conn, conn:
            conn.execute(
                """
                INSERT INTO chat_requests (
                    request_id,
                    message_hash,
                    status,
                    reply,
                    created_at,
                    updated_at
                )
                VALUES (
                    ?, ?, 'completed',
                    '历史私有回复', 't1', 't1'
                )
                """,
                (
                    request_id,
                    api.message_digest("旧请求"),
                ),
            )

        with patch.object(api, "chat") as model:
            owner_read = self.get_status(
                self.owner_token,
                request_id,
            )

            member_read = self.get_status(
                self.member_token,
                request_id,
            )

            attempted_claim = self.post_chat(
                self.owner_token,
                request_id,
                "旧请求",
            )

        self.assertEqual(owner_read.status_code, 404)
        self.assertEqual(member_read.status_code, 404)

        self.assertEqual(
            attempted_claim.status_code,
            404,
        )

        # 确认旧数据并未被删除或修改
        with closing(api.connect_db()) as conn:
            row = conn.execute(
                """
                SELECT reply, user_id
                FROM chat_requests
                WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()

        self.assertEqual(
            row["reply"],
            "历史私有回复",
        )

        self.assertIsNone(row["user_id"])
        model.assert_not_called()

    # ======================================
    # 9. 退出登录后令牌立即失效
    # ======================================

    def test_logout_revokes_token_for_chat(self):
        response = self.client.post(
            "/auth/logout",
            headers=self.auth(self.owner_token),
        )

        self.assertEqual(response.status_code, 200)

        with patch.object(api, "chat") as model:
            sent = self.post_chat(
                self.owner_token,
                self.request_id(),
            )

            read = self.get_status(
                self.owner_token,
                self.request_id(),
            )

        self.assertEqual(sent.status_code, 401)
        self.assertEqual(read.status_code, 401)

        model.assert_not_called()

    # ======================================
    # 10. 并发重复请求不能调用两次模型
    # ======================================

    def test_duplicate_while_processing_returns_202(
        self,
    ):
        request_id = self.request_id()

        started = Event()
        release = Event()

        def slow_model(_message):
            started.set()

            if not release.wait(timeout=8):
                raise TimeoutError(
                    "模拟模型等待超时"
                )

            return "模拟长任务回复"

        with patch.object(
            api,
            "chat",
            side_effect=slow_model,
        ) as model:

            with ThreadPoolExecutor(
                max_workers=2,
            ) as executor:

                pending = executor.submit(
                    self.post_chat,
                    self.owner_token,
                    request_id,
                )

                try:
                    self.assertTrue(
                        started.wait(timeout=5)
                    )

                    duplicate = self.post_chat(
                        self.owner_token,
                        request_id,
                    )

                    self.assertEqual(
                        duplicate.status_code,
                        202,
                    )

                    self.assertEqual(
                        duplicate.json()["status"],
                        "processing",
                    )

                finally:
                    release.set()

                completed = pending.result(
                    timeout=10
                )

        self.assertEqual(
            completed.status_code,
            200,
        )

        model.assert_called_once()


if __name__ == "__main__":
    unittest.main()