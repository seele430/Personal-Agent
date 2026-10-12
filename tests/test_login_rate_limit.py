
"""登录限流测试：全部使用临时认证数据库。"""

import unittest

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.auth_api import (
    LOGIN_WINDOW_SECONDS,
    get_auth_store,
    router,
)

from src.core.auth_store import (
    AuthError,
    AuthStore,
)


class TestLoginRateLimit(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()

        self.store = AuthStore(
            Path(self.temp.name) / "auth_test.db"
        )

        self.store.bootstrap_owner(
            "testowner",
            "VeryStrongTest2026!",
        )

        self.app = FastAPI()
        self.app.include_router(router)

        self.app.dependency_overrides[
            get_auth_store
        ] = lambda: self.store

        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(
            None, None, None
        )

        self.app.dependency_overrides.clear()
        self.temp.cleanup()

    def attempt(
        self,
        username="testowner",
        password="wrong_password_2026",
    ):
        return self.client.post(
            "/auth/login",
            json={
                "username": username,
                "password": password,
            },
        )

    # --------------------------------------
    # 1. 错误次数达到上限后返回 429
    # --------------------------------------

    def test_many_failures_are_limited(self):

        with patch.object(
            self.store,
            "login",
            side_effect=AuthError("wrong"),
        ) as login:

            for _ in range(8):
                self.assertEqual(
                    self.attempt().status_code,
                    401,
                )

            response = self.attempt()

        self.assertEqual(
            response.status_code,
            429,
        )

        self.assertGreater(
            int(response.headers["retry-after"]),
            0,
        )

        login.assert_called()

        self.assertEqual(
            login.call_count,
            8,
        )

    # --------------------------------------
    # 2. 新客户端仍受到登录限制
    # --------------------------------------

    def test_limit_survives_new_client(self):

        with patch.object(
            self.store,
            "login",
            side_effect=AuthError("wrong"),
        ):
            for _ in range(8):
                self.attempt()

        second_client = TestClient(self.app)

        with patch.object(
            self.store,
            "login",
            side_effect=AuthError("wrong"),
        ):
            response = second_client.post(
                "/auth/login",
                json={
                    "username": "testowner",
                    "password": "wrong_password_2026",
                },
            )

        self.assertEqual(
            response.status_code,
            429,
        )

    # --------------------------------------
    # 3. 成功登录后重置用户名计数
    # --------------------------------------

    def test_success_resets_user_count(self):

        with patch.object(
            self.store,
            "login",
            side_effect=AuthError("wrong"),
        ):
            for _ in range(3):
                self.attempt()

        valid = self.attempt(
            password="VeryStrongTest2026!"
        )

        self.assertEqual(
            valid.status_code,
            200,
        )

        with patch.object(
            self.store,
            "login",
            side_effect=AuthError("wrong"),
        ):
            for _ in range(8):
                self.assertEqual(
                    self.attempt().status_code,
                    401,
                )

        self.assertEqual(
            self.attempt().status_code,
            429,
        )

    # --------------------------------------
    # 4. 时间窗口过后解除限制
    # --------------------------------------

    def test_limit_window_expires(self):

        start_time = 1000000

        with patch(
            "src.auth_api.time.time",
            return_value=start_time,
        ):
            with patch.object(
                self.store,
                "login",
                side_effect=AuthError("wrong"),
            ):
                for _ in range(8):
                    self.attempt()

                self.assertEqual(
                    self.attempt().status_code,
                    429,
                )

        with patch(
            "src.auth_api.time.time",
            return_value=(
                start_time
                + LOGIN_WINDOW_SECONDS
                + 1
            ),
        ):
            with patch.object(
                self.store,
                "login",
                side_effect=AuthError("wrong"),
            ):
                self.assertEqual(
                    self.attempt().status_code,
                    401,
                )

    # --------------------------------------
    # 5. 更换用户名无法绕过 IP 限制
    # --------------------------------------

    def test_ip_limit_across_usernames(self):

        with patch.object(
            self.store,
            "login",
            side_effect=AuthError("wrong"),
        ) as login:

            for index in range(40):
                response = self.attempt(
                    username=f"unknownuser{index}"
                )

                self.assertEqual(
                    response.status_code,
                    401,
                )

            blocked = self.attempt(
                username="anotherdifferentuser"
            )

        self.assertEqual(
            blocked.status_code,
            429,
        )

        self.assertEqual(
            login.call_count,
            40,
        )

    # --------------------------------------
    # 6. 一个用户名受限，不立即锁定所有账户
    # --------------------------------------

    def test_user_limits_are_independent(self):

        with patch.object(
            self.store,
            "login",
            side_effect=AuthError("wrong"),
        ):
            for _ in range(8):
                self.attempt()

            other = self.attempt(
                username="anotheruser"
            )

        self.assertEqual(
            other.status_code,
            401,
        )


if __name__ == "__main__":
    unittest.main()