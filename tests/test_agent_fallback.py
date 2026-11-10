"""聊天模型 fallback 测试（全 mock，不联网）。"""

import unittest

from types import SimpleNamespace
from unittest.mock import patch

from src import agent
from src.core.run_control import ChatCancelled


class FakeClient:
    def __init__(self, outcomes):
        self.base_url = "https://api.deepseek.com"
        self.outcomes = list(outcomes)
        self.calls = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)

        if isinstance(outcome, Exception):
            raise outcome

        if callable(outcome):
            return outcome(kwargs)

        return outcome


class FakeUsage:
    def __init__(self):
        self.records = []

    def record_completion(self, **kwargs):
        self.records.append(kwargs)


class StatusError(RuntimeError):
    def __init__(self, status):
        super().__init__(f"status {status}")
        self.status_code = status


def response(text="ok", tool_calls=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=text, tool_calls=tool_calls or []),
            finish_reason="stop",
        )],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )


class BadStream:
    def __init__(self, error):
        self.error = error
        self.closed = False

    def __iter__(self):
        raise self.error
        yield

    def close(self):
        self.closed = True


class TextStream:
    def __init__(self, text, error=None):
        self.text = text
        self.error = error
        self.closed = False
        self.chunks = [
            SimpleNamespace(
                usage=None,
                choices=[SimpleNamespace(
                    delta=SimpleNamespace(content=text, tool_calls=None),
                    finish_reason=None,
                )],
            )
        ]

    def __iter__(self):
        for chunk in self.chunks:
            yield chunk

        if self.error is not None:
            raise self.error

    def close(self):
        self.closed = True


class FallbackCase(unittest.TestCase):
    def run_completion(self, outcomes, *, fallbacks=(), stream=False, callback=None):
        client = FakeClient(outcomes)

        with patch.object(agent, "client", client), patch.object(
            agent, "MODEL_FALLBACKS", list(fallbacks)
        ):
            return agent._completion(
                [{"role": "user", "content": "hi"}],
                "auto",
                tools=[],
                on_delta=callback,
            ), client

    def test_parse_fallbacks(self):
        self.assertEqual(
            agent._parse_model_fallbacks(" a, ,b,a,c ", "a"), ["b", "c"]
        )
        self.assertEqual(agent._parse_model_fallbacks("", "a"), [])
        self.assertEqual(agent._parse_model_fallbacks("a, b, c, d", "a"), ["b", "c"])

    def test_no_fallback_single_call(self):
        result = self.run_completion([response("ok")])[1].calls

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["model"], agent.MODEL)

    def test_timeout_then_fallback(self):
        output, client = self.run_completion(
            [TimeoutError("slow"), response("ok")], fallbacks=["backup"]
        )

        self.assertEqual([call["model"] for call in client.calls], [agent.MODEL, "backup"])
        self.assertTrue(output.choices)

    def test_rate_limit_then_fallback(self):
        _, client = self.run_completion(
            [StatusError(429), response("ok")], fallbacks=["backup"]
        )

        self.assertEqual(len(client.calls), 2)

    def test_three_failures_raise_last(self):
        with self.assertRaises(StatusError) as caught:
            self.run_completion(
                [StatusError(500), StatusError(502), StatusError(503)],
                fallbacks=["b1", "b2"],
            )

        self.assertEqual(caught.exception.status_code, 503)

    def test_fatal_not_switched(self):
        for error in (StatusError(400), StatusError(401), StatusError(403), StatusError(404), RuntimeError("x")):
            with self.assertRaises(type(error)):
                self.run_completion([error, response("never")], fallbacks=["backup"])

    def test_cancelled_not_switched(self):
        client = FakeClient([ChatCancelled("stop"), response("never")])

        with patch.object(agent, "client", client), patch.object(agent, "MODEL_FALLBACKS", ["backup"]):
            with self.assertRaises(ChatCancelled):
                agent._completion([], "auto", tools=[])

        self.assertEqual(len(client.calls), 1)

    def test_classifier_matrix(self):
        self.assertEqual(agent._classify_completion_error(TimeoutError())[0], "timeout")
        self.assertEqual(agent._classify_completion_error(StatusError(429))[0], "rate_limit")

        for status in (500, 502, 503, 504):
            self.assertTrue(agent._classify_completion_error(StatusError(status))[1])

        for status in (501, 418):
            self.assertFalse(agent._classify_completion_error(StatusError(status))[1])

    def test_kwargs_isolated_per_attempt(self):
        messages = [{"role": "user", "content": "hi"}]
        _, client = self.run_completion(
            [StatusError(500), response("ok")], fallbacks=["backup"]
        )

        first, second = client.calls
        self.assertEqual(first["messages"], messages)
        self.assertEqual(second["messages"], messages)
        self.assertNotIn("extra_body", second)
        self.assertEqual(second["model"], "backup")

    def test_thinking_only_for_flash(self):
        _, client = self.run_completion(
            [StatusError(500), response("ok")], fallbacks=["deepseek-chat"]
        )

        self.assertIn("extra_body", client.calls[0])
        self.assertNotIn("extra_body", client.calls[1])

    def test_stream_error_before_delta_falls_back(self):
        first = BadStream(TimeoutError("slow"))
        second = TextStream("ok")
        _, client = self.run_completion(
            [first, second], fallbacks=["backup"], stream=True, callback=lambda text: None
        )

        self.assertEqual(len(client.calls), 2)
        self.assertTrue(first.closed)

    def test_stream_text_then_error_no_fallback(self):
        stream = TextStream("partial", error=TimeoutError("slow"))
        client = FakeClient([stream, response("never")])
        seen = []

        with patch.object(agent, "client", client), patch.object(agent, "MODEL_FALLBACKS", ["backup"]):
            with self.assertRaises(TimeoutError):
                agent._completion([], "auto", tools=[], on_delta=seen.append)

        self.assertEqual(seen, ["partial"])
        self.assertEqual(len(client.calls), 1)

    def test_stream_tool_delta_then_error_no_fallback(self):
        stream = TextStream("", error=TimeoutError("slow"))
        stream.chunks[0].choices[0].delta = SimpleNamespace(
            content=None,
            tool_calls=[SimpleNamespace(
                index=0, id="1",
                function=SimpleNamespace(name="reminder", arguments="{}"),
            )],
        )
        client = FakeClient([stream, response("never")])

        with patch.object(agent, "client", client), patch.object(agent, "MODEL_FALLBACKS", ["backup"]):
            with self.assertRaises(TimeoutError):
                agent._completion([], "auto", tools=[], on_delta=lambda text: None)

        self.assertEqual(len(client.calls), 1)

    def test_usage_records_actual_model(self):
        usage = FakeUsage()

        with patch.object(agent, "_get_usage_store", return_value=usage):
            with agent.track_chat_usage("u", "r"):
                self.run_completion(
                    [StatusError(500), response("ok")], fallbacks=["backup"]
                )

        self.assertEqual(len(usage.records), 1)
        self.assertEqual(usage.records[0]["model"], "backup")

    def test_audit_written_on_switch(self):
        with self.assertLogs("personal_agent.usage", level="WARNING") as captured:
            self.run_completion(
                [StatusError(500), response("ok")], fallbacks=["backup"]
            )

        text = "\n".join(captured.output)
        self.assertIn("model_fallback", text)
        self.assertIn("error_class=server", text)


if __name__ == "__main__":
    unittest.main()
