"""长音频"不截断"判定（规则先写死）。

    .\\.venv/Scripts/python.exe scripts/check_long_video.py --truth ... --summary ... --out ...

四项：
1. 最后一句的 key 出现在转写里（音频处理到结尾）；
2. 转写命中率 ≥95%；
3. 至少 1 条要点 start > 总时长 × 0.66（后半段真的进了摘要）；
4. summary.truncated == False。
"""

from __future__ import annotations

import argparse
import json

from pathlib import Path

from score_synthetic import normalize


def check(truth: dict, summary: dict) -> dict:
    segments = summary["transcript"]["segments"]
    text = "".join(item["text"] for item in segments)
    text_norm = normalize(text)
    duration = float(summary["video"]["duration_s"])
    points = summary["points"]

    # 双口径：raw = 字面匹配；normalized = 数字/百分数归一后匹配（更公平）
    raw_hits = [row["key"] for row in truth["sentences"] if row["key"].lower() in text.lower()]
    hits = [
        row["key"]
        for row in truth["sentences"]
        if normalize(row["key"]) in text_norm
    ]
    total = len(truth["sentences"])
    last_key = truth["sentences"][-1]["key"]

    late_points = [p for p in points if p["start"] > duration * 0.66]

    checks = {
        "last_sentence_transcribed": last_key.lower() in text,
        "transcript_rate_ge_95": len(hits) / total >= 0.95,
        "late_section_summarized": bool(late_points),
        "not_truncated": not summary.get("truncated", False),
    }

    return {
        "sentences": total,
        "transcript_hits": len(hits),
        "transcript_hits_raw": len(raw_hits),
        "transcript_rate": round(len(hits) / total, 3),
        "duration_s": round(duration, 1),
        "points": len(points),
        "late_points": len(late_points),
        "last_point_start": round(max((p["start"] for p in points), default=0.0), 2),
        "checks": checks,
        "passed": all(checks.values()),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="长音频不截断判定")
    parser.add_argument("--truth", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    truth = json.loads(Path(args.truth).read_text(encoding="utf-8"))
    summary = json.loads(Path(args.summary).read_text(encoding="utf-8"))
    result = check(truth, summary)

    Path(args.out).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(
        f"时长 {result['duration_s']} s，转写命中 {result['transcript_hits']}/{result['sentences']}"
        f"（{result['transcript_rate']:.0%}），要点 {result['points']} 条，"
        f"后半段要点 {result['late_points']} 条（最晚起点 {result['last_point_start']} s）"
    )
    print("checks:", result["checks"])

    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
