"""视频探测：用 ffprobe 读时长、流信息、有没有音轨。

只做事实读取，不做"猜内容"。没有音轨时**不需要**转写就能判定不可总结，
这比"先转写再发现是空的"省得多。
"""

from __future__ import annotations

import json
import shutil
import subprocess

from dataclasses import dataclass
from pathlib import Path


FFPROBE_TIMEOUT = 30


class VideoError(RuntimeError):
    """视频不可用（文件不存在、ffprobe 失败）。"""


class VideoUnverified(VideoError):
    """无法验证：没有音轨、或转写不足以支撑总结。"""


@dataclass(frozen=True)
class StreamInfo:
    codec_type: str
    codec_name: str
    channels: int = 0
    sample_rate: int = 0


@dataclass(frozen=True)
class VideoInfo:
    path: Path
    duration_s: float
    size_bytes: int
    streams: tuple[StreamInfo, ...]

    @property
    def has_audio(self) -> bool:
        return any(stream.codec_type == "audio" for stream in self.streams)

    @property
    def has_video(self) -> bool:
        return any(stream.codec_type == "video" for stream in self.streams)

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "duration_s": round(self.duration_s, 2),
            "size_bytes": self.size_bytes,
            "has_audio": self.has_audio,
            "has_video": self.has_video,
            "streams": [
                {
                    "type": stream.codec_type,
                    "codec": stream.codec_name,
                    "channels": stream.channels,
                    "sample_rate": stream.sample_rate,
                }
                for stream in self.streams
            ],
        }


def ffprobe_binary() -> str:
    binary = shutil.which("ffprobe")

    if not binary:
        raise VideoError("找不到 ffprobe（请确认 ffmpeg 已安装并在 PATH 里）")

    return binary


def probe(path: str | Path) -> VideoInfo:
    """读取视频元信息；文件不存在或不可解析时抛 VideoError。"""

    target = Path(path)

    if not target.is_file():
        raise VideoError(f"视频不存在：{target}")

    completed = subprocess.run(
        [
            ffprobe_binary(),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(target),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=FFPROBE_TIMEOUT,
    )

    if completed.returncode != 0:
        raise VideoError(f"ffprobe 解析失败：{completed.stderr.strip()[:200]}")

    try:
        payload = json.loads(completed.stdout or "{}")

    except json.JSONDecodeError as error:
        raise VideoError("ffprobe 输出不是合法 JSON") from error

    streams = tuple(
        StreamInfo(
            codec_type=str(item.get("codec_type") or ""),
            codec_name=str(item.get("codec_name") or ""),
            channels=int(item.get("channels") or 0),
            sample_rate=int(item.get("sample_rate") or 0),
        )
        for item in payload.get("streams") or []
        if isinstance(item, dict)
    )

    duration = 0.0
    raw_duration = (payload.get("format") or {}).get("duration")

    try:
        duration = float(raw_duration)

    except (TypeError, ValueError):
        duration = 0.0

    info = VideoInfo(
        path=target,
        duration_s=duration,
        size_bytes=target.stat().st_size,
        streams=streams,
    )

    if not info.has_audio:
        raise VideoUnverified(f"视频没有音轨，无法转写：{target.name}")

    return info
