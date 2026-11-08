"""视频总结 API 测试：鉴权、路径边界、时长上限、假转写器、422 语义。"""

import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.video_api as video_api

from src.auth_api import get_current_user
from src.config import build_paths
from src.video.asr import Segment, Transcript
from src.video.pipeline import UsageTotals, VideoSummary
from src.video.probe import StreamInfo, VideoInfo


def fake_info(duration=60.0, has_audio=True):
    streams = [StreamInfo("video", "h264")]

    if has_audio:
        streams.append(StreamInfo("audio", "aac", 2, 44100))

    return VideoInfo(
        path=Path("x.mp4"), duration_s=duration, size_bytes=10, streams=tuple(streams)
    )


def fake_summary():
    segments = tuple(
        Segment(start=index * 2.0, end=index * 2.0 + 2.0, text="这是一段足够长的解说内容。")
        for index in range(3)
    )

    from src.video.summarize import Point

    return VideoSummary(
        info=fake_info(),
        transcript=Transcript(language="zh", segments=segments),
        points=(Point(text="要点一", start=0.0, end=2.0, segment_indices=(0,)),),
        generated_at="2026-11-08T00:00:00Z",
        usage=UsageTotals(prompt=10, completion=5, total=15),
    )


class VideoApiCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)

        self.base = Path(self._temporary.name)
        self.paths = build_paths(self.base)
        self.videos = self.base / "videos"
        self.videos.mkdir()
        (self.videos / "demo.mp4").write_bytes(b"x")

        self.app = FastAPI()
        self.app.include_router(video_api.router)
        self.app.dependency_overrides[get_current_user] = lambda: {"id": "u1"}
        self.client = TestClient(self.app)

        patcher = patch("src.config.paths", self.paths)
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, payload):
        return self.client.post("/api/video/summarize", json=payload)


class TestAuth(VideoApiCase):
    def test_requires_token(self):
        self.app.dependency_overrides.pop(get_current_user, None)

        response = self.post({"path": "demo.mp4"})

        self.assertIn(response.status_code, (401, 403))


class TestPaths(VideoApiCase):
    def test_absolute_path_rejected(self):
        response = self.post({"path": str(self.videos / "demo.mp4")})

        self.assertEqual(response.status_code, 400)

    def test_traversal_rejected(self):
        (self.base / "secret.mp4").write_bytes(b"x")

        response = self.post({"path": "../secret.mp4"})

        self.assertEqual(response.status_code, 400)

    def test_missing_file(self):
        response = self.post({"path": "nope.mp4"})

        self.assertEqual(response.status_code, 404)

    def test_empty_path(self):
        self.assertEqual(self.post({"path": ""}).status_code, 400)


class TestDuration(VideoApiCase):
    def test_too_long_rejected(self):
        with patch("src.video_api.probe", return_value=fake_info(duration=1200)):
            response = self.post({"path": "demo.mp4"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("超过上限", response.json()["detail"])

    def test_no_audio_is_422(self):
        from src.video.probe import VideoUnverified

        with patch(
            "src.video_api.probe", side_effect=VideoUnverified("视频没有音轨")
        ):
            response = self.post({"path": "demo.mp4"})

        self.assertEqual(response.status_code, 422)


class TestSuccess(VideoApiCase):
    def test_returns_points_and_usage(self):
        with patch("src.video_api.probe", return_value=fake_info()), patch.object(
            video_api, "summarize_hook", lambda *a, **k: fake_summary()
        ):
            response = self.post({"path": "demo.mp4"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload["points"][0]["text"], "要点一")
        self.assertEqual(payload["usage"]["total"], 15)
        self.assertTrue(Path(payload["report_path"]).is_file())

    def test_unverified_from_pipeline_is_422(self):
        from src.video.pipeline import VideoUnverified as PipelineUnverified

        def boom(*args, **kwargs):
            raise PipelineUnverified("转写不足")

        with patch("src.video_api.probe", return_value=fake_info()), patch.object(
            video_api, "summarize_hook", boom
        ):
            response = self.post({"path": "demo.mp4"})

        self.assertEqual(response.status_code, 422)

    def test_faster_requires_model_dir(self):
        def runner(*args, **kwargs):
            return video_api._default_runner(*args, **kwargs)

        with patch("src.video_api.probe", return_value=fake_info()), patch.object(
            video_api, "summarize_hook", runner
        ):
            response = self.post({"path": "demo.mp4", "backend": "faster"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("model_dir", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
