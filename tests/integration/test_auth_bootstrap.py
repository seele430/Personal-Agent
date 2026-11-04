"""首次运行：空库时通过 HTTP 创建站长账号（不再依赖终端脚本）。"""

from __future__ import annotations

import unittest

from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from src.auth_api import get_auth_store
from src.core.auth_store import AuthStore
from src.main import app
from tests.helper import isolate_paths

PASSWORD = "seeledeepseek2026"


class TestAuthBootstrap(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(ignore_cleanup_errors=True)

        isolate_paths(self, self.tmp)

        self.store = AuthStore(Path(self.tmp.name) / "auth_test.db")

        app.dependency_overrides[get_auth_store] = lambda: self.store

        self.client = TestClient(app)
        self.client.__enter__()

    def tearDown(self):
        try:
            self.client.__exit__(None, None, None)
        finally:
            app.dependency_overrides.pop(get_auth_store, None)
            self.tmp.cleanup()

    def bootstrap(self, username="seele", password=PASSWORD):
        return self.client.post(
            "/auth/bootstrap",
            json={"username": username, "password": password},
        )

    def test_empty_database_reports_registration_open(self):
        state = self.client.get("/auth/state").json()

        self.assertFalse(state["initialized"])
        self.assertTrue(state["registration_open"])

    def test_bootstrap_creates_owner_and_session(self):
        response = self.bootstrap()

        self.assertEqual(response.status_code, 201, response.text)

        body = response.json()
        self.assertEqual(body["user"]["username"], "seele")
        self.assertEqual(body["user"]["role"], "owner")
        self.assertTrue(body["token"])

        me = self.client.get(
            "/auth/me",
            headers={"Authorization": f"Bearer {body['token']}"},
        )

        self.assertEqual(me.status_code, 200, me.text)
        self.assertEqual(me.json()["user"]["username"], "seele")

    def test_bootstrap_is_one_time(self):
        self.assertEqual(self.bootstrap().status_code, 201)

        second = self.bootstrap(username="intruder")

        self.assertEqual(second.status_code, 409, second.text)
        self.assertIn("已经初始化", second.json()["detail"])

        state = self.client.get("/auth/state").json()
        self.assertTrue(state["initialized"])
        self.assertFalse(state["registration_open"])

    def test_short_password_is_rejected(self):
        response = self.bootstrap(password="short")

        self.assertEqual(response.status_code, 400, response.text)
        self.assertFalse(self.store.has_users())

    def test_after_bootstrap_normal_login_works(self):
        self.assertEqual(self.bootstrap().status_code, 201)

        login = self.client.post(
            "/auth/login",
            json={"username": "seele", "password": PASSWORD},
        )

        self.assertEqual(login.status_code, 200, login.text)

    def test_password_reset_revokes_old_sessions_and_password(self):
        created = self.bootstrap()
        token = created.json()["token"]

        self.assertIsNotNone(self.store.session_user(token))

        self.store.set_password("seele", "another-password-123")

        # 旧 token 失效、旧密码登录失败
        self.assertIsNone(self.store.session_user(token))

        old_login = self.client.post(
            "/auth/login",
            json={"username": "seele", "password": PASSWORD},
        )
        self.assertEqual(old_login.status_code, 401, old_login.text)

        new_login = self.client.post(
            "/auth/login",
            json={"username": "seele", "password": "another-password-123"},
        )
        self.assertEqual(new_login.status_code, 200, new_login.text)

    def test_password_reset_rejects_short_password(self):
        self.bootstrap()

        with self.assertRaises(Exception):
            self.store.set_password("seele", "short")

    def test_web_reset_endpoint_works_from_loopback(self):
        created = self.bootstrap()
        token = created.json()["token"]

        response = self.client.post(
            "/auth/reset-password",
            json={"username": "seele", "password": "webreset123456"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(self.store.session_user(token))

        self.assertEqual(
            self.client.post(
                "/auth/login",
                json={"username": "seele", "password": PASSWORD},
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.post(
                "/auth/login",
                json={"username": "seele", "password": "webreset123456"},
            ).status_code,
            200,
        )

    def test_web_reset_rejects_unknown_user(self):
        self.bootstrap()

        response = self.client.post(
            "/auth/reset-password",
            json={"username": "nobody", "password": "webreset123456"},
        )

        self.assertEqual(response.status_code, 400, response.text)


if __name__ == "__main__":
    unittest.main()
