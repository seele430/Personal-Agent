"""一条命令跑完两个端到端复核：黄金路径 + 远程 TLS。

    .\\.venv/Scripts/python.exe scripts/verify_e2e.py

两个脚本各自独立（独立数据目录、独立端口），本脚本只负责串起来与汇总。
任一失败则整体退出码 1；每步都有超时，不无限等。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

STEPS = (
    ("golden-path", SCRIPTS / "verify_golden_path.py", 300),
    ("tls", SCRIPTS / "verify_remote_tls.py", 300),
)

OUT_DIR = (
    Path(r"C:\Users\123\Documents\deepseek-harness\default-workspace")
    / "pa-v127-evidence-20261108"
)


def _safe(text: str) -> str:
    """终端编码可能装不下某些字符，打印前先降级，别让汇总脚本自己崩。"""

    encoding = sys.stdout.encoding or "utf-8"

    return text.encode(encoding, "replace").decode(encoding, "replace")


def main() -> int:
    results = []
    overall = True

    for name, script, timeout in STEPS:
        started = time.perf_counter()

        try:
            completed = subprocess.run(
                [sys.executable, str(script)],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            code = completed.returncode
            tail = (completed.stdout or "").strip().splitlines()[-6:]

        except subprocess.TimeoutExpired:
            code = 124
            tail = [f"超时（{timeout}s）"]

        elapsed = round(time.perf_counter() - started, 1)
        ok = code == 0
        overall = overall and ok

        results.append(
            {"step": name, "exit_code": code, "ok": ok, "seconds": elapsed, "tail": tail}
        )

        print(f"[{name}] exit={code} {elapsed}s")

        for line in tail:
            print(f"    {_safe(line)}")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "steps": results,
        "passed": overall,
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "e2e-summary.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"汇总：{'PASS' if overall else 'FAIL'} → {path}")

    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
