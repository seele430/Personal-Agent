"""远程会话桥接测试：有标记 → 只读 owner；无标记 → 原逻辑；安装幂等。"""

import unittest

from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from src.remote import bridge


def request_with(marker=None):
    state = {bridge.REMOTE_MARKER: marker} if marker else {}

    return Request({"type": "http", "state": state, "headers": []})


class TestMarker(unittest.TestCase):
    def test_mark_sets_value(self):
        scope = {}

        bridge.mark_owner(scope, "u-1")

        self.assertEqual(scope["state"][bridge.REMOTE_MARKER], "u-1")

    def test_mark_ignores_empty(self):
        scope = {}

        bridge.mark_owner(scope, "")

        self.assertEqual(scope.get("state", {}), {})

    def test_marked_owner_reads(self):
        self.assertEqual(bridge.marked_owner(request_with("u-2")), "u-2")

    def test_marked_owner_absent(self):
        self.assertIsNone(bridge.marked_owner(request_with()))


class TestBridgedAuth(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def original(credentials=None, store=None):
            self.calls.append(1)
            raise HTTPException(status_code=401, detail="请先登录")

        self.bridged = bridge.make_bridged_auth(
            original, bearer_scheme=lambda: None, get_auth_store=lambda: None
        )

    def test_marked_returns_owner_identity(self):
        user = self.bridged(request_with("u-9"), credentials=None, store=None)

        self.assertEqual(user["id"], "u-9")
        self.assertEqual(user["username"], "remote")
        self.assertTrue(user["remote"])

    def test_marked_role_is_member_not_owner(self):
        user = self.bridged(request_with("u-9"), credentials=None, store=None)

        self.assertEqual(user["role"], "member")

    def test_marked_skips_original(self):
        self.bridged(request_with("u-9"), credentials=None, store=None)

        self.assertEqual(self.calls, [])

    def test_unmarked_delegates(self):
        with self.assertRaises(HTTPException):
            self.bridged(request_with(), credentials=None, store=None)

        self.assertEqual(len(self.calls), 1)

    def test_unmarked_error_is_401(self):
        with self.assertRaises(HTTPException) as caught:
            self.bridged(request_with(), credentials=None, store=None)

        self.assertEqual(caught.exception.status_code, 401)


class TestInstall(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()

        def original(credentials=None, store=None):
            raise HTTPException(status_code=401)

        self.original = original

    def test_install_registers_override(self):
        installed = bridge.install(
            self.app, self.original,
            bearer_scheme=lambda: None, get_auth_store=lambda: None,
        )

        self.assertTrue(installed)
        self.assertIn(self.original, self.app.dependency_overrides)

    def test_install_is_idempotent(self):
        bridge.install(
            self.app, self.original,
            bearer_scheme=lambda: None, get_auth_store=lambda: None,
        )
        again = bridge.install(
            self.app, self.original,
            bearer_scheme=lambda: None, get_auth_store=lambda: None,
        )

        self.assertFalse(again)

    def test_different_originals_both_install(self):
        second = lambda credentials=None, store=None: None  # noqa: E731

        first_ok = bridge.install(
            self.app, self.original,
            bearer_scheme=lambda: None, get_auth_store=lambda: None,
        )
        second_ok = bridge.install(
            self.app, second,
            bearer_scheme=lambda: None, get_auth_store=lambda: None,
        )

        self.assertTrue(first_ok)
        self.assertTrue(second_ok)


if __name__ == "__main__":
    unittest.main()