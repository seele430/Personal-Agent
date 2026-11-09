"""远程守卫测试：先鉴权、再判方法、再判白名单；拒绝时不触达下游。"""

import asyncio
import tempfile
import unittest

from pathlib import Path

from src.remote.access_log import AccessLog
from src.remote.config import RemoteConfig, RemoteConfigError, from_env
from src.remote.guard import RemoteGuard, extract_token


TOKEN = "t" * 40


async def call(
    app,
    method,
    path,
    *,
    headers=None,
    client=("10.0.0.9", 1234),
    query=b"",
    scheme="http",
):
    sent = []

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query,
        "scheme": scheme,
        "headers": headers or [],
        "client": client,
    }

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)

    return sent


def status_of(sent):
    return next(item["status"] for item in sent if item["type"] == "http.response.start")


class CountingApp:
    def __init__(self):
        self.calls = 0

    async def __call__(self, scope, receive, send):
        self.calls += 1

        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


class GuardCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.log_path = Path(self._temporary.name) / "access.jsonl"
        self.app = CountingApp()

    def build(self, **overrides):
        config = RemoteConfig(
            enabled=True,
            token=TOKEN,
            log_path=self.log_path,
            **overrides,
        )

        return RemoteGuard(self.app, config, log=AccessLog(self.log_path))

    def run_call(self, guard, method, path, **kwargs):
        return asyncio.run(call(guard, method, path, **kwargs))


class TestAuth(GuardCase):
    def test_missing_token_is_401(self):
        sent = self.run_call(self.build(), "GET", "/api/artifacts")

        self.assertEqual(status_of(sent), 401)

    def test_wrong_token_is_401(self):
        sent = self.run_call(
            self.build(), "GET", "/api/artifacts", headers=[(b"authorization", b"Bearer nope")]
        )

        self.assertEqual(status_of(sent), 401)

    def test_bearer_token_passes(self):
        sent = self.run_call(
            self.build(),
            "GET",
            "/api/artifacts",
            headers=[(b"authorization", f"Bearer {TOKEN}".encode())],
        )

        self.assertEqual(status_of(sent), 200)

    def test_cookie_token_passes(self):
        sent = self.run_call(
            self.build(),
            "GET",
            "/api/artifacts",
            headers=[(b"cookie", f"pa_remote={TOKEN}".encode())],
        )

        self.assertEqual(status_of(sent), 200)

    def test_extract_token_prefers_bearer(self):
        token = extract_token(
            {
                "headers": [
                    (b"authorization", b"Bearer abc"),
                    (b"cookie", b"pa_remote=def"),
                ]
            }
        )

        self.assertEqual(token, "abc")


class TestQueryLogin(GuardCase):
    """手机浏览器：一次性链接换 cookie。"""

    def login(self, query, *, method="GET", headers=None):
        return asyncio.run(
            call(
                self.build(),
                method,
                "/api/artifacts",
                headers=headers,
                query=query,
            )
        )

    def test_valid_query_redirects_with_cookie(self):
        sent = self.login(f"token={TOKEN}".encode())

        self.assertEqual(status_of(sent), 303)
        start = next(item for item in sent if item["type"] == "http.response.start")
        headers = dict(start["headers"])

        self.assertIn(b"pa_remote=", headers[b"set-cookie"])
        self.assertEqual(headers[b"location"], b"/api/artifacts")
        self.assertEqual(headers[b"referrer-policy"], b"no-referrer")

    def test_cookie_omits_secure_on_http(self):
        sent = self.login(f"token={TOKEN}".encode())
        start = next(item for item in sent if item["type"] == "http.response.start")

        self.assertNotIn(b"Secure", dict(start["headers"])[b"set-cookie"])

    def test_cookie_marks_secure_on_https(self):
        sent = asyncio.run(
            call(
                self.build(),
                "GET",
                "/api/artifacts",
                query=f"token={TOKEN}".encode(),
                scheme="https",
            )
        )
        start = next(item for item in sent if item["type"] == "http.response.start")

        self.assertIn(b"Secure", dict(start["headers"])[b"set-cookie"])

    def test_wrong_query_token_is_401_without_cookie(self):
        sent = self.login(b"token=wrong")

        self.assertEqual(status_of(sent), 401)
        start = next(item for item in sent if item["type"] == "http.response.start")

        self.assertNotIn(b"set-cookie", dict(start["headers"]))

    def test_post_with_query_token_is_rejected(self):
        sent = self.login(f"token={TOKEN}".encode(), method="POST")

        self.assertEqual(status_of(sent), 401)

    def test_bearer_header_wins_over_query(self):
        sent = self.login(
            b"token=wrong",
            headers=[(b"authorization", f"Bearer {TOKEN}".encode())],
        )

        self.assertEqual(status_of(sent), 200)

    def test_log_has_no_token_from_query(self):
        self.login(f"token={TOKEN}".encode())

        text = Path(self.log_path).read_text(encoding="utf-8")

        self.assertNotIn(TOKEN, text)


class TestReadOnly(GuardCase):
    def test_post_is_forbidden(self):
        sent = self.run_call(
            self.build(),
            "POST",
            "/api/artifacts",
            headers=[(b"authorization", f"Bearer {TOKEN}".encode())],
        )

        self.assertEqual(status_of(sent), 403)
        self.assertEqual(self.app.calls, 0)

    def test_delete_is_forbidden(self):
        sent = self.run_call(
            self.build(),
            "DELETE",
            "/api/artifacts/x",
            headers=[(b"authorization", f"Bearer {TOKEN}".encode())],
        )

        self.assertEqual(status_of(sent), 403)

    def test_path_outside_whitelist_is_forbidden(self):
        sent = self.run_call(
            self.build(),
            "GET",
            "/api/admin/users",
            headers=[(b"authorization", f"Bearer {TOKEN}".encode())],
        )

        self.assertEqual(status_of(sent), 403)
        self.assertEqual(self.app.calls, 0)

    def test_allowed_prefix_boundary(self):
        config = self.build().config

        self.assertTrue(config.allows("/api/artifacts"))
        self.assertTrue(config.allows("/api/artifacts/123/download"))
        self.assertFalse(config.allows("/api/artifactsx"))
        self.assertFalse(config.allows("/api"))

    def test_denied_requests_never_reach_app(self):
        self.run_call(self.build(), "GET", "/api/artifacts")

        self.assertEqual(self.app.calls, 0)


class TestLogging(GuardCase):
    def test_records_request(self):
        self.run_call(
            self.build(),
            "GET",
            "/api/artifacts?token=secret-value",
            headers=[(b"authorization", f"Bearer {TOKEN}".encode())],
        )

        records = AccessLog(self.log_path).read()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["path"], "/api/artifacts")
        self.assertEqual(records[0]["status"], 200)

    def test_log_has_no_token(self):
        self.run_call(
            self.build(),
            "GET",
            "/api/artifacts",
            headers=[(b"authorization", f"Bearer {TOKEN}".encode())],
        )

        raw = self.log_path.read_text(encoding="utf-8")

        self.assertNotIn(TOKEN, raw)

    def test_401_is_logged(self):
        self.run_call(self.build(), "GET", "/api/artifacts")

        records = AccessLog(self.log_path).read()

        self.assertEqual(records[0]["status"], 401)


class TestConfig(unittest.TestCase):
    def test_short_token_rejected(self):
        with self.assertRaises(RemoteConfigError):
            RemoteConfig(enabled=True, token="short")

    def test_disabled_by_default(self):
        config = from_env({})

        self.assertFalse(config.enabled)
        self.assertFalse(config.usable)

    def test_short_token_forces_disabled(self):
        config = from_env(
            {"PERSONAL_AGENT_REMOTE": "1", "PERSONAL_AGENT_REMOTE_TOKEN": "short"}
        )

        self.assertFalse(config.enabled)
        self.assertIn("令牌不足", config.reason)

    def test_enabled_with_long_token(self):
        config = from_env(
            {"PERSONAL_AGENT_REMOTE": "1", "PERSONAL_AGENT_REMOTE_TOKEN": "x" * 40}
        )

        self.assertTrue(config.usable)

    def test_invalid_port(self):
        with self.assertRaises(RemoteConfigError):
            RemoteConfig(enabled=False, token="", port=0)


class TestPassthrough(unittest.TestCase):
    def test_disabled_guard_does_nothing(self):
        app = CountingApp()
        guard = RemoteGuard(app, RemoteConfig(enabled=False, token=""))
        sent = asyncio.run(call(guard, "GET", "/api/artifacts"))

        self.assertEqual(status_of(sent), 200)
        self.assertEqual(app.calls, 1)


class TestRateLimit(GuardCase):
    """失败限速：超阈值 → 429 + Retry-After；成功一次清零。"""

    def build_limited(self, **overrides):
        return self.build(max_failures=2, block_seconds=60, **overrides)

    def test_repeated_bad_token_becomes_429(self):
        guard = self.build_limited()

        self.run_call(guard, "GET", "/api/artifacts")
        self.run_call(guard, "GET", "/api/artifacts")
        sent = self.run_call(guard, "GET", "/api/artifacts")

        self.assertEqual(status_of(sent), 429)
        headers = dict(sent[0]["headers"])

        self.assertIn(b"retry-after", headers)

    def test_success_resets_failures(self):
        guard = self.build_limited()

        self.run_call(guard, "GET", "/api/artifacts")
        self.run_call(
            guard,
            "GET",
            "/api/artifacts",
            headers=[(b"authorization", f"Bearer {TOKEN}".encode())],
        )
        sent = self.run_call(guard, "GET", "/api/artifacts")

        self.assertEqual(status_of(sent), 401)

    def test_other_client_not_blocked(self):
        guard = self.build_limited()
        self.run_call(guard, "GET", "/api/artifacts")
        self.run_call(guard, "GET", "/api/artifacts")

        sent = self.run_call(
            guard,
            "GET",
            "/api/artifacts",
            headers=[(b"authorization", f"Bearer {TOKEN}".encode())],
            client=("10.0.0.10", 99),
        )

        self.assertEqual(status_of(sent), 200)
if __name__ == "__main__":
    unittest.main()
