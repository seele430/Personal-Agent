"""W3.4：沙箱销毁后不留残留（目录 / DB / 进程）。

用法：python scripts/verify_w34_residue.py
输出：docs/evidence/w34-residue.json（残留为 0 才算通过）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def scan_processes(marker: str) -> list[dict]:
    command = (
        "Get-CimInstance Win32_Process | "
        f"Where-Object {{ $_.CommandLine -like '*{marker}*' }} | "
        "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    )

    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )

    raw = (result.stdout or "").strip()

    if not raw:
        return []

    data = json.loads(raw)

    if isinstance(data, dict):
        data = [data]

    return [
        item
        for item in data
        if item
        and str(item.get("ProcessId")) != str(os.getpid())
        # 扫描命令自身也包含路径，必须排除。
        and "Get-CimInstance Win32_Process" not in str(item.get("CommandLine"))
    ]


def main() -> int:
    data_dir = Path(tempfile.mkdtemp(prefix="pa-w34-residue-"))

    os.environ["PERSONAL_AGENT_DATA_DIR"] = str(data_dir)
    os.environ["PERSONAL_AGENT_SANDBOX_ROOT"] = str(data_dir / "sandboxes")
    os.environ["PERSONAL_AGENT_PLATFORM_REAPER"] = "0"
    os.environ["PERSONAL_AGENT_SANDBOX_GLOBAL_LIMIT"] = "0"

    from fastapi.testclient import TestClient

    from src.main import app
    from tests.helper import wait_for_exec_worker

    client = TestClient(app)
    client.__enter__()

    try:
        created = client.post(
            "/v1/sandboxes",
            json={
                "tier": "tier1",
                "workspace_id": "ws_w34",
                "ttl_seconds": 600,
            },
            headers={"Idempotency-Key": f"w34-{int(time.time())}"},
        )
        sandbox = created.json()
        sandbox_id = sandbox["sandbox_id"]

        accepted = client.post(
            f"/v1/sandboxes/{sandbox_id}/exec",
            json={
                "command": [sys.executable, "-c", "print('w34')"],
                "timeout_ms": 30_000,
            },
        )
        exec_id = accepted.json()["exec_id"]

        deadline = time.monotonic() + 60

        while time.monotonic() < deadline:
            state = client.get(f"/v1/exec/{exec_id}").json()["state"]

            if state in {"succeeded", "failed", "cancelled", "timed_out"}:
                break

            time.sleep(0.05)

        destroyed = client.post(f"/v1/sandboxes/{sandbox_id}/destroy")
        assert destroyed.status_code in {200, 202}, destroyed.text
        assert destroyed.json()["state"] == "reclaimed", destroyed.text

        wait_for_exec_worker(exec_id)
    finally:
        client.__exit__(None, None, None)

    root = data_dir / "sandboxes"

    remaining_dirs = [
        str(path)
        for path in root.iterdir()
        if path.is_dir()
    ] if root.is_dir() else []

    import sqlite3

    conn = sqlite3.connect(data_dir / "platform.db")
    expired_rows = conn.execute(
        """
        SELECT id, state FROM agent_sandboxes
        WHERE state != 'reclaimed'
        """
    ).fetchall()
    conn.close()

    processes = scan_processes(str(root))

    report = {
        "sandbox_root": str(root),
        "sandbox_dirs": remaining_dirs,
        "non_reclaimed_rows": expired_rows,
        "processes": processes,
    }

    out = ROOT / "docs" / "evidence" / "w34-residue.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    ok = not remaining_dirs and not expired_rows and not processes
    print(json.dumps({"ok": ok, "out": str(out)}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
