"""视频总结管线：probe → audio → asr → summarize → keyframes → 报告。

三处"不许硬凑"的地方：

1. 没有音轨 → `VideoUnverified`（不需要转写就能判定）；
2. 转写字数不足 → `VideoUnverified`，报告写"未验证 + 原因"；
3. 要点没有段号（定位不到时间）→ 丢弃该条，不让它进报告。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from src.video.asr import (
    MIN_SPEECH_CHARS,
    Transcriber,
    Transcript,
    render_transcript,
)
from src.video.audio import AudioTrack, extract_audio
from src.video.keyframes import extract_keyframes
from src.video.probe import VideoInfo, VideoUnverified, probe
from src.video.summarize import Point, render_points, summarize


@dataclass(frozen=True)
class UsageTotals:
    """只记 token，不折算金额（单价未知，沿用 W7 口径）。"""

    prompt: int = 0
    completion: int = 0
    total: int = 0

    def to_dict(self) -> dict:
        return {
            "prompt": self.prompt,
            "completion": self.completion,
            "total": self.total,
        }


@dataclass(frozen=True)
class VideoSummary:
    info: VideoInfo
    transcript: Transcript
    points: tuple[Point, ...]
    keyframes: tuple[Path, ...] = ()
    keyframe_error: str = ""
    generated_at: str = ""
    usage: UsageTotals = UsageTotals()
    truncated: bool = False
    truncation: dict = field(default_factory=dict)

    @property
    def verified(self) -> bool:
        return bool(self.points) and self.transcript.speech_chars >= MIN_SPEECH_CHARS

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "video": self.info.to_dict(),
            "transcript": self.transcript.to_dict(),
            "points": [point.to_dict() for point in self.points],
            "keyframes": [str(path) for path in self.keyframes],
            "keyframe_error": self.keyframe_error,
            "verified": self.verified,
            "usage": self.usage.to_dict(),
            "truncated": self.truncated,
            "truncation": dict(self.truncation),
        }


def render_report(summary: VideoSummary) -> str:
    """Markdown 报告：要点在前，转写与关键帧在后。"""

    info = summary.info
    lines = [
        f"# 视频总结：{info.path.name}",
        "",
        f"- 时长：{info.duration_s:.1f} s",
        f"- 语言：{summary.transcript.language}",
        f"- 转写字数（去空白）：{summary.transcript.speech_chars}",
        f"- 要点：{len(summary.points)} 条",
        "",
        "## 要点",
        "",
        render_points(list(summary.points)) or "（没有可用要点）",
        "",
    ]

    if summary.keyframes:
        lines += ["## 关键帧", ""]

        for path in summary.keyframes:
            lines.append(f"- `{path.name}`")

        lines.append("")

    lines += [
        "## 转写",
        "",
        "```text",
        render_transcript(summary.transcript),
        "```",
        "",
    ]

    if summary.keyframe_error:
        lines += [f"> 关键帧抽取失败：{summary.keyframe_error}", ""]

    return "\n".join(lines)


def summarize_video(
    video: str | Path,
    *,
    transcriber: Transcriber,
    completer=None,
    out_dir: str | Path,
    extractor=extract_audio,
    keyframe_extractor=extract_keyframes,
) -> VideoSummary:
    """跑完整管线。任何"无法验证"都以 VideoUnverified 抛出。"""

    target_dir = Path(out_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    info = probe(video)
    audio: AudioTrack = extractor(info.path, out_dir=target_dir, duration_s=info.duration_s)

    transcript = transcriber.transcribe(audio.path)

    if transcript.speech_chars < MIN_SPEECH_CHARS:
        raise VideoUnverified(
            f"转写不足：只有 {transcript.speech_chars} 个非空白字符"
            f"（阈值 {MIN_SPEECH_CHARS}），不生成总结"
        )

    totals = {"prompt": 0, "completion": 0, "total": 0}

    def collect_usage(usage) -> None:
        totals["prompt"] += int(getattr(usage, "prompt_tokens", 0) or 0)
        totals["completion"] += int(getattr(usage, "completion_tokens", 0) or 0)
        totals["total"] += int(getattr(usage, "total_tokens", 0) or 0)

    truncation: dict = {}

    def collect_truncation(info) -> None:
        truncation.update(info)

    points = summarize(
        transcript,
        completer=completer,
        usage_sink=collect_usage,
        on_truncated=collect_truncation,
    )

    if not points:
        raise VideoUnverified("没有定位到时间的要点，不生成总结")

    keyframes: tuple[Path, ...] = ()
    keyframe_error = ""

    try:
        keyframes = tuple(keyframe_extractor(info.path, target_dir / "keyframes"))

    except Exception as error:  # noqa: BLE001 - 抽帧失败不影响总结
        keyframe_error = f"{type(error).__name__}: {error}"

    return VideoSummary(
        info=info,
        transcript=transcript,
        points=tuple(points),
        keyframes=keyframes,
        keyframe_error=keyframe_error,
        generated_at=datetime.now(timezone.utc).isoformat(),
        usage=UsageTotals(**totals),
        truncated=bool(truncation),
        truncation=truncation,
    )
