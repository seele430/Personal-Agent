
"""Personal Agent 站长初始化及密码输入测试。"""

import unittest

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.core.auth_store import (
    AuthError,
    AuthForbidden,
    AuthStore,
)

from src.tools.bootstrap_owner import (
    create_owner,
    prompt_password_pair,
    validate_password,
)


PASSWORD = "correct horse battery staple"


class TestBootstrapOwner(unittest.TestCase):

    def setUp(self):
        self.tmp = TemporaryDirectory()

        self.store = AuthStore(
            Path(self.tmp.name) / "owner_test.db"
        )

    def tearDown(self):
        self.tmp.cleanup()

    # ======================================
    # 1. 正常创建站长
    # ======================================

    def test_owner_created_successfully(self):

        user = create_owner(
            self.store,
            "siteowner",
            PASSWORD,
            PASSWORD,
        )

        self.assertEqual(
            user["username"],
            "siteowner",
        )

        self.assertEqual(
            user["role"],
            "owner",
        )

    # ======================================
    # 2. 密码确认不一致
    # ======================================

    def test_password_confirmation_mismatch(self):

        with self.assertRaises(AuthError):
            create_owner(
                self.store,
                "siteowner",
                PASSWORD,
                "another password",
            )

        user = create_owner(
            self.store,
            "siteowner",
            PASSWORD,
            PASSWORD,
        )

        self.assertEqual(
            user["role"],
            "owner",
        )

    # ======================================
    # 3. 站长不能重复创建
    # ======================================

    def test_owner_cannot_be_created_twice(self):

        create_owner(
            self.store,
            "siteowner",
            PASSWORD,
            PASSWORD,
        )

        with self.assertRaises(AuthForbidden):
            create_owner(
                self.store,
                "secondowner",
                PASSWORD,
                PASSWORD,
            )

    # ======================================
    # 4. 拒绝弱密码
    # ======================================

    def test_weak_password_rejected(self):

        with self.assertRaises(AuthError):
            create_owner(
                self.store,
                "siteowner",
                "123456",
                "123456",
            )

    # ======================================
    # 5. 两次空密码不能创建账户
    # ======================================

    def test_empty_password_rejected(self):

        with self.assertRaises(AuthError):
            create_owner(
                self.store,
                "siteowner",
                "",
                "",
            )

        with self.assertRaises(AuthError):
            validate_password("   ")

    # ======================================
    # 6. 首次输入为空立即重新输入
    # ======================================

    def test_empty_first_input_retries(self):

        with patch(
            "src.tools.bootstrap_owner.getpass.getpass",
            side_effect=[
                "",
                PASSWORD,
                PASSWORD,
            ],
        ) as mocked:

            password, confirmation = (
                prompt_password_pair()
            )

        self.assertEqual(
            password,
            PASSWORD,
        )

        self.assertEqual(
            confirmation,
            PASSWORD,
        )

        self.assertEqual(
            mocked.call_count,
            3,
        )

    # ======================================
    # 7. 两次密码不同允许重新输入
    # ======================================

    def test_mismatched_password_retries(self):

        with patch(
            "src.tools.bootstrap_owner.getpass.getpass",
            side_effect=[
                PASSWORD,
                "different-password-123",
                PASSWORD,
                PASSWORD,
            ],
        ) as mocked:

            password, confirmation = (
                prompt_password_pair()
            )

        self.assertEqual(
            password,
            confirmation,
        )

        self.assertEqual(
            mocked.call_count,
            4,
        )

    # ======================================
    # 8. 连续空密码必须终止
    # ======================================

    def test_repeated_empty_password_fails(self):

        with patch(
            "src.tools.bootstrap_owner.getpass.getpass",
            side_effect=[
                "",
                "",
                "",
            ],
        ) as mocked:

            with self.assertRaises(AuthError):
                prompt_password_pair()

        self.assertEqual(
            mocked.call_count,
            3,
        )


if __name__ == "__main__":
    unittest.main()