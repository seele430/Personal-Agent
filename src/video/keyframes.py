"""关键帧：ffmpeg 场景检测，最多取 N 张。

只作**证据图片**，不送模型（模型没有视觉能力）。抽帧失败不影响总结，
但要如实记录失败原因。
"""

from __future__ import annotations

import shutil
import subprocess

from pathlib import Path

from src.video.probe import VideoError


MAX_FRAMES = 8
SCENE_THRESHOLD = 0.3
KEYFRAME_TIMEOUT = 300


def extract_keyframes(
    video: str | Path,
    out_dir: str | Path,
    *,
    max_frames: int = MAX_FRAMES,
    threshold: float = SCENE_THRESHOLD,
) -> list[Path]:
    """按场景变化抽帧；返回抽到的文件列表（可能为空）。"""

    if isinstance(max_frames, bool) or not isinstance(max_frames, int) or max_frames < 1:
        raise ValueError("max_frames 必须是正整数")

    if not 0.0 < threshold <= 1.0:
        raise ValueError("threshold 必须在 (0, 1]")

    source = Path(video)
    target_dir = Path(out_dir)

    if not source.is_file():
        raise VideoError(f"视频不存在：{source}")

    target_dir.mkdir(parents=True, exist_ok=True)

    completed = subprocess.run(
        [
            shutil.which("ffmpeg") or "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-vf",
            f"select='gt(scene,{threshold})',scale=640:-1",
            "-vsync",
            "vfr",
            "-frames:v",
            str(max_frames),
            str(target_dir / "kf-%03d.jpg"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=KEYFRAME_TIMEOUT,
    )

    if completed.returncode != 0:
        raise VideoError(f"抽帧失败：{completed.stderr.strip()[:200]}")

    return sorted(target_dir.glob("kf-*.jpg"))[:max_frames]
