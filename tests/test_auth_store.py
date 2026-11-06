import gc
import os
import sqlite3
import shutil
import tempfile
import unittest

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from src.core.auth_store import (
    AuthError,
    AuthForbidden,
    AuthStore,
)
from tests.helper import create_auth_user


PASSWORD = "correct horse battery staple"


class TestAuthStore(unittest.TestCase):

    def setUp(self):

        self.temp_dir = Path(
            tempfile.mkdtemp(
                prefix="auth_test_"
            )
        )

        self.db_path = (
            self.temp_dir
            / "auth_test.db"
        )

        self.store = AuthStore(
            self.db_path
        )

        self.owner = (
            self.store.bootstrap_owner(
                "siteowner",
                PASSWORD,
            )
        )


    def tearDown(self):

        # 释放 SQLite 句柄
        gc.collect()

        # Windows 文件锁释放等待
        try:

            shutil.rmtree(
                self.temp_dir,
                ignore_errors=True,
            )

        except Exception:
            pass


    # ======================================
    # 1. 站长只能创建一次
    # ======================================

    def test_bootstrap_only_once(self):

        with self.assertRaises(
            AuthForbidden
        ):

            self.store.bootstrap_owner(
                "anotherowner",
                PASSWORD,
            )


    # ======================================
    # 2. 邀请码只能使用一次
    # ======================================



    # ======================================
    # 3. admin权限限制
    # ======================================



    # ======================================
    # 4. 普通用户不能发邀请码
    # ======================================



    # ======================================
    # 5. 过期邀请码
    # ======================================



    # ======================================
    # 6. 登录退出
    # ======================================

    def test_login_permission_logout(self):

        create_auth_user(
            self.store,
            "alice",
            PASSWORD,
            "trusted",
        )


        token = (
            self.store.login(
                "alice",
                PASSWORD,
            )
        )


        user = (
            self.store.require_role(
                token,
                "trusted",
            )
        )


        self.assertEqual(
            user["username"],
            "alice",
        )


        with self.assertRaises(
            AuthForbidden
        ):

            self.store.require_role(
                token,
                "admin",
            )


        self.store.logout(
            token
        )


        self.assertIsNone(
            self.store.session_user(
                token
            )
        )


    # ======================================
    # 7. 错误密码
    # ======================================

    def test_wrong_password(self):

        with self.assertRaises(
            AuthError
        ):

            self.store.login(
                "siteowner",
                "wrong password here",
            )


    # ======================================
    # 8. 不保存明文
    # ======================================

    def test_password_and_tokens_not_stored_in_plaintext(self):

        create_auth_user(
            self.store,
            "alice",
            PASSWORD,
            "member",
        )


        session = (
            self.store.login(
                "alice",
                PASSWORD,
            )
        )


        conn = sqlite3.connect(
            self.db_path
        )

        try:

            session_row = conn.execute(
                """
                SELECT token_hash
                FROM auth_sessions
                """
            ).fetchone()


            password_hash = conn.execute(
                """
                SELECT password_hash
                FROM auth_users
                WHERE username='alice'
                """
            ).fetchone()[0]


        finally:

            conn.close()



        self.assertIsNotNone(
            session_row
        )


        self.assertNotIn(
            PASSWORD.encode(),
            password_hash,
        )


        self.assertNotEqual(
            session,
            session_row[0],
        )


    # ======================================
    # 9. 并发邀请码
    # ======================================




if __name__ == "__main__":
    unittest.main()