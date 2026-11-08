"""W8 混合回归：固定组合跑 N 轮，每轮独立进程。

    .\\.venv\\Scripts\\python.exe scripts/run_mixed_regression.py --rounds 10

为什么固定组合：按时间挑样本等于挑好看的。这里每轮跑**同一组模块**，
10 轮里坏几次就记几次，**不推导总体比率**（§9.4）。

红线事件：子进程非 0 退出 / 输出出现 FAILED / 输出为空。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_DIR = (
    Path(r"C:\Users\123\Documents\deepseek-harness\default-workspace")
    / "pa-w8-evidence-20261107"
)

#: 固定组合：web / memory / repair / artifact / score / fault / 真实入口 / diff / 远程
MODULES = (
    "tests.unit.test_web_search",
    "tests.unit.test_web_extract",
    "tests.unit.test_memory_store",
    "tests.unit.test_memory_eval",
    "tests.unit.test_repair_loop",
    "tests.unit.test_artifact_registry",
    "tests.unit.test_scorecard",
    "tests.unit.test_fault_matrix",
    "tests.unit.test_remote_guard",
    "tests.unit.test_remote_bridge",
    "tests.integration.test_web_pipeline",
    "tests.integration.test_artifact_api",
    "tests.integration.test_main_app_routes",
    "tests.integration.test_diff_api",
    "tests.integration.test_activity_timeline_api",
    "tests.unit.test_comfy_tools",
    "tests.unit.test_remote_ratelimit",
    "tests.unit.test_collab_cli",
    "tests.unit.test_lifecycle_files",
    "tests.integration.test_video_api",
)

ROUND_TIMEOUT = 180


def run_round(index: int) -> dict:
    started = time.perf_counter()

    completed = subprocess.run(
        [sys.executable, "-m", "unittest", *MODULES],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=ROUND_TIMEOUT,
    )

    duration = round(time.perf_counter() - started, 2)
    output = (completed.stdout or "") + (completed.stderr or "")
    red_line = completed.returncode != 0 or "FAILED" in output or not output.strip()

    return {
        "round": index + 1,
        "exit_code": completed.returncode,
        "duration_s": duration,
        "passed": not red_line,
        "tail": output.strip().splitlines()[-5:],
        "red_line": red_line,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="W8 混合回归")
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--out", default=str(OUT_DIR))
    args = parser.parse_args(argv)

    if not 1 <= args.rounds <= 50:
        parser.error("--rounds 必须在 1..50")

    results = []

    for index in range(args.rounds):
        result = run_round(index)
        results.append(result)

        mark = "PASS" if result["passed"] else "RED"
        print(
            f"[{index + 1}/{args.rounds}] {mark} exit={result['exit_code']} "
            f"{result['duration_s']}s"
        )

        if not result["passed"]:
            for line in result["tail"]:
                print(f"    {line}")

    red_lines = [item for item in results if item["red_line"]]
    total_seconds = round(sum(item["duration_s"] for item in results), 1)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rounds": args.rounds,
        "modules": list(MODULES),
        "results": results,
        "red_line_events": len(red_lines),
        "all_passed": not red_lines,
        "total_seconds": total_seconds,
        "note": "10 次中是否出现，不推导总体比率",
    }

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"mixed-regression-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"红线事件 {len(red_lines)}/{args.rounds}，总耗时 {total_seconds}s → "
        f"{'PASS' if not red_lines else 'FAIL'}"
    )
    print(f"证据：{path}")

    return 0 if not red_lines else 1


if __name__ == "__main__":
    raise SystemExit(main())
