"""视频总结能力（v1.1）：探测 → 抽音频 → 转写 → 分段摘要 → 关键帧 → 报告。

四个模块都可注入，所以离线测试不依赖 ffmpeg、模型或网络。
"""

from src.video.asr import (
    MIN_SPEECH_CHARS,
    Segment,
    Transcript,
    TranscriptError,
    WhisperTranscriber,
    render_transcript,
)
from src.video.audio import AudioTrack, extract_audio
from src.video.keyframes import extract_keyframes
from src.video.pipeline import (
    VideoSummary,
    render_report,
    summarize_video,
)
from src.video.probe import (
    StreamInfo,
    VideoError,
    VideoInfo,
    VideoUnverified,
    probe,
)
from src.video.summarize import (
    MAX_POINTS,
    Point,
    SummarizeError,
    chunk_segments,
    parse_citations,
    points_from_payload,
    render_points,
    summarize,
)


__all__ = [
    "MAX_POINTS",
    "MIN_SPEECH_CHARS",
    "AudioTrack",
    "Point",
    "Segment",
    "StreamInfo",
    "SummarizeError",
    "Transcript",
    "TranscriptError",
    "VideoError",
    "VideoInfo",
    "VideoSummary",
    "VideoUnverified",
    "WhisperTranscriber",
    "chunk_segments",
    "extract_audio",
    "extract_keyframes",
    "parse_citations",
    "points_from_payload",
    "probe",
    "render_points",
    "render_report",
    "render_transcript",
    "summarize",
    "summarize_video",
]
