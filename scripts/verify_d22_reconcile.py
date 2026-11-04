"""D22 验收：真实进程重启后，孤儿资源被对账清掉。

步骤（全部对真实文件与 SQLite 生效）：

1. 起服务，创建沙箱，记下 id；
2. 杀掉服务（模拟崩溃，不 destroy）；
3. 人为造孤儿：删掉它的工作区目录、插一条 running 的 exec、放一个
   ``sbx_`` 命名但没有数据库记录的目录；
4. 重启服务（lifespan 里跑启动对账）；
5. 直接读 platform.db：沙箱 failed/workspace_missing、exec failed/INTERRUPTED、
   孤儿目录消失。

用法：

    .venv/Scripts/python.exe scripts/verify_d22_reconcile.py
"""

from __future__ import annotations

import json
import os
import shutil
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

EVIDENCE = ROOT / "docs" / "evidence" / "d22-reconcile.json"

ORPHAN_ID = "sbx_" + "f" * 32


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_server(port: int, env: dict, log_path: Path) -> subprocess.Popen:
    log = log_path.open("a", encoding="utf-8")
    process = subprocess.Popen(
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
    return process


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


def stop_server(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return

    subprocess.run(
        ["taskkill", "/F", "/T", "/PID", str(process.pid)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )

    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()


def post_json(port: int, path: str, body: dict, headers: dict | None = None):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read())


def row(db_path: Path, sql: str, parameters=()) -> list[dict]:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(item) for item in conn.execute(sql, parameters).fetchall()]


def main() -> int:
    port = free_port()

    with tempfile.TemporaryDirectory(prefix="personal-agent-d22-") as raw:
        temp = Path(raw)
        data_dir = temp / "data"
        data_dir.mkdir()
        sandbox_root = temp / "sandboxes"
        db_path = data_dir / "platform.db"
        log_path = ROOT / "docs" / "evidence" / "d22-reconcile-server.log"

        env = os.environ.copy()
        env.update(
            {
                "PERSONAL_AGENT_DATA_DIR": str(data_dir),
                "PERSONAL_AGENT_SANDBOX_ROOT": str(sandbox_root),
                "PERSONAL_AGENT_TEST_MODE": "1",
                "PERSONAL_AGENT_PLATFORM_REAPER": "0",
                "OPENAI_API_KEY": "invalid-local-test-key",
                "OPENAI_BASE_URL": "https://api.deepseek.com",
                "MODEL": "deepseek-flash",
                "NTFY_SERVER": "http://127.0.0.1:1",
                "NTFY_TOPIC": "d22-live-topic",
                "PYTHONPATH": str(ROOT),
                "PYTHONIOENCODING": "utf-8",
            }
        )

        first = start_server(port, env, log_path)

        try:
            wait_health(port, first)
            status, sandbox = post_json(
                port,
                "/v1/sandboxes",
                {
                    "tier": "tier1",
                    "workspace_id": "ws_reconcile",
                    "ttl_seconds": 600,
                },
                {"Idempotency-Key": "d22-reconcile-001"},
            )
            assert status == 201, sandbox
            sandbox_id = sandbox["sandbox_id"]
        finally:
            stop_server(first)

        # 人为制造三种孤儿状态
        shutil.rmtree(sandbox_root / sandbox_id, ignore_errors=True)

        orphan_dir = sandbox_root / ORPHAN_ID
        orphan_dir.mkdir(parents=True, exist_ok=True)

        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO platform_execs(
                    id, sandbox_id, run_id, step_id, state, argv_json,
                    timeout_ms, max_output_bytes, created_at
                ) VALUES (?, ?, NULL, NULL, 'running', ?, 1000, 1024, ?)
                """,
                (
                    "exec_interrupted_d22",
                    sandbox_id,
                    '["python", "-c", "print(1)"]',
                    "2026-11-04T00:00:00+00:00",
                ),
            )

        # 重启：lifespan 里的启动对账应该把上面三种都收敛
        second = start_server(port, env, log_path)

        try:
            wait_health(port, second)

            sandboxes = row(
                db_path,
                "SELECT state, fail_reason FROM agent_sandboxes WHERE id = ?",
                (sandbox_id,),
            )
            execs = row(
                db_path,
                "SELECT state, error_code FROM platform_execs WHERE id = ?",
                ("exec_interrupted_d22",),
            )

            report = {
                "status": "PASS",
                "sandbox_id": sandbox_id,
                "sandbox_state": sandboxes[0]["state"],
                "sandbox_fail_reason": sandboxes[0]["fail_reason"],
                "exec_state": execs[0]["state"],
                "exec_error_code": execs[0]["error_code"],
                "orphan_dir_removed": not orphan_dir.exists(),
                "server_alive_after_reconcile": second.poll() is None,
            }

            assert report["sandbox_state"] == "failed", report
            assert report["sandbox_fail_reason"] == "workspace_missing", report
            assert report["exec_state"] == "failed", report
            assert report["exec_error_code"] == "INTERRUPTED_BY_RESTART", report
            assert report["orphan_dir_removed"] is True, report

            EVIDENCE.write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0

        finally:
            stop_server(second)


if __name__ == "__main__":
    raise SystemExit(main())
