"""独立 TTS 路由与房间路由下线的 HTTP 回归测试。"""

import subprocess
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from src.main import app
from tests.helper import isolate_paths


class TestTTSAPI(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_paths(self, self.temp)
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(
            lambda: self.client.__exit__(None, None, None)
        )

    def test_tts_keeps_api_prefix_and_audio_contract(self):
        with patch(
            "src.tts_api._synthesize", return_value=b"mp3-bytes"
        ) as synthesize:
            response = self.client.post(
                "/api/tts",
                json={"text": "你好", "voice": "male-1"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"mp3-bytes")
        self.assertEqual(response.headers["content-type"], "audio/mpeg")
        self.assertEqual(response.headers["cache-control"], "no-store")
        synthesize.assert_called_once_with(
            "你好", "zh-CN-YunxiNeural", "+20%"
        )

    def test_tts_diag_is_available_without_real_network(self):
        probe = subprocess.CompletedProcess(
            args=["python"], returncode=0, stdout=b"child-ok", stderr=b""
        )
        with (
            patch("src.tts_api.subprocess.run", return_value=probe),
            patch("src.tts_api._synthesize", return_value=b"audio"),
            patch("urllib.request.getproxies", return_value={}),
            patch("urllib.request.urlopen") as urlopen,
            patch("socket.create_connection", return_value=MagicMock()),
        ):
            urlopen.return_value.__enter__.return_value.status = 200
            response = self.client.get("/api/tts/diag")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["child"], "child-ok")
        self.assertEqual(response.json()["synth"], "ok 5 bytes")
        self.assertEqual(response.json()["tcp_443"], "ok")
        self.assertEqual(response.json()["https"], "ok 200")

    def test_room_and_hand_routes_are_not_registered(self):
        self.assertEqual(self.client.get("/api/rooms").status_code, 404)
        self.assertEqual(
            self.client.post("/api/hands/hand-1/approve").status_code,
            404,
        )
        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json(), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
