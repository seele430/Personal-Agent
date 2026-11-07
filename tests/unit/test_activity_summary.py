"""中文活动摘要卡测试：计数、耗时、文案、空态。"""

import unittest

from src.activity.summary import (
    EMPTY_TEXT,
    format_duration,
    summarize_spans,
    summarize_trace,
)


def span(name, *, start=1000.0, dur=100.0, status="ok"):
    return {
        "name": name,
        "start_ms": start,
        "dur_ms": dur,
        "status": status,
    }


class TestEmpty(unittest.TestCase):
    def test_empty_returns_placeholder(self):
        summary = summarize_spans([], trace_id="t1")

        self.assertEqual(summary.text, EMPTY_TEXT)
        self.assertEqual(summary.span_count, 0)
        self.assertEqual(summary.duration_ms, 0.0)
        self.assertTrue(summary.ok)

    def test_non_list_is_rejected(self):
        with self.assertRaises(TypeError):
            summarize_spans("nope")

    def test_empty_dict_shape(self):
        payload = summarize_spans([]).to_dict()

        self.assertEqual(payload["text"], EMPTY_TEXT)
        self.assertEqual(payload["llm_calls"], 0)
        self.assertEqual(payload["error_count"], 0)


class TestCounting(unittest.TestCase):
    def test_counts_llm_calls(self):
        summary = summarize_spans([span("llm.generate"), span("llm.generate")])

        self.assertEqual(summary.llm_calls, 2)
        self.assertEqual(summary.span_count, 2)
        self.assertIn("2 次模型调用", summary.text)

    def test_counts_tools_and_commands(self):
        summary = summarize_spans(
            [span("tool.sandbox_exec"), span("exec.run"), span("tts.synth")]
        )

        self.assertEqual(summary.tool_calls, 1)
        self.assertEqual(summary.command_calls, 1)
        self.assertEqual(summary.tts_calls, 1)
        self.assertEqual(summary.other_calls, 0)

    def test_unknown_names_bucket_as_other(self):
        summary = summarize_spans([span("routing.decide"), span("dispatch.plan")])

        self.assertEqual(summary.other_calls, 2)
        self.assertIn("2 个其他步骤", summary.text)

    def test_all_buckets_in_one_text(self):
        summary = summarize_spans(
            [
                span("llm.generate"),
                span("tool.x"),
                span("exec.run"),
                span("tts.synth"),
                span("misc.thing"),
            ]
        )

        for fragment in ("1 次模型调用", "1 次工具调用", "1 条命令", "1 次语音合成", "1 个其他步骤"):
            self.assertIn(fragment, summary.text)


class TestDuration(unittest.TestCase):
    def test_duration_spans_first_start_to_last_end(self):
        summary = summarize_spans(
            [
                span("a", start=1000.0, dur=100.0),
                span("b", start=1050.0, dur=200.0),
            ]
        )

        self.assertAlmostEqual(summary.duration_ms, 250.0)
        self.assertIn("毫秒", summary.text)

    def test_missing_timing_yields_zero(self):
        summary = summarize_spans([{"name": "x", "status": "ok"}])

        self.assertEqual(summary.duration_ms, 0.0)

    def test_format_duration_units(self):
        self.assertEqual(format_duration(500), "500 毫秒")
        self.assertEqual(format_duration(1500), "1.5 秒")
        self.assertEqual(format_duration(65_000), "1 分 5 秒")

    def test_format_duration_rejects_invalid(self):
        for bad in (-1, "x", None):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    format_duration(bad)


class TestStatus(unittest.TestCase):
    def test_all_ok_phrase(self):
        summary = summarize_spans([span("llm.generate")])

        self.assertIn("全部成功", summary.text)
        self.assertTrue(summary.ok)

    def test_errors_are_counted_and_reported(self):
        summary = summarize_spans(
            [span("llm.generate", status="error"), span("exec.run")]
        )

        self.assertEqual(summary.error_count, 1)
        self.assertFalse(summary.ok)
        self.assertIn("1 个步骤失败", summary.text)


class FakeStore:
    def __init__(self, spans):
        self.spans = spans

    def fetch_trace(self, trace_id):
        return self.spans


class TestSummarizeTrace(unittest.TestCase):
    def test_uses_store_spans(self):
        summary = summarize_trace("t-1", store=FakeStore([span("llm.generate")]))

        self.assertEqual(summary.trace_id, "t-1")
        self.assertEqual(summary.llm_calls, 1)

    def test_trace_id_is_required(self):
        for bad in ("", "   ", None):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    summarize_trace(bad, store=FakeStore([]))

    def test_store_failure_degrades_to_empty(self):
        class Broken:
            def fetch_trace(self, trace_id):
                raise RuntimeError("db down")

        summary = summarize_trace("t-2", store=Broken())

        self.assertEqual(summary.text, EMPTY_TEXT)

    def test_to_dict_rounds_duration(self):
        payload = summarize_trace(
            "t-3", store=FakeStore([span("llm.generate", dur=123.456)])
        ).to_dict()

        self.assertEqual(payload["duration_ms"], 123.5)
        self.assertEqual(payload["trace_id"], "t-3")


if __name__ == "__main__":
    unittest.main()
