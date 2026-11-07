"""转写层测试：后端可注入、字数判定、渲染。"""

import tempfile
import unittest

from unittest.mock import patch

from pathlib import Path

from src.video.asr import (
    MIN_SPEECH_CHARS,
    Segment,
    Transcript,
    TranscriptError,
    WhisperTranscriber,
    render_transcript,
)


def transcript(texts, *, language="en"):
    segments = tuple(
        Segment(start=index * 2.0, end=index * 2.0 + 2.0, text=text)
        for index, text in enumerate(texts)
    )

    return Transcript(language=language, segments=segments)


class TestTranscript(unittest.TestCase):
    def test_text_join(self):
        item = transcript(["hello", "world"])

        self.assertEqual(item.text, "hello world")

    def test_speech_chars_ignores_whitespace(self):
        item = transcript(["a b c"])

        self.assertEqual(item.speech_chars, 3)

    def test_empty_transcript(self):
        item = transcript([])

        self.assertEqual(item.text, "")
        self.assertEqual(item.speech_chars, 0)

    def test_serializes(self):
        payload = transcript(["x"]).to_dict()

        self.assertEqual(payload["language"], "en")
        self.assertEqual(payload["segments"][0]["start"], 0.0)

    def test_min_speech_threshold_is_meaningful(self):
        self.assertGreater(MIN_SPEECH_CHARS, 10)


class TestWhisperTranscriber(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.audio = Path(self._temporary.name) / "a.wav"
        self.audio.write_bytes(b"RIFF")

    def build(self, raw):
        loader = lambda: object()  # noqa: E731
        transcribe = lambda model, path: raw

        return WhisperTranscriber(loader=loader, transcribe=transcribe)

    def test_uses_injected_backend(self):
        backend = self.build(
            {
                "language": "zh",
                "segments": [
                    {"start": 0.0, "end": 1.5, "text": " 你好 "},
                    {"start": 1.5, "end": 3.0, "text": "世界"},
                ],
            }
        )

        result = backend.transcribe(self.audio)

        self.assertEqual(result.language, "zh")
        self.assertEqual(result.text, "你好 世界")
        self.assertEqual(len(result.segments), 2)
        self.assertEqual(result.segments[0].text, "你好")

    def test_blank_segments_are_dropped(self):
        backend = self.build(
            {
                "language": "en",
                "segments": [
                    {"start": 0, "end": 1, "text": "  "},
                    {"start": 1, "end": 2, "text": "ok"},
                ],
            }
        )

        self.assertEqual(len(backend.transcribe(self.audio).segments), 1)

    def test_missing_audio_file(self):
        backend = self.build({"segments": []})

        with self.assertRaises(TranscriptError):
            backend.transcribe(Path("nope.wav"))

    def test_missing_library_raises(self):
        backend = WhisperTranscriber(loader=None, transcribe=None)

        with patch.dict("sys.modules", {"whisper": None}):
            with self.assertRaises(TranscriptError):
                backend.transcribe(self.audio)

    def test_model_is_lazy_loaded_once(self):
        calls = []

        def loader():
            calls.append(1)

            return object()

        backend = WhisperTranscriber(
            loader=loader,
            transcribe=lambda model, path: {"segments": []},
        )

        backend.transcribe(self.audio)
        backend.transcribe(self.audio)

        self.assertEqual(len(calls), 1)

    def test_unknown_language_defaults(self):
        backend = self.build({"segments": []})

        self.assertEqual(backend.transcribe(self.audio).language, "unknown")

    def test_name_is_reported(self):
        self.assertEqual(WhisperTranscriber().name, "openai-whisper")


class TestRender(unittest.TestCase):
    def test_render_lines_with_timestamps(self):
        rendered = render_transcript(transcript(["hello", "world"]))

        self.assertIn("[0.0-2.0] hello", rendered)
        self.assertIn("[2.0-4.0] world", rendered)

    def test_render_respects_limit(self):
        rendered = render_transcript(transcript(["a"] * 5), limit=2)

        self.assertEqual(len(rendered.splitlines()), 2)

    def test_render_empty(self):
        self.assertEqual(render_transcript(transcript([])), "")


class TestInitialPrompt(unittest.TestCase):
    """术语偏置：prompt 必须真的进 whisper 调用，默认不改变行为。"""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.audio = Path(self._temporary.name) / "a.wav"
        self.audio.write_bytes(b"RIFF")

    def test_prompt_is_stored(self):
        backend = WhisperTranscriber(initial_prompt="PagedAttention,vLLM")

        self.assertEqual(backend.initial_prompt, "PagedAttention,vLLM")

    def test_default_prompt_is_empty(self):
        self.assertEqual(WhisperTranscriber().initial_prompt, "")

    def test_prompt_forwarded_to_whisper(self):
        captured = {}

        class FakeWhisper:
            @staticmethod
            def transcribe(model, path, **kwargs):
                captured.update(kwargs)

                return {"language": "zh", "segments": []}

        import sys
        import types

        fake_module = types.ModuleType("whisper")
        fake_module.transcribe = FakeWhisper.transcribe
        fake_module.load_model = lambda *a, **k: object()

        backend = WhisperTranscriber(initial_prompt="术语表")

        with patch.dict(sys.modules, {"whisper": fake_module}):
            backend.transcribe(self.audio)

        self.assertEqual(captured.get("initial_prompt"), "术语表")

    def test_empty_prompt_becomes_none(self):
        captured = {}

        class FakeWhisper:
            @staticmethod
            def transcribe(model, path, **kwargs):
                captured.update(kwargs)

                return {"language": "zh", "segments": []}

        import sys
        import types

        fake_module = types.ModuleType("whisper")
        fake_module.transcribe = FakeWhisper.transcribe
        fake_module.load_model = lambda *a, **k: object()

        backend = WhisperTranscriber()

        with patch.dict(sys.modules, {"whisper": fake_module}):
            backend.transcribe(self.audio)

        self.assertIsNone(captured.get("initial_prompt"))

    def test_injected_transcribe_still_works(self):
        backend = WhisperTranscriber(
            initial_prompt="x",
            loader=lambda: object(),
            transcribe=lambda model, path: {"segments": [{"start": 0, "end": 1, "text": "ok"}]},
        )

        self.assertEqual(backend.transcribe(self.audio).text, "ok")

    def test_prompt_does_not_change_segment_parsing(self):
        backend = WhisperTranscriber(
            initial_prompt="x",
            loader=lambda: object(),
            transcribe=lambda model, path: {
                "segments": [
                    {"start": 0, "end": 1, "text": "a"},
                    {"start": 1, "end": 2, "text": " "},
                ]
            },
        )

        self.assertEqual(len(backend.transcribe(self.audio).segments), 1)
if __name__ == "__main__":
    unittest.main()
