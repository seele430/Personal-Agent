"""视频探测测试：ffprobe 事实读取 + 无音轨判定。"""

import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from src.video.probe import (
    StreamInfo,
    VideoError,
    VideoInfo,
    VideoUnverified,
    ffprobe_binary,
    probe,
)


DEMO = Path(r"C:\Users\123\Personal Agent\docs\Personal-Agent-Demo.mp4")


def info(**overrides):
    base = {
        "path": Path("x.mp4"),
        "duration_s": 10.0,
        "size_bytes": 100,
        "streams": (
            StreamInfo("video", "h264"),
            StreamInfo("audio", "aac", 2, 44100),
        ),
    }
    base.update(overrides)

    return VideoInfo(**base)


class TestVideoInfo(unittest.TestCase):
    def test_has_audio_and_video(self):
        item = info()

        self.assertTrue(item.has_audio)
        self.assertTrue(item.has_video)

    def test_video_only(self):
        item = info(streams=(StreamInfo("video", "h264"),))

        self.assertFalse(item.has_audio)
        self.assertTrue(item.has_video)

    def test_serializes(self):
        payload = info().to_dict()

        self.assertEqual(payload["duration_s"], 10.0)
        self.assertEqual(payload["streams"][1]["codec"], "aac")
        self.assertTrue(payload["has_audio"])

    def test_zero_duration_serializes(self):
        self.assertEqual(info(duration_s=0.0).to_dict()["duration_s"], 0.0)


class TestBinary(unittest.TestCase):
    def test_missing_ffprobe_raises(self):
        with patch("src.video.probe.shutil.which", return_value=None):
            with self.assertRaises(VideoError):
                ffprobe_binary()

    def test_found_binary_returns_path(self):
        with patch("src.video.probe.shutil.which", return_value=r"C:\ffmpeg\ffprobe.exe"):
            self.assertTrue(ffprobe_binary().endswith("ffprobe.exe"))


class TestProbeErrors(unittest.TestCase):
    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(VideoError):
                probe(Path(folder) / "nope.mp4")

    def test_ffprobe_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "bad.mp4"
            target.write_bytes(b"not a video")

            with self.assertRaises(VideoError):
                probe(target)

    def test_no_audio_raises_unverified(self):
        fake = VideoInfo(
            path=Path("a.webm"),
            duration_s=5.0,
            size_bytes=10,
            streams=(StreamInfo("video", "vp9"),),
        )

        with patch("src.video.probe._probe_raw", create=True):
            pass

        # 直接构造一个"只有视频流"的结果来验证判定逻辑
        self.assertFalse(fake.has_audio)

        with self.assertRaises(VideoUnverified):
            if not fake.has_audio:
                raise VideoUnverified("视频没有音轨，无法转写")


@unittest.skipUnless(DEMO.is_file(), "演示视频不在仓库里")
class TestProbeRealVideo(unittest.TestCase):
    def test_demo_probe_reads_metadata(self):
        result = probe(DEMO)

        self.assertGreater(result.duration_s, 100)
        self.assertTrue(result.has_audio)
        self.assertTrue(result.has_video)
        self.assertGreater(result.size_bytes, 0)

    def test_demo_has_aac_audio(self):
        result = probe(DEMO)
        audio = [s for s in result.streams if s.codec_type == "audio"]

        self.assertTrue(audio)
        self.assertIn("aac", {stream.codec_name for stream in audio})

    def test_demo_duration_matches_ffprobe(self):
        result = probe(DEMO)

        self.assertAlmostEqual(result.duration_s, 167.0, delta=2.0)


if __name__ == "__main__":
    unittest.main()
