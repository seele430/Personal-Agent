"""D25 /v1 认证：配了令牌就必须带 Bearer，缺/错一律 401。"""

from __future__ import annotations

import unittest

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.main import app
from tests.helper import isolate_paths

TOKEN = "d25-platform-token"


class PlatformAuthTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_paths(self, self.temp)

        patcher = patch.dict(
            "os.environ",
            {
                "PERSONAL_AGENT_SANDBOX_ROOT": str(
                    Path(self.temp.name) / "sandboxes"
                ),
                "PERSONAL_AGENT_PLATFORM_REAPER": "0",
            },
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def create(self, headers=None):
        return self.client.post(
            "/v1/sandboxes",
            json={
                "tier": "tier1",
                "workspace_id": "ws_auth",
                "ttl_seconds": 600,
            },
            headers=headers or {"Idempotency-Key": "auth-key"},
        )

    # ------------------------------------------------------------------

    def test_missing_token_is_rejected(self):
        with patch.dict("os.environ", {"PERSONAL_AGENT_PLATFORM_TOKEN": TOKEN}):
            response = self.create()

        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.json()["error"]["code"], "UNAUTHORIZED")
        self.assertFalse(response.json()["error"]["retryable"])
        self.assertEqual(response.headers.get("www-authenticate"), "Bearer")
        # 令牌本身不能出现在响应里
        self.assertNotIn(TOKEN, response.text)

    def test_wrong_token_is_rejected(self):
        with patch.dict("os.environ", {"PERSONAL_AGENT_PLATFORM_TOKEN": TOKEN}):
            response = self.create(
                headers={
                    "Idempotency-Key": "auth-key",
                    "Authorization": "Bearer wrong-token",
                }
            )

        self.assertEqual(response.status_code, 401, response.text)

    def test_non_bearer_scheme_is_rejected(self):
        with patch.dict("os.environ", {"PERSONAL_AGENT_PLATFORM_TOKEN": TOKEN}):
            response = self.create(
                headers={
                    "Idempotency-Key": "auth-key",
                    "Authorization": f"Basic {TOKEN}",
                }
            )

        self.assertEqual(response.status_code, 401, response.text)

    def test_valid_token_is_accepted(self):
        with patch.dict("os.environ", {"PERSONAL_AGENT_PLATFORM_TOKEN": TOKEN}):
            created = self.create(
                headers={
                    "Idempotency-Key": "auth-key",
                    "Authorization": f"Bearer {TOKEN}",
                }
            )

            self.assertEqual(created.status_code, 201, created.text)
            sandbox_id = created.json()["sandbox_id"]

            fetched = self.client.get(
                f"/v1/sandboxes/{sandbox_id}",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )

        self.assertEqual(fetched.status_code, 200, fetched.text)

    def test_exec_also_requires_token(self):
        with patch.dict("os.environ", {"PERSONAL_AGENT_PLATFORM_TOKEN": TOKEN}):
            created = self.create(
                headers={
                    "Idempotency-Key": "auth-key",
                    "Authorization": f"Bearer {TOKEN}",
                }
            )
            sandbox_id = created.json()["sandbox_id"]

            blocked = self.client.post(
                f"/v1/sandboxes/{sandbox_id}/exec",
                json={"command": ["cmd", "/c", "echo", "hi"]},
            )

        self.assertEqual(blocked.status_code, 401, blocked.text)

    def test_unset_token_rejects_writes_by_default(self):
        """R6：未配令牌时，/v1 写请求默认拒绝（不再等于本机开放）。"""

        with patch.dict(
            "os.environ",
            {
                "PERSONAL_AGENT_PLATFORM_TOKEN": "",
                "PERSONAL_AGENT_ALLOW_ANON_V1": "0",
            },
        ):
            blocked = self.create()
            read = self.client.get("/v1/queue")

        self.assertEqual(blocked.status_code, 401, blocked.text)
        self.assertEqual(blocked.json()["error"]["code"], "UNAUTHORIZED")
        self.assertIn("ALLOW_ANON_V1", blocked.json()["error"]["message"])

        # 读请求沿用旧行为（只监听本机的边界仍在）
        self.assertEqual(read.status_code, 200, read.text)

    def test_unset_token_allows_writes_with_explicit_flag(self):
        """显式 PERSONAL_AGENT_ALLOW_ANON_V1=1 时才恢复匿名可写。"""

        with patch.dict(
            "os.environ",
            {
                "PERSONAL_AGENT_PLATFORM_TOKEN": "",
                "PERSONAL_AGENT_ALLOW_ANON_V1": "1",
            },
        ):
            response = self.create()

        self.assertEqual(response.status_code, 201, response.text)

    def test_health_is_not_guarded(self):
        with patch.dict("os.environ", {"PERSONAL_AGENT_PLATFORM_TOKEN": TOKEN}):
            response = self.client.get("/health")

        self.assertEqual(response.status_code, 200, response.text)

    def test_error_envelope_shape(self):
        with patch.dict("os.environ", {"PERSONAL_AGENT_PLATFORM_TOKEN": TOKEN}):
            error = self.create().json()["error"]

        self.assertEqual(
            set(error), {"code", "message", "retryable", "request_id"}
        )


class SdkTokenTest(unittest.TestCase):
    """SDK 侧也要真的带上 Bearer。"""

    def test_client_sends_bearer_token(self):
        import json
        import sys

        sdk_root = (
            Path(__file__).resolve().parents[2] / "sdk" / "python"
        )

        if str(sdk_root) not in sys.path:
            sys.path.insert(0, str(sdk_root))

        from personal_agent_platform import PlatformClient, TransportResponse

        captured: dict = {}

        class Capture:
            def request(self, method, url, *, headers, body, timeout_seconds):
                captured["headers"] = dict(headers)
                payload = {
                    "sandbox_id": "sbx_" + "0" * 32,
                    "state": "created",
                    "node_id": "node_local",
                    "workspace_id": "ws_auth",
                    "created_at": "2026-11-04T00:00:00+00:00",
                    "expires_at": "2026-11-04T00:10:00+00:00",
                    "ttl_seconds": 600,
                }
                return TransportResponse(
                    status_code=201,
                    headers={},
                    body=json.dumps(payload).encode("utf-8"),
                )

        client = PlatformClient(
            "http://platform.test", transport=Capture(), token=TOKEN
        )
        client.create(
            tier="tier1",
            workspace_id="ws_auth",
            ttl_seconds=600,
            idempotency_key="auth-sdk",
        )

        self.assertEqual(
            captured["headers"].get("Authorization"), f"Bearer {TOKEN}"
        )


if __name__ == "__main__":
    unittest.main()
