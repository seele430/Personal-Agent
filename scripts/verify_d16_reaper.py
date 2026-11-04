"""D16 验收：真实服务里后台 reaper 线程会自己回收过期沙箱。

与「惰性过期」区分：这里**不访问 API**，直接在服务进程外读 ``platform.db``，
确认过期沙箱是由后台线程置为 reclaimed 的（reclaim_reason=ttl_expired）。

用法：

    .venv/Scripts/python.exe scripts/verify_d16_reaper.py
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EVIDENCE = ROOT / "docs" / "evidence" / "d16-reaper.json"

REAPER_INTERVAL_SECONDS = 0.5
SANDBOX_TTL_SECONDS = 1


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_health(port: int, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 60

    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("平台服务提前退出")

        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=2
            ) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.1)

    raise RuntimeError("平台服务启动超时")


def post(port: int, path: str, body: dict, headers: dict | None = None):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read())


def get_status(port: int, path: str) -> int:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")

    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def sandbox_row(db_path: Path, sandbox_id: str) -> dict:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM agent_sandboxes WHERE id = ?", (sandbox_id,)
        ).fetchone()

    return {} if row is None else dict(row)


def main() -> int:
    port = free_port()

    with tempfile.TemporaryDirectory(prefix="personal-agent-reaper-") as raw:
        temp = Path(raw)
        data_dir = temp / "data"
        data_dir.mkdir()
        db_path = data_dir / "platform.db"

        env = os.environ.copy()
        env.update(
            {
                "PERSONAL_AGENT_DATA_DIR": str(data_dir),
                "PERSONAL_AGENT_SANDBOX_ROOT": str(temp / "sandboxes"),
                "PERSONAL_AGENT_TEST_MODE": "1",
                "PERSONAL_AGENT_PLATFORM_REAPER": "1",
                "PERSONAL_AGENT_PLATFORM_REAPER_INTERVAL": str(
                    REAPER_INTERVAL_SECONDS
                ),
                "OPENAI_API_KEY": "invalid-local-test-key",
                "OPENAI_BASE_URL": "https://api.deepseek.com",
                "MODEL": "deepseek-flash",
                "NTFY_SERVER": "http://127.0.0.1:1",
                "NTFY_TOPIC": "d16-live-topic",
                "PYTHONPATH": str(ROOT),
                "PYTHONIOENCODING": "utf-8",
            }
        )

        log_path = ROOT / "docs" / "evidence" / "d16-reaper-server.log"

        with log_path.open("w+", encoding="utf-8") as log:
            server = subprocess.Popen(
                [
                    sys.executable, "-m", "uvicorn", "src.main:app",
                    "--host", "127.0.0.1", "--port", str(port),
                    "--log-level", "warning",
                ],
                cwd=str(ROOT),
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )

            try:
                wait_health(port, server)

                status, sandbox = post(
                    port,
                    "/v1/sandboxes",
                    {
                        "tier": "tier1",
                        "workspace_id": "ws_reaper",
                        "ttl_seconds": SANDBOX_TTL_SECONDS,
                    },
                    {"Idempotency-Key": "reaper-live-001"},
                )
                assert status == 201, sandbox

                sandbox_id = sandbox["sandbox_id"]
                before = sandbox_row(db_path, sandbox_id)

                # 关键：这里不访问 API，只等服务进程里的后台线程自己动手。
                deadline = time.monotonic() + 30
                row = before

                while time.monotonic() < deadline:
                    row = sandbox_row(db_path, sandbox_id)

                    if row.get("state") == "reclaimed":
                        break

                    time.sleep(0.2)

                after_status = get_status(
                    port, f"/v1/sandboxes/{sandbox_id}"
                )

                report = {
                    "status": "PASS" if row.get("state") == "reclaimed" else "FAIL",
                    "sandbox_id": sandbox_id,
                    "ttl_seconds": SANDBOX_TTL_SECONDS,
                    "reaper_interval_seconds": REAPER_INTERVAL_SECONDS,
                    "state_before": before.get("state"),
                    "state_after": row.get("state"),
                    "reclaim_reason": row.get("reclaim_reason"),
                    "reclaimed_at": row.get("reclaimed_at"),
                    "api_status_after_reclaim": after_status,
                    "proved_by": "background reaper thread (no API access before read)",
                }

                assert report["state_before"] == "created", report
                assert report["state_after"] == "reclaimed", report
                assert report["reclaim_reason"] == "ttl_expired", report
                assert after_status == 410, report

                EVIDENCE.write_text(
                    json.dumps(report, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0

            finally:
                if server.poll() is None:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(server.pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                    try:
                        server.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        server.kill()


if __name__ == "__main__":
    raise SystemExit(main())
