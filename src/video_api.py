"""视频总结 API（v1.1.11）：受控、同步、只读。

安全边界（写死在这里，不靠调用方守规矩）：

1. 只接受 `data_dir/videos/` 内的相对路径；绝对路径与 `..` 一律 400；
2. 时长上限 600 s，超过直接拒绝（不排队、不偷偷截断）；
3. 无音轨 / 转写不足 → **422**，与"参数错误 400"区分；
4. 不写产物库；报告写到 `data_dir/video-reports/`。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from src.auth_api import get_current_user
from src.video.pipeline import VideoUnverified, render_report, summarize_video
from src.video.probe import VideoError, probe


router = APIRouter(prefix="/api/video", tags=["视频总结"])

MAX_DURATION_S = 600.0

#: 测试注入点：默认用真实管线（faster 或 openai，由调用方参数决定）
summarize_hook = None


def video_paths() -> tuple[Path, Path]:
    from src.config import paths

    base = paths.platform_db.parent

    return base / "videos", base / "video-reports"


def resolve_video(raw: str) -> Path:
    """把请求里的相对路径安全地解析到 videos/ 下。"""

    if not isinstance(raw, str) or not raw.strip():
        raise HTTPException(status_code=400, detail="path 不能为空")

    candidate = Path(raw)

    if candidate.is_absolute():
        raise HTTPException(status_code=400, detail="只接受 videos/ 内的相对路径")

    videos, _ = video_paths()
    target = (videos / candidate).resolve()

    if not target.is_relative_to(videos.resolve()):
        raise HTTPException(status_code=400, detail="路径越界")

    if not target.is_file():
        raise HTTPException(status_code=404, detail="视频不存在")

    return target


@router.post("/summarize")
def summarize(payload: dict, user: dict = Depends(get_current_user)) -> dict:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是对象")

    target = resolve_video(payload.get("path", ""))

    try:
        info = probe(target)

    except VideoUnverified as error:
        raise HTTPException(status_code=422, detail=str(error)) from None

    except VideoError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None

    if info.duration_s > MAX_DURATION_S:
        raise HTTPException(
            status_code=400,
            detail=f"视频时长 {info.duration_s:.0f}s 超过上限 {MAX_DURATION_S:.0f}s",
        )

    _, reports = video_paths()
    reports.mkdir(parents=True, exist_ok=True)

    runner = summarize_hook or _default_runner

    try:
        summary = runner(
            target,
            prompt=str(payload.get("prompt") or ""),
            backend=str(payload.get("backend") or "openai"),
            model_dir=str(payload.get("model_dir") or ""),
            out_dir=reports,
        )

    except VideoUnverified as error:
        raise HTTPException(status_code=422, detail=str(error)) from None

    report = reports / f"{target.stem}.md"
    report.write_text(render_report(summary), encoding="utf-8")

    return {
        "video": target.name,
        "duration_s": round(info.duration_s, 1),
        "language": summary.transcript.language,
        "speech_chars": summary.transcript.speech_chars,
        "points": [point.to_dict() for point in summary.points],
        "usage": summary.usage.to_dict(),
        "truncated": summary.truncated,
        "report_path": str(report),
    }


def _default_runner(video: Path, *, prompt: str, backend: str, model_dir: str, out_dir: Path):
    """默认用真实转写器；faster 需要显式 model_dir。"""

    if backend == "faster":
        if not model_dir:
            raise HTTPException(status_code=400, detail="backend=faster 需要 model_dir")

        from src.video.asr import FasterWhisperTranscriber

        transcriber = FasterWhisperTranscriber(
            model_dir, language="zh", initial_prompt=prompt
        )

    else:
        from src.video.asr import WhisperTranscriber

        transcriber = WhisperTranscriber(language="zh", initial_prompt=prompt)

    return summarize_video(video, transcriber=transcriber, out_dir=out_dir)
