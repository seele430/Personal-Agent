"""D30 背压演示：真实服务上的「排队 → 429 → 排队超时」。

用法：

    .\\venv\\Scripts\\python.exe scripts\\verify_d30_queue.py

脚本自己选端口、自己建临时数据目录，不碰真实库。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EVIDENCE = ROOT / "docs" / "evidence" / "d30-demo.json"

SLEEP = [sys.executable, "-c", "import time; time.sleep(4)"]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_server(port: int, env: dict, log_path: Path) -> subprocess.Popen:
    log = log_path.open("w+", encoding="utf-8")
    return subprocess.Popen(
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


def wait_health(port: int, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 60

    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("服务提前退出")

        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=2
            ) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.1)

    raise RuntimeError("服务启动超时")


def request(port: int, method: str, path: str, body: dict | None = None, headers: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method,
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}"), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}"), dict(exc.headers)


def main() -> int:
    port = free_port()

    with tempfile.TemporaryDirectory(prefix="personal-agent-d30-") as raw:
        temp = Path(raw)
        data_dir = temp / "data"
        data_dir.mkdir()
        log_path = ROOT / "docs" / "evidence" / "d30-demo-server.log"

        env = os.environ.copy()
        env.update(
            {
                "PERSONAL_AGENT_DATA_DIR": str(data_dir),
                "PERSONAL_AGENT_SANDBOX_ROOT": str(temp / "sandboxes"),
                "PERSONAL_AGENT_TEST_MODE": "1",
                "PERSONAL_AGENT_PLATFORM_REAPER": "0",
                "PERSONAL_AGENT_PLATFORM_MAX_ACTIVE_EXECS": "1",
                "PERSONAL_AGENT_PLATFORM_QUEUE_SIZE": "1",
                "PERSONAL_AGENT_PLATFORM_QUEUE_MAX_WAIT": "2",
                "PERSONAL_AGENT_PLATFORM_QUEUE_SWEEP": "0.3",
                "OPENAI_API_KEY": "invalid-local-test-key",
                "OPENAI_BASE_URL": "https://api.deepseek.com",
                "MODEL": "deepseek-flash",
                "NTFY_SERVER": "http://127.0.0.1:1",
                "NTFY_TOPIC": "d30-demo-topic",
                "PYTHONPATH": str(ROOT),
                "PYTHONIOENCODING": "utf-8",
            }
        )

        server = start_server(port, env, log_path)

        try:
            wait_health(port, server)

            status, sandbox, _ = request(
                port,
                "POST",
                "/v1/sandboxes",
                {"tier": "tier1", "workspace_id": "ws_d30", "ttl_seconds": 600},
                headers={"Idempotency-Key": "d30-demo-001"},
            )
            assert status == 201, sandbox
            sandbox_id = sandbox["sandbox_id"]

            _, first, _ = request(
                port, "POST", f"/v1/sandboxes/{sandbox_id}/exec",
                {"command": SLEEP, "timeout_ms": 30000},
            )
            _, second, _ = request(
                port, "POST", f"/v1/sandboxes/{sandbox_id}/exec",
                {"command": SLEEP, "timeout_ms": 30000},
            )

            status_full, full, full_headers = request(
                port, "POST", f"/v1/sandboxes/{sandbox_id}/exec",
                {"command": SLEEP, "timeout_ms": 30000},
            )

            _, queue, _ = request(port, "GET", "/v1/queue")

            deadline = time.monotonic() + 20
            second_state = None

            while time.monotonic() < deadline:
                _, body, _ = request(port, "GET", f"/v1/exec/{second['exec_id']}")
                second_state = body["state"]
                if second_state == "timed_out":
                    break
                time.sleep(0.1)

            report = {
                "status": "PASS",
                "first": first["state"],
                "second": second["state"],
                "third_http": status_full,
                "third_code": full["error"]["code"],
                "retry_after": full_headers.get("Retry-After") or full_headers.get("retry-after"),
                "queue": queue,
                "second_final": second_state,
            }

            assert report["first"] == "queued", report
            assert report["second"] == "queued", report
            assert report["third_http"] == 429, report
            assert report["third_code"] == "QUEUE_FULL", report
            assert report["retry_after"] is not None, report
            assert report["queue"]["active"] == 1, report
            assert report["queue"]["queued"] == 1, report
            assert report["second_final"] == "timed_out", report

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
