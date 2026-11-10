"""事件解析测试（真实 schema 夹具）。"""

import json
import unittest

from pathlib import Path

from src.collab.qwen_events import (
    EventError,
    check_model,
    check_session,
    is_success,
    parse_events,
    usage_of,
)


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "qwen"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class ParseCase(unittest.TestCase):
    def test_success_fixture(self):
        parsed = parse_events(fixture("success.json"))

        self.assertEqual(parsed["init"]["subtype"], "init")
        self.assertEqual(parsed["result"]["subtype"], "success")
        self.assertFalse(parsed["result"]["is_error"])
        self.assertTrue(is_success(parsed))

    def test_missing_result(self):
        data = json.loads(fixture("success.json"))
        data = [event for event in data if event.get("type") != "result"]

        with self.assertRaises(EventError) as caught:
            parse_events(json.dumps(data))

        self.assertEqual(caught.exception.code, "missing_result")

    def test_invalid_json(self):
        with self.assertRaises(EventError) as caught:
            parse_events("{not json")

        self.assertEqual(caught.exception.code, "invalid_output")

    def test_non_array(self):
        with self.assertRaises(EventError):
            parse_events('{"type":"result"}')

    def test_duplicate_init(self):
        data = json.loads(fixture("success.json"))
        data.insert(1, data[0])

        with self.assertRaises(EventError) as caught:
            parse_events(json.dumps(data))

        self.assertEqual(caught.exception.code, "duplicate_event")

    def test_duplicate_result(self):
        data = json.loads(fixture("success.json"))
        data.append(data[-1])

        with self.assertRaises(EventError):
            parse_events(json.dumps(data))

    def test_session_mismatch(self):
        parsed = parse_events(fixture("success.json"))

        with self.assertRaises(EventError) as caught:
            check_session(parsed, "00000000-0000-0000-0000-000000000000")

        self.assertEqual(caught.exception.code, "session_mismatch")

    def test_model_mismatch(self):
        parsed = parse_events(fixture("success.json"))

        with self.assertRaises(EventError) as caught:
            check_model(parsed, "some-other-model")

        self.assertEqual(caught.exception.code, "model_mismatch")

    def test_model_fallback_fixture_detected(self):
        parsed = parse_events(fixture("model_fallback.json"))

        with self.assertRaises(EventError) as caught:
            check_model(parsed, "model-does-not-exist")

        self.assertEqual(caught.exception.code, "model_mismatch")
        self.assertEqual(parsed["init"]["model"], "qwen3.8-max")

    def test_usage_fields(self):
        parsed = parse_events(fixture("success.json"))
        usage = usage_of(parsed)

        self.assertIsInstance(usage["input_tokens"], int)
        self.assertIsInstance(usage["total_tokens"], int)

    def test_missing_usage_is_null(self):
        data = json.loads(fixture("success.json"))
        data[-1].pop("usage", None)
        parsed = parse_events(json.dumps(data))

        self.assertIsNone(usage_of(parsed)["input_tokens"])

    def test_is_error_false_required(self):
        data = json.loads(fixture("success.json"))
        data[-1]["is_error"] = True
        parsed = parse_events(json.dumps(data))

        self.assertFalse(is_success(parsed))



    def test_non_object_event_rejected(self):
        with self.assertRaises(EventError) as caught:
            parse_events('["not-an-event"]')

        self.assertEqual(caught.exception.code, "invalid_output")

    def test_one_side_session_missing(self):
        data = json.loads(fixture("success.json"))
        data[-1].pop("session_id", None)
        parsed = parse_events(json.dumps(data))

        with self.assertRaises(EventError) as caught:
            check_session(parsed, data[0]["session_id"])

        self.assertEqual(caught.exception.code, "session_mismatch")

    def test_invalid_usage_type(self):
        data = json.loads(fixture("success.json"))
        data[-1]["usage"]["input_tokens"] = "many"
        parsed = parse_events(json.dumps(data))

        with self.assertRaises(EventError) as caught:
            usage_of(parsed)

        self.assertEqual(caught.exception.code, "invalid_output")

    def test_result_before_init_rejected(self):
        data = json.loads(fixture("success.json"))
        reordered = [data[-1], data[0]] + data[1:-1]

        with self.assertRaises(EventError):
            parse_events(json.dumps(reordered))


if __name__ == "__main__":
    unittest.main()
