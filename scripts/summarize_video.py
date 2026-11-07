"""v1.1 视频总结 CLI。

    .\\.venv\\Scripts\\python.exe scripts/summarize_video.py --video docs/Personal-Agent-Demo.mp4 --out <dir> --eval

退出码：
    0  成功
    3  无法验证（无音轨 / 转写不足 / 定位不到要点）—— 如实说明原因，不算崩溃
    4  评测基线缺失或不可用
    1  其它错误
"""

from __future__ import annotations

import argparse
import json
import sys

from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.video.asr import WhisperTranscriber  # noqa: E402
from src.video.pipeline import render_report, summarize_video  # noqa: E402
from src.video.probe import VideoError, VideoUnverified  # noqa: E402


DEFAULT_OUT = (
    Path(r"C:\Users\123\Documents\deepseek-harness\default-workspace")
    / "pa-v11-evidence-20261107"
)


def evaluate(summary, ground_truth: dict) -> dict:
    """按 20 要点 / 20 事实 / 时间戳三条判定。"""

    points = [point.to_dict() for point in summary.points]
    joined = " ".join(point["text"] for point in points)

    expected_points = [str(item).strip() for item in ground_truth.get("points") or []]
    expected_facts = [str(item).strip() for item in ground_truth.get("facts") or []]

    covered = [item for item in expected_points if item and item in joined]
    matched_facts = [item for item in expected_facts if item and item in joined]

    expected_times = [
        float(value) for value in ground_truth.get("timestamps") or [] if value is not None
    ]
    actual_times = [(point["start"] + point["end"]) / 2 for point in points]

    errors = [
        abs(actual - expected) for actual in actual_times for expected in expected_times
    ]

    coverage = len(covered) / len(expected_points) if expected_points else 0.0
    accuracy = len(matched_facts) / len(expected_facts) if expected_facts else 0.0
    median_error = sorted(errors)[len(errors) // 2] if errors else None

    return {
        "expected_points": len(expected_points),
        "covered_points": len(covered),
        "coverage": round(coverage, 3),
        "expected_facts": len(expected_facts),
        "matched_facts": len(matched_facts),
        "accuracy": round(accuracy, 3),
        "median_timestamp_error_s": None if median_error is None else round(median_error, 2),
        "passed": (
            coverage >= 0.9
            and accuracy >= 0.95
            and median_error is not None
            and median_error <= 3.0
        ),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="视频总结（v1.1）")
    parser.add_argument("--video", required=True)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--model", default="base")
    parser.add_argument("--language", default=None)
    parser.add_argument("--eval", dest="eval_path", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    transcriber = WhisperTranscriber(
        model_name=args.model, language=args.language
    )

    try:
        summary = summarize_video(
            args.video, transcriber=transcriber, out_dir=out_dir
        )

    except VideoUnverified as error:
        print(f"未验证：{error}")

        return 3

    except VideoError as error:
        print(f"视频不可用：{error}")

        return 1

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = out_dir / f"video-summary-{stamp}.md"
    payload_path = out_dir / f"video-summary-{stamp}.json"

    report_path.write_text(render_report(summary), encoding="utf-8")

    payload = summary.to_dict()
    payload["report_path"] = str(report_path)
    payload["evaluation"] = None

    if args.eval_path:
        baseline = Path(args.eval_path)

        if not baseline.is_file():
            print(f"评测基线不存在：{baseline}")

            return 4

        ground_truth = json.loads(baseline.read_text(encoding="utf-8"))
        payload["evaluation"] = evaluate(summary, ground_truth)

    payload_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"要点 {len(summary.points)} 条，关键帧 {len(summary.keyframes)} 张")
    print(f"转写 {summary.transcript.speech_chars} 字（{summary.transcript.language}）")
    print(f"报告：{report_path}")

    if args.json:
        print(json.dumps(payload, ensure_ascii=False)[:2000])

    if payload["evaluation"]:
        evaluation = payload["evaluation"]
        print(
            f"覆盖 {evaluation['covered_points']}/{evaluation['expected_points']}"
            f"（{evaluation['coverage']:.0%}），"
            f"事实 {evaluation['matched_facts']}/{evaluation['expected_facts']}"
            f"（{evaluation['accuracy']:.0%}），"
            f"时间戳中位误差 {evaluation['median_timestamp_error_s']} s"
        )

        return 0 if evaluation["passed"] else 5

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
