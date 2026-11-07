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


def render_transcript(transcript: Transcript, *, limit: int = 200) -> str:
    """把转写渲染成 `[start-end] 文本` 的行，供报告引用。"""

    lines = [
        f"[{segment.start:.1f}-{segment.end:.1f}] {segment.text}"
        for segment in transcript.segments[:limit]
    ]

    return "\n".join(lines)
