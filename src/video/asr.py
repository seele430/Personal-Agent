"""语音转写：默认 openai-whisper（CPU），后端可注入。

为什么可注入：真实模型要下载、要跑几十秒，单测里不能依赖它；
而"转写质量差到不能用"这种情况，必须由**代码判定**（字数阈值），
不能靠人肉看。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


DEFAULT_MODEL = "base"
DEFAULT_ROOT = r"E:\AppData\models\whisper-openai"

#: 少于此字数视为"没有可用语音"，不生成总结
MIN_SPEECH_CHARS = 40


class TranscriptError(RuntimeError):
    """转写不可用（后端缺失、音频读取失败）。"""


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str

    def to_dict(self) -> dict:
        return {
            "start": round(self.start, 2),
            "end": round(self.end, 2),
            "text": self.text,
        }


@dataclass(frozen=True)
class Transcript:
    language: str
    segments: tuple[Segment, ...]

    @property
    def text(self) -> str:
        return " ".join(segment.text for segment in self.segments).strip()

    @property
    def speech_chars(self) -> int:
        return len("".join(self.text.split()))

    def to_dict(self) -> dict:
        return {
            "language": self.language,
            "speech_chars": self.speech_chars,
            "segments": [segment.to_dict() for segment in self.segments],
        }


class Transcriber(Protocol):
    name: str

    def transcribe(self, audio_path: str | Path) -> Transcript:
        ...


class WhisperTranscriber:
    """openai-whisper 后端。模型懒加载，避免导入即占内存。"""

    name = "openai-whisper"

    def __init__(
        self,
        *,
        model_name: str = DEFAULT_MODEL,
        download_root: str = DEFAULT_ROOT,
        language: str | None = None,
        initial_prompt: str = "",
        loader=None,
        transcribe=None,
    ):
        self.model_name = model_name
        self.download_root = download_root
        self.language = language
        self.initial_prompt = initial_prompt
        self._loader = loader
        self._transcribe = transcribe
        self._model = None

    def _get_model(self):
        if self._model is not None:
            return self._model

        loader = self._loader

        if loader is None:
            try:
                import whisper

            except ImportError as error:
                raise TranscriptError(
                    "未安装 openai-whisper，无法转写"
                ) from error

            loader = lambda: whisper.load_model(  # noqa: E731
                self.model_name, download_root=self.download_root
            )

        self._model = loader()

        return self._model

    def transcribe(self, audio_path: str | Path) -> Transcript:
        path = Path(audio_path)

        if not path.is_file():
            raise TranscriptError(f"音频不存在：{path}")

        model = self._get_model()

        if self._transcribe is not None:
            raw = self._transcribe(model, str(path))

        else:
            import whisper

            raw = whisper.transcribe(
                model,
                str(path),
                language=self.language,
                fp16=False,
                verbose=False,
                initial_prompt=self.initial_prompt or None,
            )

        segments = tuple(
            Segment(
                start=float(item["start"]),
                end=float(item["end"]),
                text=str(item["text"]).strip(),
            )
            for item in raw.get("segments") or []
            if str(item.get("text") or "").strip()
        )

        return Transcript(
            language=str(raw.get("language") or "unknown"),
            segments=segments,
        )


class FasterWhisperTranscriber:
    """faster-whisper 后端（CPU int8）。

    为什么单独一个类：`faster_whisper.WhisperModel` 的输入是路径或 numpy 样本，
    返回的 segments 是**生成器 + 属性访问**，与 openai-whisper 的 dict 不同。
    另外本地 `av` 与 faster-whisper 的 decode_audio 不兼容，这里用标准库读 wav
    再传 numpy，绕开 av。
    """

    name = "faster-whisper"

    def __init__(
        self,
        model_dir: str | Path,
        *,
        compute_type: str = "int8",
        device: str = "cpu",
        language: str | None = "zh",
        initial_prompt: str = "",
        model_factory=None,
    ):
        self.model_dir = Path(model_dir)
        self.compute_type = compute_type
        self.device = device
        self.language = language
        self.initial_prompt = initial_prompt
        self._factory = model_factory
        self._model = None

    def _get_model(self):
        if self._model is not None:
            return self._model

        if not self.model_dir.is_dir():
            raise TranscriptError(f"faster-whisper 模型目录不存在：{self.model_dir}")

        factory = self._factory

        if factory is None:
            try:
                from faster_whisper import WhisperModel

            except ImportError as error:
                raise TranscriptError("未安装 faster-whisper") from error

            factory = lambda: WhisperModel(  # noqa: E731
                str(self.model_dir), device=self.device, compute_type=self.compute_type
            )

        self._model = factory()

        return self._model

    def transcribe(self, audio_path: str | Path) -> Transcript:
        path = Path(audio_path)

        if not path.is_file():
            raise TranscriptError(f"音频不存在：{path}")

        if path.suffix.lower() != ".wav":
            raise TranscriptError("faster-whisper 后端目前只支持 wav（管线总是抽 wav）")

        model = self._get_model()
        samples = _read_wav_samples(path)

        segments_iter, info = model.transcribe(
            samples,
            language=self.language,
            beam_size=5,
            vad_filter=True,
            initial_prompt=self.initial_prompt or None,
        )

        segments = tuple(
            Segment(start=float(item.start), end=float(item.end), text=str(item.text).strip())
            for item in segments_iter
            if str(getattr(item, "text", "")).strip()
        )

        return Transcript(
            language=str(getattr(info, "language", None) or "unknown"),
            segments=segments,
        )


def _read_wav_samples(path: Path):
    """读 16 kHz 单声道 PCM wav，返回 [-1, 1] 的 float32 数组。"""

    import wave

    import numpy as np

    with wave.open(str(path), "rb") as handle:
        if handle.getframerate() != 16_000:
            raise TranscriptError(
                f"wav 采样率必须是 16000，实际 {handle.getframerate()}"
            )

        raw = handle.readframes(handle.getnframes())

    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def render_transcript(transcript: Transcript, *, limit: int = 200) -> str:
    """把转写渲染成 `[start-end] 文本` 的行，供报告引用。"""

    lines = [
        f"[{segment.start:.1f}-{segment.end:.1f}] {segment.text}"
        for segment in transcript.segments[:limit]
    ]

    return "\n".join(lines)
