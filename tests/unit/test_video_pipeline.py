"""管线测试：成功路径 + 三种"无法验证" + 抽帧失败可容忍。"""

import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from src.video.asr import Segment, Transcript
from src.video.pipeline import render_report, summarize_video
from src.video.probe import StreamInfo, VideoInfo, VideoUnverified


def video_info():
    return VideoInfo(
        path=Path("demo.mp4"),
        duration_s=60.0,
        size_bytes=1024,
        streams=(
            StreamInfo("video", "h264"),
            StreamInfo("audio", "aac", 2, 44100),
        ),
    )


def transcript(count=12, *, text="这是一段有实际内容的解说词，用来验证管线。"):
    segments = tuple(
        Segment(start=index * 2.0, end=index * 2.0 + 2.0, text=text)
        for index in range(count)
    )

    return Transcript(language="zh", segments=segments)


class FakeTranscriber:
    name = "fake"

    def __init__(self, result):
        self.result = result

    def transcribe(self, path):
        return self.result


def fake_extractor(video, *, out_dir, duration_s):
    return type("Track", (), {"path": Path(out_dir) / "a.wav", "duration_s": duration_s, "size_bytes": 1})()


def completer(messages, chunk):
    offset, segments = chunk

    return {
        "points": [
            {"text": f"要点 {offset + index}", "segments": [offset + index]}
            for index in range(min(2, len(segments)))
        ]
    }


class PipelineCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.out = Path(self._temporary.name) / "out"

    def run_pipeline(self, *, transcriber=None, keyframes=None, probe_result=None):
        with patch("src.video.pipeline.probe", return_value=probe_result or video_info()):
            return summarize_video(
                "demo.mp4",
                transcriber=transcriber or FakeTranscriber(transcript()),
                completer=completer,
                out_dir=self.out,
                extractor=fake_extractor,
                keyframe_extractor=keyframes or (lambda video, target: []),
            )


class TestSuccess(PipelineCase):
    def test_builds_points_with_timestamps(self):
        summary = self.run_pipeline()

        self.assertTrue(summary.verified)
        self.assertGreaterEqual(len(summary.points), 2)
        self.assertEqual(summary.points[0].start, 0.0)

    def test_out_dir_created(self):
        self.run_pipeline()

        self.assertTrue(self.out.is_dir())

    def test_generated_at_present(self):
        summary = self.run_pipeline()

        self.assertIn("T", summary.generated_at)

    def test_serializes(self):
        payload = self.run_pipeline().to_dict()

        self.assertTrue(payload["verified"])
        self.assertEqual(payload["video"]["duration_s"], 60.0)
        self.assertIn("points", payload)

    def test_report_has_points_and_transcript(self):
        report = render_report(self.run_pipeline())

        self.assertIn("## 要点", report)
        self.assertIn("## 转写", report)
        self.assertIn("[0.0–2.0]", report)


class TestUnverified(PipelineCase):
    def test_no_audio_raises(self):
        with patch("src.video.pipeline.probe", side_effect=VideoUnverified("视频没有音轨")):
            with self.assertRaises(VideoUnverified):
                summarize_video(
                    "a.webm",
                    transcriber=FakeTranscriber(transcript()),
                    completer=completer,
                    out_dir=self.out,
                    extractor=fake_extractor,
                    keyframe_extractor=lambda video, target: [],
                )

    def test_short_speech_raises_with_count(self):
        with self.assertRaises(VideoUnverified) as caught:
            self.run_pipeline(transcriber=FakeTranscriber(transcript(text="嗯")))

        self.assertIn("转写不足", str(caught.exception))

    def test_no_points_raises(self):
        def empty_completer(messages, chunk):
            return {"points": []}

        with patch("src.video.pipeline.probe", return_value=video_info()):
            with self.assertRaises(VideoUnverified):
                summarize_video(
                    "demo.mp4",
                    transcriber=FakeTranscriber(transcript()),
                    completer=empty_completer,
                    out_dir=self.out,
                    extractor=fake_extractor,
                    keyframe_extractor=lambda video, target: [],
                )


class TestKeyframes(PipelineCase):
    def test_keyframe_failure_is_tolerated(self):
        def broken(video, target):
            raise RuntimeError("ffmpeg 崩了")

        summary = self.run_pipeline(keyframes=broken)

        self.assertTrue(summary.verified)
        self.assertIn("ffmpeg 崩了", summary.keyframe_error)

    def test_keyframes_recorded(self):
        frames = [self.out / "kf-001.jpg", self.out / "kf-002.jpg"]

        summary = self.run_pipeline(keyframes=lambda video, target: frames)

        self.assertEqual(len(summary.keyframes), 2)


class TestUsage(PipelineCase):
    def test_usage_defaults_to_zero(self):
        summary = self.run_pipeline()

        self.assertEqual(summary.usage.total, 0)

    def test_usage_in_payload(self):
        payload = self.run_pipeline().to_dict()

        self.assertEqual(payload["usage"]["total"], 0)
        self.assertIn("prompt", payload["usage"])
class TestUsageAggregation(PipelineCase):
    def test_usage_totals_serialize(self):
        from src.video.pipeline import UsageTotals

        payload = UsageTotals(prompt=10, completion=5, total=15).to_dict()

        self.assertEqual(payload, {"prompt": 10, "completion": 5, "total": 15})

    def test_usage_defaults(self):
        from src.video.pipeline import UsageTotals

        self.assertEqual(UsageTotals().to_dict(), {"prompt": 0, "completion": 0, "total": 0})

    def test_pipeline_aggregates_multiple_calls(self):
        from unittest.mock import patch

        class Usage:
            prompt_tokens = 100
            completion_tokens = 50
            total_tokens = 150

        class Call:
            usage = Usage()

        payload = {"points": [{"text": f"要点{i}", "segments": [i]} for i in range(4)]}

        with patch(
            "src.research.llm.complete_json",
            return_value=(payload, Call()),
        ), patch("src.video.pipeline.probe", return_value=video_info()):
            summary = summarize_video(
                "demo.mp4",
                transcriber=FakeTranscriber(transcript(20)),
                completer=None,
                out_dir=self.out,
                extractor=fake_extractor,
                keyframe_extractor=lambda video, target: [],
            )
        # 20 段 / 每块 8 段 = 3 块 → 3 次调用
        self.assertEqual(summary.usage.total, 450)
        self.assertEqual(summary.usage.prompt, 300)

    def test_missing_usage_fields_do_not_crash(self):
        from unittest.mock import patch

        class Usage:
            total_tokens = 7

        class Call:
            usage = Usage()

        with patch(
            "src.research.llm.complete_json",
            return_value=({"points": [{"text": "x", "segments": [0]}]}, Call()),
        ), patch("src.video.pipeline.probe", return_value=video_info()):
            summary = summarize_video(
                "demo.mp4",
                transcriber=FakeTranscriber(transcript(3)),
                completer=None,
                out_dir=self.out,
                extractor=fake_extractor,
                keyframe_extractor=lambda video, target: [],
            )

        self.assertEqual(summary.usage.total, 7)
        self.assertEqual(summary.usage.prompt, 0)

    def test_usage_survives_serialization(self):
        payload = self.run_pipeline().to_dict()

        self.assertIsInstance(payload["usage"]["total"], int)

    def test_cli_writes_usage(self):
        import json

        import scripts.summarize_video as cli

        summary = self.run_pipeline()

        with patch.object(cli, "summarize_video", lambda *a, **k: summary):
            code = cli.main(["--video", "demo.mp4", "--out", str(self.out)])

        self.assertEqual(code, 0)

        files = sorted(self.out.glob("video-summary-*.json"))
        payload = json.loads(files[-1].read_text(encoding="utf-8"))

        self.assertIn("usage", payload)
        self.assertEqual(payload["usage"]["total"], 0)

    def test_cli_prints_usage(self):
        import io

        from contextlib import redirect_stdout

        import scripts.summarize_video as cli

        summary = self.run_pipeline()
        buffer = io.StringIO()

        with patch.object(cli, "summarize_video", lambda *a, **k: summary):
            with redirect_stdout(buffer):
                cli.main(["--video", "demo.mp4", "--out", str(self.out)])

        self.assertIn("tokens", buffer.getvalue())
if __name__ == "__main__":
    unittest.main()
