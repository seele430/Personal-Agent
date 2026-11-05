"""评估意图路由：准确率、macro-F1、各类 P/R、混淆矩阵、高危漏审批。"""

from __future__ import annotations

import argparse
import json
import sys

from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA = ROOT / "tests" / "data" / "router-eval.jsonl"


def load(split: str) -> list[dict]:
    rows = []

    for line in DATA.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue

        row = json.loads(line)

        if split != "all" and row["split"] != split:
            continue

        rows.append(row)

    return rows


def prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )

    return round(precision, 3), round(recall, 3), round(f1, 3)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", default="test", choices=["train", "dev", "test", "all"])
    parser.add_argument("--out", default=str(ROOT / "docs" / "evidence" / "router-eval.json"))
    args = parser.parse_args()

    from src.routing import Router
    from src.routing.policy import approval_gate

    router = Router()
    rows = load(args.split)

    labels = sorted({row["intent"] for row in rows})
    confusion: dict[str, Counter] = defaultdict(Counter)
    stats: dict[str, Counter] = defaultdict(Counter)

    correct = 0
    clarified = 0
    high_risk_total = 0
    high_risk_missed = 0

    for row in rows:
        decision = router.decide(row["text"])
        expected = row["intent"]

        if decision.decision == "clarify":
            clarified += 1
            pred = "__clarify__"
        else:
            pred = decision.intent

        confusion[expected][pred] += 1

        if pred == expected:
            correct += 1

        for label in labels:
            if pred == label and expected == label:
                stats[label]["tp"] += 1
            elif pred == label and expected != label:
                stats[label]["fp"] += 1
            elif pred != label and expected == label:
                stats[label]["fn"] += 1

        gate = approval_gate(decision)

        if row["risk"] == "high":
            high_risk_total += 1

            if gate.allowed:
                high_risk_missed += 1

    per_class = {}

    for label in labels:
        precision, recall, f1 = prf(
            stats[label]["tp"],
            stats[label]["fp"],
            stats[label]["fn"],
        )
        per_class[label] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": sum(
                1 for row in rows if row["intent"] == label
            ),
        }

    macro_f1 = round(
        sum(item["f1"] for item in per_class.values()) / max(len(per_class), 1),
        3,
    )

    report = {
        "split": args.split,
        "rows": len(rows),
        "accuracy": round(correct / max(len(rows), 1), 3),
        "macro_f1": macro_f1,
        "clarify_count": clarified,
        "clarify_rate": round(clarified / max(len(rows), 1), 3),
        "per_class": per_class,
        "confusion": {key: dict(value) for key, value in confusion.items()},
        "high_risk": {
            "total": high_risk_total,
            "missed_approval": high_risk_missed,
        },
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(
        json.dumps(
            {
                "split": report["split"],
                "rows": report["rows"],
                "accuracy": report["accuracy"],
                "macro_f1": report["macro_f1"],
                "high_risk_missed": high_risk_missed,
                "out": str(out),
            },
            ensure_ascii=False,
        )
    )

    return 0 if high_risk_missed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
