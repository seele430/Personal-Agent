"""抽音频：统一成 16 kHz 单声道 wav，交给 ASR。

为什么固定 16 kHz 单声道：whisper 系模型内部就是按这个重采样，
提前做掉可以避免"每个后端自己处理采样率"导致的结果漂移。
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile

from dataclasses import dataclass
from pathlib import Path

from src.video.probe import VideoError


TARGET_RATE = 16_000
EXTRACT_TIMEOUT = 300


@dataclass(frozen=True)
class AudioTrack:
    path: Path
    duration_s: float
    size_bytes: int

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "duration_s": round(self.duration_s, 2),
            "size_bytes": self.size_bytes,
        }


def ffmpeg_binary() -> str:
    binary = shutil.which("ffmpeg")

    if not binary:
        raise VideoError("找不到 ffmpeg（请确认已安装并在 PATH 里）")

    return binary


def extract_audio(
    video: str | Path,
    *,
    out_dir: str | Path | None = None,
    duration_s: float = 0.0,
) -> AudioTrack:
    """把视频音轨抽成 16 kHz 单声道 wav。"""

    source = Path(video)

    if not source.is_file():
        raise VideoError(f"视频不存在：{source}")

    target_dir = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="pa-audio-"))
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{source.stem}.wav"

    completed = subprocess.run(
        [
            ffmpeg_binary(),
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(TARGET_RATE),
            "-f",
            "wav",
            str(target),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=EXTRACT_TIMEOUT,
    )

    if completed.returncode != 0 or not target.is_file():
        raise VideoError(f"抽音频失败：{completed.stderr.strip()[:200]}")

    return AudioTrack(
        path=target,
        duration_s=duration_s,
        size_bytes=target.stat().st_size,
    )
