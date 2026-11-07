"""按预写规则给合成素材打分（规则先写死，跑完不调）。

规则：
1. **转写命中**：该句的 key（唯一标识词）出现在转写全文 → 算转写拿到。
2. **摘要覆盖**：key 出现在任一条要点的文本里 → 算该句被覆盖。
3. **时间戳误差**：对每个含 key 的要点，取其覆盖句的已知 start 与要点 start 之差的绝对值；
   跨句要点（覆盖多句）取最近的那句。中位数 ≤3 s 算通过。
"""

from __future__ import annotations

import argparse
import json
import statistics

from pathlib import Path



#: 中文数字 → 阿拉伯数字：仅用于**归一化比对**，不改变转写原文
_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
           "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_UNITS = {"十": 10, "百": 100, "千": 1000}


def _cn_to_int(text: str) -> int | None:
    """解析 1..9999 的中文数字；解析不了返回 None。"""

    if not text:
        return None

    total = 0
    current = 0

    for char in text:
        if char in _DIGITS:
            current = _DIGITS[char]

        elif char in _UNITS:
            unit = _UNITS[char]
            total += (current or 1) * unit
            current = 0

        else:
            return None

    return total + current


def normalize(text: str) -> str:
    """把中文数字与百分数统一成阿拉伯形式，便于比对。"""

    import re

    value = text.lower().replace(" ", "")

    def replace_percent(match):
        number = _cn_to_int(match.group(1))

        return f"{number}%" if number is not None else match.group(0)

    value = re.sub(r"百分之([零一二两三四五六七八九十百千]+)", replace_percent, value)

    def replace_number(match):
        number = _cn_to_int(match.group(0))

        return str(number) if number is not None else match.group(0)

    value = re.sub(r"[零一二两三四五六七八九十百千]{1,6}", replace_number, value)

    return value

def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def score(truth: dict, summary: dict) -> dict:
    segments = summary["transcript"]["segments"]
    transcript = "".join(item["text"] for item in segments)
    transcript_norm = normalize(transcript)
    points = summary["points"]

    rows = []

    for sentence in truth["sentences"]:
        key = sentence["key"]
        key_norm = normalize(key)
        in_transcript_raw = key.lower() in transcript.lower()
        in_transcript = key_norm in transcript_norm

        owners = [
            point
            for point in points
            if key_norm in normalize(point["text"]) or key.lower() in point["text"].lower()
        ]

        best_error = None

        for point in owners:
            error = abs(point["start"] - sentence["start"])
            best_error = error if best_error is None else min(best_error, error)

        rows.append(
            {
                "index": sentence["index"],
                "key": key,
                "in_transcript": in_transcript,
                "in_transcript_raw": in_transcript_raw,
                "covered": bool(owners),
                "timestamp_error_s": None if best_error is None else round(best_error, 2),
            }
        )

    transcript_hits = sum(1 for row in rows if row["in_transcript"])
    transcript_hits_raw = sum(1 for row in rows if row["in_transcript_raw"])
    covered = sum(1 for row in rows if row["covered"])
    errors = [row["timestamp_error_s"] for row in rows if row["timestamp_error_s"] is not None]

    total = len(rows)
    median_error = round(statistics.median(errors), 2) if errors else None

    return {
        "sentences": total,
        "transcript_hits": transcript_hits,
        "transcript_hits_raw": transcript_hits_raw,
        "note": "raw = 字面匹配（中文数字 vs 阿拉伯数字会不匹配）；归一化口径见 normalize()",
        "transcript_rate": round(transcript_hits / total, 3),
        "covered": covered,
        "coverage": round(covered / total, 3),
        "timestamp_samples": len(errors),
        "median_timestamp_error_s": median_error,
        "checks": {
            "transcript_rate_ge_95": transcript_hits / total >= 0.95,
            "coverage_ge_90": covered / total >= 0.9,
            "median_timestamp_le_3": median_error is not None and median_error <= 3.0,
        },
        "rows": rows,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="合成素材评测")
    parser.add_argument("--truth", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    truth = load(Path(args.truth))
    summary = load(Path(args.summary))
    result = score(truth, summary)

    Path(args.out).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(
        f"转写命中 {result['transcript_hits']}/{result['sentences']}"
        f"（{result['transcript_rate']:.0%}）"
    )
    print(
        f"摘要覆盖 {result['covered']}/{result['sentences']}"
        f"（{result['coverage']:.0%}）"
    )
    print(
        f"时间戳中位误差 {result['median_timestamp_error_s']} s"
        f"（样本 {result['timestamp_samples']}）"
    )
    print("checks:", result["checks"])

    return 0 if all(result["checks"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
