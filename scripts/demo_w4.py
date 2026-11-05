"""W4.4：一条命令跑完路由 + 弹性 + 回收 的演示。

用法：python scripts/demo_w4.py
"""

from __future__ import annotations

import json
import subprocess
import sys

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(script: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )

    payload = {
        "script": script,
        "exit": result.returncode,
        "stdout_tail": (result.stdout or "")[-1500:],
    }

    if result.returncode != 0:
        payload["stderr_tail"] = (result.stderr or "")[-1500:]

    return payload


def main() -> int:
    pipeline = run("demo_route_pipeline.py")
    autoscale = run("autoscale_compare.py")
    residue = run("verify_w34_residue.py")

    summary = {
        "pipeline": pipeline,
        "autoscale": autoscale,
        "residue": residue,
    }

    out = ROOT / "docs" / "evidence" / "w4-demo.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    for name, item in summary.items():
        print(f"{name}: exit={item['exit']}")

    print(f"evidence={out}")

    return 0 if all(item["exit"] == 0 for item in summary.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
