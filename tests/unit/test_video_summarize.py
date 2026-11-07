"""分段摘要测试：时间戳必须来自转写，不许模型自编。"""

import unittest

from src.video.asr import Segment, Transcript
from src.video.summarize import (
    CHUNK_SEGMENTS,
    SummarizeError,
    build_chunk_messages,
    chunk_segments,
    parse_citations,
    points_from_payload,
    render_points,
    summarize,
)


def segments(count, *, step=2.0):
    return tuple(
        Segment(start=index * step, end=index * step + step, text=f"s{index}")
        for index in range(count)
    )


def transcript(count):
    return Transcript(language="en", segments=segments(count))


class TestChunking(unittest.TestCase):
    def test_splits_by_size(self):
        chunks = chunk_segments(segments(20))

        self.assertEqual(len(chunks), 3)
        self.assertEqual(chunks[0][0], 0)
        self.assertEqual(len(chunks[0][1]), CHUNK_SEGMENTS)

    def test_caps_chunk_count(self):
        chunks = chunk_segments(segments(200), size=5, max_chunks=4)

        self.assertEqual(len(chunks), 4)

    def test_invalid_size(self):
        with self.assertRaises(ValueError):
            chunk_segments(segments(3), size=0)

    def test_prompt_contains_segment_ids(self):
        chunk = chunk_segments(segments(3))[0]
        messages = build_chunk_messages(chunk, index=0, total=1)

        self.assertIn("[0]", messages[1]["content"])
        self.assertIn("不要写时间", messages[0]["content"])


class TestPointsFromPayload(unittest.TestCase):
    def setUp(self):
        self.segments = segments(5)
        self.chunk = (0, self.segments)

    def test_maps_indices_to_timestamps(self):
        points = points_from_payload(
            {"points": [{"text": "要点", "segments": [1, 2]}]},
            chunk=self.chunk,
            segments=self.segments,
        )

        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].start, 2.0)
        self.assertEqual(points[0].end, 6.0)

    def test_drops_points_without_segments(self):
        points = points_from_payload(
            {"points": [{"text": "没段号"}]}, chunk=self.chunk, segments=self.segments
        )

        self.assertEqual(points, [])

    def test_drops_out_of_range_indices(self):
        points = points_from_payload(
            {"points": [{"text": "越界", "segments": [99]}]},
            chunk=self.chunk,
            segments=self.segments,
        )

        self.assertEqual(points, [])

    def test_rejects_non_object(self):
        with self.assertRaises(SummarizeError):
            points_from_payload("nope", chunk=self.chunk, segments=self.segments)

    def test_rejects_missing_points_array(self):
        with self.assertRaises(SummarizeError):
            points_from_payload({}, chunk=self.chunk, segments=self.segments)

    def test_truncates_long_text(self):
        points = points_from_payload(
            {"points": [{"text": "长" * 400, "segments": [0]}]},
            chunk=self.chunk,
            segments=self.segments,
        )

        self.assertLessEqual(len(points[0].text), 160)


class TestSummarize(unittest.TestCase):
    def test_dedupes_and_caps(self):
        calls = []

        def completer(messages, chunk):
            calls.append(chunk)
            offset, items = chunk

            return {
                "points": [
                    {"text": "重复要点", "segments": [offset]},
                    {"text": "重复要点", "segments": [offset + 1]},
                ]
            }

        result = summarize(transcript(20), completer=completer, max_points=5)

        self.assertEqual(len(result), 1)
        self.assertEqual(len(calls), 3)

    def test_caps_total_points(self):
        def completer(messages, chunk):
            offset, items = chunk

            return {
                "points": [
                    {"text": f"第{offset + index}条", "segments": [offset + index]}
                    for index in range(len(items))
                ]
            }

        result = summarize(transcript(40), completer=completer, max_points=4)

        self.assertEqual(len(result), 4)

    def test_empty_transcript_rejected(self):
        with self.assertRaises(SummarizeError):
            summarize(Transcript(language="en", segments=()))

    def test_render_points(self):
        def completer(messages, chunk):
            return {"points": [{"text": "一条", "segments": [0]}]}

        result = summarize(transcript(3), completer=completer)
        rendered = render_points(result)

        self.assertIn("一条", rendered)
        self.assertIn("[0.0–2.0]", rendered)


class TestCitations(unittest.TestCase):
    def test_parses_single_and_range(self):
        self.assertEqual(parse_citations("见 [12] 与 [13-15]"), (12, 13))

    def test_empty_text(self):
        self.assertEqual(parse_citations(""), ())

    def test_ignores_non_numeric(self):
        self.assertEqual(parse_citations("[abc] [ ]"), ())


class TestUsageSink(unittest.TestCase):
    """用量只由真实 LLM 调用产生；注入 completer 时不编数字。"""

    def test_injected_completer_reports_no_usage(self):
        seen = []

        def completer(messages, chunk):
            return {"points": [{"text": "x", "segments": [0]}]}

        summarize(transcript(3), completer=completer, usage_sink=seen.append)

        self.assertEqual(seen, [])

    def test_sink_is_optional(self):
        def completer(messages, chunk):
            return {"points": [{"text": "x", "segments": [0]}]}

        self.assertEqual(len(summarize(transcript(3), completer=completer)), 1)
class TestRealBranchUsage(unittest.TestCase):
    """真实 LLM 分支：用量必须回传，且与 total 自洽。"""

    def test_usage_is_forwarded(self):
        from unittest.mock import patch

        class Usage:
            prompt_tokens = 120
            completion_tokens = 30
            total_tokens = 150

        class Call:
            usage = Usage()

        payload = {"points": [{"text": "要点", "segments": [0]}]}
        seen = []

        with patch(
            "src.research.llm.complete_json",
            return_value=(payload, Call()),
        ):
            summarize(transcript(3), usage_sink=seen.append)

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].total_tokens, 150)
        self.assertEqual(
            seen[0].prompt_tokens + seen[0].completion_tokens,
            seen[0].total_tokens,
        )
class TestLongVideoCoverage(unittest.TestCase):
    """长转写不能被静默截断：60 段 = 8 块，必须全部处理。"""

    def test_all_chunks_processed(self):
        calls = []

        def completer(messages, chunk):
            calls.append(chunk)

            return {"points": []}

        summarize(transcript(60), completer=completer)

        self.assertEqual(len(calls), 8)

    def test_truncation_callback_not_called_when_under_limit(self):
        seen = []

        def completer(messages, chunk):
            return {"points": [{"text": "x", "segments": [chunk[0]]}]}

        summarize(transcript(20), completer=completer, on_truncated=seen.append)

        self.assertEqual(seen, [])

    def test_truncation_callback_fires_over_limit(self):
        seen = []
        calls = []

        def completer(messages, chunk):
            calls.append(1)

            return {"points": []}

        # 300 段 = 38 块 > 30 上限
        summarize(transcript(300), completer=completer, on_truncated=seen.append)

        self.assertEqual(len(calls), 30)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["total_chunks"], 38)
        self.assertEqual(seen[0]["used_chunks"], 30)

    def test_points_still_capped(self):
        def completer(messages, chunk):
            offset, items = chunk

            return {
                "points": [
                    {"text": f"p{offset + i}", "segments": [offset + i]}
                    for i in range(len(items))
                ]
            }

        result = summarize(transcript(120), completer=completer, max_points=20)

        self.assertEqual(len(result), 20)
class TestPointSpread(unittest.TestCase):
    """要点上限不能变成"只摘要前半段"：所有块跑完再等距抽样。"""

    def test_all_chunks_called_even_if_many_points(self):
        calls = []

        def completer(messages, chunk):
            calls.append(1)
            offset, items = chunk

            return {
                "points": [
                    {"text": f"p{offset + i}", "segments": [offset + i]}
                    for i in range(len(items))
                ]
            }

        result = summarize(transcript(40), completer=completer, max_points=3)

        self.assertEqual(len(calls), 5)
        self.assertLessEqual(len(result), 3)

    def test_spread_keeps_last_chunk(self):
        def completer(messages, chunk):
            offset, items = chunk

            return {
                "points": [
                    {"text": f"p{offset + i}", "segments": [offset + i]}
                    for i in range(len(items))
                ]
            }

        result = summarize(transcript(40), completer=completer, max_points=3)
        texts = [point.text for point in result]

        self.assertTrue(any(text in ("p0", "p1", "p2", "p3") for text in texts))
        self.assertTrue(any(text.startswith("p3") for text in texts))

    def test_small_count_is_unchanged(self):
        def completer(messages, chunk):
            offset, items = chunk

            return {"points": [{"text": f"x{offset}", "segments": [offset]}]}

        result = summarize(transcript(20), completer=completer, max_points=20)

        self.assertEqual(len(result), 3)

    def test_points_stay_sorted_by_time(self):
        def completer(messages, chunk):
            offset, items = chunk

            return {"points": [{"text": f"t{offset}", "segments": [offset]}]}

        result = summarize(transcript(40), completer=completer, max_points=3)
        starts = [point.start for point in result]

        self.assertEqual(starts, sorted(starts))
if __name__ == "__main__":
    unittest.main()
