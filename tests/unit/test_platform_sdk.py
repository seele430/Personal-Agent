"""Python SDK and language-neutral v1 fixture contract tests.

Every HTTP exchange is served by FixtureTransport. No listening socket,
real platform service, database, credential, or external network is used.
"""

import copy
import json
import sys
import unittest

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SDK_ROOT = ROOT / "sdk" / "python"
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from personal_agent_platform import (  # noqa: E402
    ApiError,
    ConflictError,
    GoneError,
    NotFoundError,
    PlatformClient,
    RateLimitError,
    TransportResponse,
    ValidationError,
    WaitTimeoutError,
)


FIXTURE_PATH = ROOT / "sdk" / "fixtures" / "v1" / "contracts.json"
REQUIRED_CASES = {
    "create_success",
    "exec_success",
    "exec_timed_out",
    "fn_success",
    "cancel_success",
    "destroy_success",
    "metrics_success",
    "wait_timeout_cancels",
    "error_invalid_idempotency_key",
    "error_idempotency_conflict",
    "error_sandbox_not_found",
    "error_sandbox_gone",
    "error_exec_not_cancellable",
    "error_rate_limited",
}


def load_document():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def load_case(name):
    return next(
        case
        for case in load_document()["cases"]
        if case["name"] == name
    )


class FixtureTransport:
    def __init__(self, exchanges):
        self.expected = list(exchanges)
        self.calls = []

    def request(
        self,
        method,
        url,
        *,
        headers,
        body,
        timeout_seconds,
    ):
        if not self.expected:
            raise AssertionError(f"Unexpected HTTP request: {method} {url}")
        exchange = self.expected.pop(0)
        expected = exchange["request"]
        actual_path = expected["path"]
        self.assert_equal(expected["method"], method, "method")
        self.assert_equal(
            f"http://platform.test{expected['path']}",
            url,
            "URL",
        )
        for name, value in expected.get("headers", {}).items():
            self.assert_equal(value, headers.get(name), f"header {name}")
        actual_json = json.loads(body.decode("utf-8")) if body else None
        self.assert_equal(expected.get("json"), actual_json, "JSON body")
        self.calls.append(
            {
                "method": method,
                "path": actual_path,
                "timeout_seconds": timeout_seconds,
            }
        )
        response = exchange["response"]
        if "json" in response:
            response_body = json.dumps(
                response["json"],
                ensure_ascii=False,
            ).encode("utf-8")
        else:
            response_body = response["text"].encode("utf-8")
        return TransportResponse(
            status_code=response["status"],
            headers={},
            body=response_body,
        )

    @staticmethod
    def assert_equal(expected, actual, label):
        if expected != actual:
            raise AssertionError(
                f"{label}: expected {expected!r}, got {actual!r}"
            )

    def assert_used(self, test_case):
        test_case.assertEqual([], self.expected)


def make_client(case, exchanges=None):
    transport = FixtureTransport(
        case["exchanges"] if exchanges is None else exchanges
    )
    client = PlatformClient(
        "http://platform.test",
        transport=transport,
        poll_interval_seconds=0,
    )
    return client, transport


def invoke(client, case, extra_input=None):
    arguments = copy.deepcopy(case["input"])
    if extra_input:
        arguments.update(extra_input)
    return getattr(client, case["operation"])(**arguments)


class TestPlatformSdkV1Fixtures(unittest.TestCase):
    def test_v1_success_fixtures(self):
        cases = [
            case
            for case in load_document()["cases"]
            if case["kind"] == "success"
        ]
        for case in cases:
            with self.subTest(case=case["name"]):
                client, transport = make_client(case)
                result = invoke(client, case)
                expected = case["expected"]
                if expected["model"] == "text":
                    self.assertEqual(
                        case["exchanges"][-1]["response"]["text"],
                        result,
                    )
                else:
                    for field, value in expected.items():
                        if field != "model":
                            self.assertEqual(value, getattr(result, field))
                transport.assert_used(self)

    def test_v1_api_error_fixtures(self):
        error_types = {
            400: ValidationError,
            404: NotFoundError,
            409: ConflictError,
            410: GoneError,
            429: RateLimitError,
        }
        cases = [
            case
            for case in load_document()["cases"]
            if case["kind"] == "api_error"
        ]
        for case in cases:
            with self.subTest(case=case["name"]):
                client, transport = make_client(case)
                with self.assertRaises(ApiError) as caught:
                    invoke(client, case)
                error = caught.exception
                expected = case["expected"]
                self.assertIsInstance(
                    error,
                    error_types[expected["status_code"]],
                )
                self.assertEqual(expected["code"], error.code)
                self.assertEqual(
                    expected["status_code"],
                    error.status_code,
                )
                self.assertEqual(
                    expected["request_id"],
                    error.request_id,
                )
                self.assertEqual(
                    expected["retryable"],
                    error.retryable,
                )
                transport.assert_used(self)

    def test_wait_timeout_cancels_exec(self):
        case = load_case("wait_timeout_cancels")
        client, transport = make_client(case)
        with self.assertRaises(WaitTimeoutError) as caught:
            invoke(client, case)
        error = caught.exception
        self.assertEqual("SDK_WAIT_TIMEOUT", error.code)
        self.assertEqual("exec_wait", error.exec_id)
        self.assertIsNotNone(error.cancel_result)
        self.assertEqual("cancelled", error.cancel_result.state)
        self.assertIsNone(error.cancel_error)
        transport.assert_used(self)

    def test_wait_timeout_can_stop_without_cancelling(self):
        case = load_case("wait_timeout_cancels")
        client, transport = make_client(
            case,
            exchanges=case["exchanges"][:1],
        )
        with self.assertRaises(WaitTimeoutError) as caught:
            invoke(
                client,
                case,
                {"cancel_on_wait_timeout": False},
            )
        self.assertIsNone(caught.exception.cancel_result)
        self.assertIsNone(caught.exception.cancel_error)
        transport.assert_used(self)

    def test_wait_false_returns_accepted_exec(self):
        case = load_case("exec_success")
        client, transport = make_client(
            case,
            exchanges=case["exchanges"][:1],
        )
        result = invoke(client, case, {"wait": False})
        self.assertEqual("queued", result.state)
        self.assertFalse(result.terminal)
        transport.assert_used(self)

    def test_unknown_optional_response_fields_are_ignored(self):
        case = copy.deepcopy(load_case("create_success"))
        case["exchanges"][0]["response"]["json"][
            "future_optional_field"
        ] = {"nested": [1, 2, 3]}
        client, transport = make_client(case)
        result = invoke(client, case)
        self.assertEqual("sb_001", result.sandbox_id)
        transport.assert_used(self)

    def test_fixture_coverage_and_shape(self):
        document = load_document()
        self.assertEqual(1, document["schema_version"])
        names = [case["name"] for case in document["cases"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(REQUIRED_CASES.issubset(names))
        for case in document["cases"]:
            with self.subTest(case=case["name"]):
                self.assertIn(
                    case["kind"],
                    {"success", "wait_timeout", "api_error"},
                )
                self.assertIsInstance(case["input"], dict)
                self.assertTrue(case["exchanges"])
                for exchange in case["exchanges"]:
                    request = exchange["request"]
                    self.assertIn(request["method"], {"GET", "POST"})
                    self.assertTrue(
                        request["path"].startswith("/v1/")
                        or request["path"] == "/metrics"
                    )
                    self.assertIsInstance(
                        exchange["response"]["status"],
                        int,
                    )

    def test_invalid_limits_do_not_send_requests(self):
        case = load_case("exec_success")
        client, transport = make_client(case, exchanges=[])
        for timeout_ms, max_output_bytes in (
            (0, 65536),
            (300001, 65536),
            (30000, 0),
            (30000, 1048577),
        ):
            with self.subTest(
                timeout_ms=timeout_ms,
                max_output_bytes=max_output_bytes,
            ):
                arguments = copy.deepcopy(case["input"])
                arguments["timeout_ms"] = timeout_ms
                arguments["max_output_bytes"] = max_output_bytes
                with self.assertRaises(ValueError):
                    client.exec(**arguments)
        arguments = copy.deepcopy(case["input"])
        arguments["wait_timeout_seconds"] = -1
        with self.assertRaises(ValueError):
            client.exec(**arguments)
        transport.assert_used(self)


if __name__ == "__main__":
    unittest.main()
