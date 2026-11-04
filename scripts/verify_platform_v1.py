"""D15 续：用 D10 的 Python SDK 打真实 /v1 服务（真实 HTTP + 真实子进程）。

覆盖：幂等 create（两次同 key）、exec 成功、exec 超时、exec 取消、destroy，
以及销毁后的 410。SDK 用的是它自带的 ``UrllibTransport``，不是替身。

用法：

    .venv/Scripts/python.exe scripts/verify_platform_v1.py
"""

from __future__ import annotations

import http.client
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

SDK_PATH = ROOT / "sdk" / "python"

if str(SDK_PATH) not in sys.path:
    sys.path.insert(0, str(SDK_PATH))

from personal_agent_platform import PlatformClient  # noqa: E402

EVIDENCE = ROOT / "docs" / "evidence" / "d15-platform-v1-sdk.json"


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


def get_status(url: str) -> tuple[int, dict]:
    request = urllib.request.Request(url, method="GET")

    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def main() -> int:
    port = free_port()

    with tempfile.TemporaryDirectory(prefix="personal-agent-v1-") as raw:
        temp = Path(raw)
        data_dir = temp / "data"
        data_dir.mkdir()

        env = os.environ.copy()
        env.update(
            {
                "PERSONAL_AGENT_DATA_DIR": str(data_dir),
                "PERSONAL_AGENT_SANDBOX_ROOT": str(temp / "sandboxes"),
                "PERSONAL_AGENT_TEST_MODE": "1",
                "OPENAI_API_KEY": "invalid-local-test-key",
                "OPENAI_BASE_URL": "https://api.deepseek.com",
                "MODEL": "deepseek-flash",
                "NTFY_SERVER": "http://127.0.0.1:1",
                "NTFY_TOPIC": "d15-live-topic",
                "PYTHONPATH": str(ROOT),
                "PYTHONIOENCODING": "utf-8",
            }
        )

        log_path = ROOT / "docs" / "evidence" / "d15-platform-v1-server.log"

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

                client = PlatformClient(
                    f"http://127.0.0.1:{port}",
                    poll_interval_seconds=0.1,
                    request_timeout_seconds=30,
                )

                first = client.create(
                    tier="tier1",
                    workspace_id="ws_sdk_live",
                    ttl_seconds=600,
                    idempotency_key="sdk-live-001",
                )
                second = client.create(
                    tier="tier1",
                    workspace_id="ws_sdk_live",
                    ttl_seconds=600,
                    idempotency_key="sdk-live-001",
                )

                echo = client.exec(
                    first.sandbox_id,
                    [sys.executable, "-c", "print('sdk-ok')"],
                    run_id="run_sdk_live",
                    step_id="step_sdk_live",
                    timeout_ms=20000,
                )

                timed = client.exec(
                    first.sandbox_id,
                    [sys.executable, "-c", "import time; time.sleep(10)"],
                    run_id="run_sdk_live",
                    step_id="step_sdk_timeout",
                    timeout_ms=600,
                )

                long_run = client.exec(
                    first.sandbox_id,
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    run_id="run_sdk_live",
                    step_id="step_sdk_cancel",
                    timeout_ms=60000,
                    wait=False,
                )
                client.cancel(long_run.exec_id)
                cancelled = client.get_exec(long_run.exec_id)

                # 等取消真正落地，再断言终态
                for _ in range(200):
                    cancelled = client.get_exec(long_run.exec_id)
                    if cancelled.state == "cancelled":
                        break
                    time.sleep(0.05)

                destroyed = client.destroy(first.sandbox_id)
                gone_status, gone_body = get_status(
                    f"http://127.0.0.1:{port}/v1/sandboxes/{first.sandbox_id}"
                )

                report = {
                    "status": "PASS",
                    "idempotent_create_same_id": (
                        first.sandbox_id == second.sandbox_id
                    ),
                    "sandbox_id": first.sandbox_id,
                    "exec_state": echo.state,
                    "exec_stdout": echo.stdout.strip(),
                    "exec_result_code": echo.result_code,
                    "timeout_state": timed.state,
                    "timeout_result_code": timed.result_code,
                    "cancel_state": cancelled.state,
                    "cancel_result_code": cancelled.result_code,
                    "destroy_state": destroyed.state,
                    "after_destroy_http_status": gone_status,
                    "after_destroy_error_code": gone_body.get("error", {}).get("code"),
                }

                assert report["idempotent_create_same_id"] is True
                assert echo.state == "succeeded", echo
                assert "sdk-ok" in echo.stdout, echo
                assert timed.state == "timed_out", timed
                assert cancelled.state == "cancelled", cancelled
                assert destroyed.state == "reclaimed", destroyed
                assert gone_status == 410, (gone_status, gone_body)

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
