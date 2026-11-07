"""真实 TLS 端到端验证：起带自签名证书的 uvicorn，用真实客户端打四条路径。

    .\\.venv/Scripts/python.exe scripts/verify_remote_tls.py

证明：证书 / TLS / 守卫 / 限速 / 访问日志在**真实网络栈**上串得起来。
不证明：手机端证书信任、真实 LAN 路由（那需要真机）。

脚本自己在 finally 里杀进程，不留后台任务。
"""

from __future__ import annotations

import json
import os
import socket
import ssl
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

from src.remote.cert import generate_self_signed  # noqa: E402


TOKEN = "t" * 40
HOST = "127.0.0.1"
PORT = 18443
BASE = f"https://{HOST}:{PORT}"


def free_port(preferred: int) -> int:
    with socket.socket() as probe:
        try:
            probe.bind((HOST, preferred))

            return preferred

        except OSError:
            probe.bind((HOST, 0))

            return probe.getsockname()[1]


def request(path: str, *, token: str = "", method: str = "GET") -> dict:
    context = ssl._create_unverified_context()
    headers = {}

    if token:
        headers["Authorization"] = f"Bearer {token}"

    payload = urllib.request.Request(
        f"{BASE}{path}", headers=headers, method=method
    )

    try:
        with urllib.request.urlopen(payload, context=context, timeout=10) as response:
            body = response.read().decode("utf-8", "replace")

            return {
                "status": response.status,
                "retry_after": response.headers.get("Retry-After"),
                "body": body[:160],
            }

    except urllib.error.HTTPError as error:
        return {
            "status": error.code,
            "retry_after": error.headers.get("Retry-After"),
            "body": error.read().decode("utf-8", "replace")[:160],
        }

    except Exception as error:  # noqa: BLE001 - 连接失败也要记录
        return {"status": 0, "retry_after": None, "body": f"{type(error).__name__}: {error}"}


def wait_ready(process: subprocess.Popen, *, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False

        result = request("/health")

        if result["status"] in (200, 401, 403):
            return True

        time.sleep(0.4)

    return False


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="pa-tls-e2e-"))
    data_dir = work / "data"
    data_dir.mkdir()
    tls_dir = work / "tls"

    cert, key = generate_self_signed(ip=HOST, out_dir=tls_dir)
    port = free_port(PORT)
    base = f"https://{HOST}:{port}"

    # 造一个 owner 账号，并把它的 id 配给远程模式（owner 鉴权）
    from src.config import build_paths
    from src.core.auth_store import AuthStore

    isolated_paths = build_paths(data_dir)
    store = AuthStore(isolated_paths.auth_users_db)
    store.init_db()
    owner_id = store.bootstrap_owner("remoteowner", "tls-e2e-pass-1")["id"]

    env = dict(os.environ)
    env.update(
        {
            "PERSONAL_AGENT_DATA_DIR": str(data_dir),
            "PERSONAL_AGENT_REMOTE": "1",
            "PERSONAL_AGENT_REMOTE_TOKEN": TOKEN,
            "PERSONAL_AGENT_REMOTE_OWNER": owner_id,
            "PERSONAL_AGENT_REMOTE_LOG": str(work / "access.jsonl"),
            "PERSONAL_AGENT_REMOTE_MAX_FAILURES": "10",
            "NO_PROXY": "*",
        }
    )

    process = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "src.main:app",
            "--host", HOST, "--port", str(port),
            "--ssl-keyfile", str(key), "--ssl-certfile", str(cert),
        ],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    global BASE
    BASE = base
    results = {}

    try:
        if not wait_ready(process):
            output = ""

            if process.poll() is not None and process.stdout:
                output = process.stdout.read()[-800:]

            print("服务没起来：", output)

            return 1

        results["no_token"] = request("/api/artifacts")
        results["write_method"] = request("/api/artifacts", token=TOKEN, method="POST")
        results["read_only"] = request("/api/artifacts", token=TOKEN)
        results["not_whitelisted"] = request("/api/admin/users", token=TOKEN)

        for _ in range(11):
            request("/api/artifacts")

        results["after_failures"] = request("/api/artifacts")

    finally:
        process.terminate()

        try:
            process.wait(timeout=10)

        except subprocess.TimeoutExpired:
            process.kill()

    log_path = work / "access.jsonl"
    log_lines = log_path.read_text(encoding="utf-8").splitlines() if log_path.is_file() else []
    token_leaked = any(TOKEN in line for line in log_lines)

    checks = {
        # 路由可达即可（此前是整个 /api 命名空间 404）；是否 200 取决于登录会话
        "route_reachable": results.get("read_only", {}).get("status") in (200, 401),
        "no_token_401": results.get("no_token", {}).get("status") == 401,
        "write_403": results.get("write_method", {}).get("status") == 403,
        "path_403": results.get("not_whitelisted", {}).get("status") == 403,
        "rate_limited_429": results.get("after_failures", {}).get("status") == 429,
        "retry_after_header": bool(results.get("after_failures", {}).get("retry_after")),
        "log_written": len(log_lines) > 0,
        "no_token_in_log": not token_leaked,
    }

    # 真缺口：远程令牌只过守卫，不等于登录会话；没有会话时应用层仍 401。
    session_required = results.get("read_only", {}).get("status") == 401

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base": base,
        "results": results,
        "checks": checks,
        "session_still_required": session_required,
        "log_lines": len(log_lines),
    }

    out = Path(
        r"C:\Users\123\Documents\deepseek-harness\default-workspace"
        r"\pa-v122-evidence-20261108"
    )
    out.mkdir(parents=True, exist_ok=True)
    (out / "tls-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if log_path.is_file():
        (out / "access.jsonl").write_text(
            log_path.read_text(encoding="utf-8"), encoding="utf-8"
        )

    for name, status in results.items():
        print(f"{name}: {status['status']} retry_after={status['retry_after']}")

    print("checks:", checks)

    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
