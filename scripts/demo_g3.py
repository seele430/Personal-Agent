"""G3 演示（冻结版）：3 条命令，约 45 秒跑完。

用途：录 60 秒演示视频时只跑这一条，输出压缩成「每段 3 行」。

用法：

    .\\venv\\Scripts\\python.exe scripts\\demo_g3.py
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CASES = [
    {
        "title": "① 一句话 3 个工具 + 轨迹",
        "script": "scripts/verify_g2_chain.py",
        "fields": ["tool_started_events", "tool_finished_events", "trace_lines"],
    },
    {
        "title": "② 全链路取消（进程树一起结束）",
        "script": "scripts/verify_d11_cancel.py",
        "fields": ["scenarios", "parent_alive", "child_alive"],
    },
    {
        "title": "③ 背压：429 + 排队超时",
        "script": "scripts/verify_d30_queue.py",
        "fields": ["first", "third_code", "retry_after", "second_final"],
    },
]


def extract(output: str, key: str) -> str | None:
    match = re.search(rf'"{re.escape(key)}":\s*("?[\w./:-]+"?)', output)

    if match is None:
        return None

    return match.group(1).strip('"')


def scenario_count(output: str) -> str | None:
    try:
        payload = json.loads(output[output.index("{"): output.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return None

    scenarios = payload.get("scenarios")

    if isinstance(scenarios, list):
        return str(len(scenarios))

    return None


def main() -> int:
    started = time.monotonic()
    results = []

    for case in CASES:
        print()
        print("=" * 60)
        print(case["title"])
        print("=" * 60)

        began = time.monotonic()
        completed = subprocess.run(
            [sys.executable, str(ROOT / case["script"])],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        elapsed = time.monotonic() - began
        output = completed.stdout or ""
        ok = completed.returncode == 0

        print(f"  script : {case['script']}")
        print(f"  exit   : {completed.returncode}   用时 {elapsed:.1f}s")

        if not ok:
            tail = (completed.stderr or "").strip().splitlines()[-3:]
            for line in tail:
                print("  " + line)

        parts = []

        for field in case["fields"]:
            value = scenario_count(output) if field == "scenarios" else extract(output, field)
            if value is not None:
                parts.append(f"{field}={value}")

        if parts:
            print("  " + "  ".join(parts))

        results.append((case["title"], ok, elapsed))

    total = time.monotonic() - started
    passed = sum(1 for _, ok, _ in results if ok)

    print()
    print("=" * 60)
    print(f"G3 演示：{passed}/{len(results)} PASS，总用时 {total:.1f}s")
    print("=" * 60)

    for title, ok, elapsed in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {title}  ({elapsed:.1f}s)")

    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
