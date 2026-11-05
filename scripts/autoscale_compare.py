"""W3.3：固定容量 vs 弹性容量的对照实验（离散时间仿真）。

不是真实压测，只验证控制器的行为与收益方向；真实压测见 docs/bench。
"""

from __future__ import annotations

import csv
import json
import sys

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.platform.autoscale import AutoscalePolicy, Autoscaler

TICKS = 180
SLOTS_PER_NODE = 8
MAX_BACKLOG = 64


def arrivals(tick: int) -> int:
    if 30 <= tick < 90:
        return 14
    if 90 <= tick < 120:
        return 6
    return 2


def simulate(elastic: bool) -> dict:
    policy = AutoscalePolicy(
        min_nodes=1,
        max_nodes=4,
        capacity_per_node=SLOTS_PER_NODE,
        headroom=0.1,
        scale_up_after=2,
        scale_down_cooldown_seconds=30,
    )
    scaler = Autoscaler(policy, now_fn=lambda: 0.0)

    # 基线选「按平均负载配 1 个节点」：峰值 14/s 超过单节点 8/s 的容量，
    # 这样才能看出弹性扩容的价值；固定 2 节点会掩盖差异。
    nodes = 1
    backlog = 0
    dropped = 0
    peak = 0
    total_wait = 0

    series = []

    for tick in range(TICKS):
        backlog += arrivals(tick)
        capacity = nodes * SLOTS_PER_NODE
        processed = min(backlog, capacity)
        backlog -= processed
        total_wait += backlog

        if backlog > MAX_BACKLOG:
            dropped += backlog - MAX_BACKLOG
            backlog = MAX_BACKLOG

        peak = max(peak, backlog)

        if elastic:
            running = min(backlog, capacity)
            queued = max(0, backlog - capacity)
            decision = scaler.observe(
                running=running,
                queued=queued,
                current=nodes,
                now=float(tick),
            )

            if decision.action == "scale_up":
                nodes = decision.desired
            elif decision.action == "scale_down":
                nodes = decision.desired

        series.append(
            {
                "tick": tick,
                "backlog": backlog,
                "nodes": nodes,
                "capacity": nodes * SLOTS_PER_NODE,
            }
        )

    return {
        "mode": "elastic" if elastic else "fixed",
        "peak_backlog": peak,
        "avg_backlog": round(total_wait / TICKS, 2),
        "dropped": dropped,
        "scale_events": len(scaler.events),
        "final_nodes": nodes,
        "series": series,
    }


def main() -> int:
    fixed = simulate(False)
    elastic = simulate(True)

    report = {
        "note": "离散时间仿真，不是真实压测；用于比较控制策略。",
        "ticks": TICKS,
        "slots_per_node": SLOTS_PER_NODE,
        "fixed": {k: v for k, v in fixed.items() if k != "series"},
        "elastic": {k: v for k, v in elastic.items() if k != "series"},
        "improvement": {
            "peak_backlog": round(
                (fixed["peak_backlog"] - elastic["peak_backlog"])
                / max(fixed["peak_backlog"], 1),
                3,
            ),
            "avg_backlog": round(
                (fixed["avg_backlog"] - elastic["avg_backlog"])
                / max(fixed["avg_backlog"], 0.01),
                3,
            ),
        },
    }

    out_dir = ROOT / "docs" / "bench"
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "autoscale-compare.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    with (out_dir / "autoscale-compare.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.writer(handle)
        writer.writerow(["tick", "fixed_backlog", "elastic_backlog", "elastic_nodes"])

        for fixed_row, elastic_row in zip(fixed["series"], elastic["series"]):
            writer.writerow(
                [
                    fixed_row["tick"],
                    fixed_row["backlog"],
                    elastic_row["backlog"],
                    elastic_row["nodes"],
                ]
            )

    print(json.dumps(report["improvement"], ensure_ascii=False))
    print(
        json.dumps(
            {
                "fixed": report["fixed"],
                "elastic": report["elastic"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
