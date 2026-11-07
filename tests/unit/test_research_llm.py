"""研究侧模型调用层单元测试。

完全离线：completer 与 sleep 都由用例注入，既不发请求也不真的等。
"""

import unittest

from src.research.llm import (
    LLMAuthError,
    LLMInvalidResponse,
    LLMRateLimited,
    LLMTimeout,
    LLMUnavailable,
    MAX_OUTPUT_TOKENS,
    complete,
    complete_json,
    parse_json_object,
)


class FakeUsage:
    def __init__(self, prompt=10, completion=20, total=30):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = total


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeChoice:
    def __init__(self, content):
        self.message = FakeMessage(content)


class FakeResponse:
    def __init__(self, content, usage=None):
        self.choices = [FakeChoice(content)]
        self.usage = usage if usage is not None else FakeUsage()


class FakeStatusError(Exception):
    def __init__(self, status_code, message="boom"):
        super().__init__(message)
        self.status_code = status_code


class FakeTimeout(Exception):
    """类名里带 Timeout，模拟 SDK 的超时异常。"""


def completer_returning(*results):
    """按顺序返回若干响应或抛异常，并记录调用参数。"""

    calls = []

    def completer(messages, params):
        calls.append({"messages": messages, "params": params})
        item = results[len(calls) - 1]

        if isinstance(item, Exception):
            raise item

        return item

    completer.calls = calls

    return completer


def no_sleep(record):
    def sleep(seconds):
        record.append(seconds)

    return sleep


MESSAGES = [{"role": "user", "content": "写一句话"}]


class TestComplete(unittest.TestCase):
    def test_returns_text_usage_and_latency(self):
        completer = completer_returning(FakeResponse("结果"))

        call = complete(MESSAGES, completer=completer, sleep=no_sleep([]))

        self.assertEqual(call.text, "结果")
        self.assertEqual(call.usage.prompt_tokens, 10)
        self.assertEqual(call.usage.completion_tokens, 20)
        self.assertEqual(call.usage.total_tokens, 30)
        self.assertEqual(call.attempts, 1)
        self.assertGreaterEqual(call.latency_ms, 0.0)
        self.assertEqual(
            completer.calls[0]["params"]["max_tokens"], MAX_OUTPUT_TOKENS
        )
        self.assertEqual(completer.calls[0]["params"]["temperature"], 0.0)

    def test_missing_usage_becomes_zero(self):
        class NoUsage:
            choices = [FakeChoice("x")]

        call = complete(
            MESSAGES, completer=completer_returning(NoUsage()), sleep=no_sleep([])
        )

        self.assertEqual(call.usage.total_tokens, 0)

    def test_message_validation(self):
        for bad in ([], "x", [{"role": "tool", "content": "x"}], [{"role": "user", "content": "  "}]):
            with self.subTest(messages=bad):
                with self.assertRaises(ValueError):
                    complete(bad, completer=completer_returning(), sleep=no_sleep([]))

    def test_too_many_messages(self):
        messages = [{"role": "user", "content": "x"}] * 25

        with self.assertRaises(ValueError):
            complete(messages, completer=completer_returning(), sleep=no_sleep([]))

    def test_parameter_bounds(self):
        bad_params = (
            {"max_tokens": 0},
            {"max_tokens": MAX_OUTPUT_TOKENS + 1},
            {"max_tokens": True},
            {"temperature": -0.1},
            {"temperature": 2.0},
            {"timeout": 0},
            {"timeout": 999},
            {"max_retries": 3},
        )

        for params in bad_params:
            with self.subTest(params=params):
                with self.assertRaises(ValueError):
                    complete(
                        MESSAGES,
                        completer=completer_returning(),
                        sleep=no_sleep([]),
                        **params,
                    )


class TestErrorClassification(unittest.TestCase):
    def test_auth_error_is_not_retried(self):
        completer = completer_returning(FakeStatusError(401))

        with self.assertRaises(LLMAuthError):
            complete(MESSAGES, completer=completer, sleep=no_sleep([]))

        self.assertEqual(len(completer.calls), 1)

    def test_rate_limit_is_retried_once_then_succeeds(self):
        sleeps = []
        completer = completer_returning(
            FakeStatusError(429), FakeResponse("第二次成功")
        )

        call = complete(MESSAGES, completer=completer, sleep=no_sleep(sleeps))

        self.assertEqual(call.text, "第二次成功")
        self.assertEqual(call.attempts, 2)
        self.assertEqual(len(sleeps), 1)

    def test_rate_limit_exhausts_retries(self):
        sleeps = []
        completer = completer_returning(
            FakeStatusError(429), FakeStatusError(429)
        )

        with self.assertRaises(LLMRateLimited):
            complete(MESSAGES, completer=completer, sleep=no_sleep(sleeps))

        self.assertEqual(len(completer.calls), 2)

    def test_server_error_is_unavailable(self):
        completer = completer_returning(FakeStatusError(503))

        with self.assertRaises(LLMUnavailable):
            complete(MESSAGES, completer=completer, sleep=no_sleep([]))

    def test_timeout_is_its_own_error(self):
        completer = completer_returning(FakeTimeout("slow"), FakeTimeout("slow"))

        with self.assertRaises(LLMTimeout):
            complete(MESSAGES, completer=completer, sleep=no_sleep([]))

    def test_connection_error_is_unavailable(self):
        class FakeConnectionError(Exception):
            pass

        completer = completer_returning(FakeConnectionError("reset"))

        with self.assertRaises(LLMUnavailable):
            complete(MESSAGES, completer=completer, sleep=no_sleep([]))

    def test_malformed_response_is_invalid(self):
        class NoChoices:
            choices = []
            usage = None

        completer = completer_returning(NoChoices())

        with self.assertRaises(LLMInvalidResponse):
            complete(MESSAGES, completer=completer, sleep=no_sleep([]))

    def test_retries_can_be_disabled(self):
        completer = completer_returning(FakeStatusError(429))

        with self.assertRaises(LLMRateLimited):
            complete(
                MESSAGES,
                max_retries=0,
                completer=completer,
                sleep=no_sleep([]),
            )

        self.assertEqual(len(completer.calls), 1)


class TestJsonParsing(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(parse_json_object('{"a": 1}'), {"a": 1})

    def test_fenced_object(self):
        text = '```json\n{"a": 1, "b": [2]}\n```'

        self.assertEqual(parse_json_object(text), {"a": 1, "b": [2]})

    def test_invalid_inputs(self):
        for bad in ("", "   ", "not json", "[1, 2]", "null", '{"a": }'):
            with self.subTest(text=bad):
                with self.assertRaises(LLMInvalidResponse):
                    parse_json_object(bad)

    def test_non_string_input(self):
        with self.assertRaises(LLMInvalidResponse):
            parse_json_object(None)

    def test_complete_json_returns_payload_and_usage(self):
        completer = completer_returning(FakeResponse('{"score": 8}'))

        payload, call = complete_json(
            MESSAGES, completer=completer, sleep=no_sleep([])
        )

        self.assertEqual(payload, {"score": 8})
        self.assertEqual(call.usage.total_tokens, 30)


if __name__ == "__main__":
    unittest.main()
