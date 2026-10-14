
"""Personal Agent 认证接口自动化测试。"""

import sqlite3
import unittest

from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from tests.helper import isolate_paths
from src.auth_api import get_auth_store
from src.core.auth_store import AuthStore
from src.main import app


PASSWORD = "correct horse battery staple"


class TestAuthAPI(unittest.TestCase):

    def setUp(self):
        self.tmp = TemporaryDirectory()

        isolate_paths(self, self.tmp)

        self.store = AuthStore(
            Path(self.tmp.name) / "auth_test.db"
        )

        self.owner = self.store.bootstrap_owner(
            "siteowner",
            PASSWORD,
        )

        app.dependency_overrides[get_auth_store] = (
            lambda: self.store
        )

        self.client = TestClient(app)
        self.client.__enter__()

    def tearDown(self):
        try:
            self.client.__exit__(None, None, None)
        finally:
            app.dependency_overrides.pop(
                get_auth_store,
                None,
            )
            self.tmp.cleanup()

    @staticmethod
    def bearer(token):
        return {
            "Authorization": f"Bearer {token}"
        }

    def login(self, username="siteowner"):
        response = self.client.post(
            "/auth/login",
            json={
                "username": username,
                "password": PASSWORD,
            },
        )

        self.assertEqual(
            response.status_code,
            200,
            response.text,
        )

        return response.json()["token"]

    # =====================================
    # 1. 原有接口和认证接口都存在
    # =====================================

    def test_existing_routes_still_available(self):

        response = self.client.get("/health")

        self.assertEqual(
            response.status_code,
            200,
        )

        self.assertEqual(
            response.json(),
            {"status": "ok"},
        )

        # 使用 FastAPI 官方 OpenAPI 路由清单
        # 兼容新版 include_router 行为
        paths = set(
            app.openapi()["paths"].keys()
        )

        expected = {
            "/health",
            "/chat",
            "/auth/login",
            "/auth/me",
            "/auth/logout",
        }

        for path in expected:
            with self.subTest(path=path):
                self.assertIn(
                    path,
                    paths,
                )

    # =====================================
    # 2. 登录、查询用户及退出
    # =====================================

    def test_login_me_logout(self):

        token = self.login()

        response = self.client.get(
            "/auth/me",
            headers=self.bearer(token),
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        user = response.json()["user"]

        self.assertEqual(
            user["username"],
            "siteowner",
        )

        self.assertEqual(
            user["role"],
            "owner",
        )

        response = self.client.post(
            "/auth/logout",
            headers=self.bearer(token),
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        self.assertEqual(
            response.json()["status"],
            "ok",
        )

        response = self.client.get(
            "/auth/me",
            headers=self.bearer(token),
        )

        self.assertEqual(
            response.status_code,
            401,
        )

    # =====================================
    # 3. 错误密码拒绝登录
    # =====================================

    def test_wrong_password_rejected(self):

        response = self.client.post(
            "/auth/login",
            json={
                "username": "siteowner",
                "password": "wrong-password",
            },
        )

        self.assertEqual(
            response.status_code,
            401,
        )

    # =====================================
    # 4. 未登录不能查询身份
    # =====================================

    def test_missing_token_rejected(self):

        me_response = self.client.get(
            "/auth/me"
        )

        logout_response = self.client.post(
            "/auth/logout"
        )

        self.assertEqual(
            me_response.status_code,
            401,
        )

        self.assertEqual(
            logout_response.status_code,
            401,
        )

    # =====================================
    # 5. 伪造 Token
    # =====================================

    def test_invalid_token_rejected(self):

        response = self.client.get(
            "/auth/me",
            headers=self.bearer(
                "this_is_not_a_valid_token"
            ),
        )

        self.assertEqual(
            response.status_code,
            401,
        )

    # =====================================
    # 6. 过期 Token
    # =====================================

    def test_expired_token_rejected(self):

        token = self.login()

        with closing(
            sqlite3.connect(self.store.db_path)
        ) as conn:
            with conn:
                conn.execute(
                    """
                    UPDATE auth_sessions
                    SET expires_at = 0
                    """
                )

        response = self.client.get(
            "/auth/me",
            headers=self.bearer(token),
        )

        self.assertEqual(
            response.status_code,
            401,
        )

    # =====================================
    # 7. 禁用用户后会话失效
    # =====================================

    def test_disabled_user_rejected(self):

        token = self.login()

        with closing(
            sqlite3.connect(self.store.db_path)
        ) as conn:
            with conn:
                conn.execute(
                    """
                    UPDATE auth_users
                    SET active = 0
                    WHERE username = ?
                    """,
                    ("siteowner",),
                )

        response = self.client.get(
            "/auth/me",
            headers=self.bearer(token),
        )

        self.assertEqual(
            response.status_code,
            401,
        )

    # =====================================
    # 8. 普通用户身份验证
    # =====================================

    def test_member_role_is_returned_correctly(self):

        invite = self.store.issue_invite(
            self.owner["id"],
            role="member",
        )

        self.store.register(
            "alice",
            PASSWORD,
            invite,
        )

        token = self.login("alice")

        response = self.client.get(
            "/auth/me",
            headers=self.bearer(token),
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        user = response.json()["user"]

        self.assertEqual(
            user["username"],
            "alice",
        )

        self.assertEqual(
            user["role"],
            "member",
        )

        self.assertNotIn(
            "password_hash",
            user,
        )

    # =====================================
    # 9. Token 哈希存储
    # =====================================

    def test_session_token_is_hashed(self):

        token = self.login()

        with closing(
            sqlite3.connect(self.store.db_path)
        ) as conn:

            row = conn.execute(
                """
                SELECT token_hash
                FROM auth_sessions
                LIMIT 1
                """
            ).fetchone()

        self.assertIsNotNone(row)

        self.assertNotEqual(
            token,
            row[0],
        )

        self.assertEqual(
            len(row[0]),
            64,
        )


if __name__ == "__main__":
    unittest.main()