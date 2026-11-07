"""黄金路径端到端：真实 HTTP、真实登录、真实产物链路。

    登录 → 登记产物 → 列表 → 详情 → 预览 → 下载 → Diff → 时间线

任何一步非 200 就失败——这正是 2026-11-08 那次 `/api/*` 全 404 事故的
防回归脚本（当时所有单测与 Playwright 都是绿的）。
"""

from __future__ import annotations

import http.cookiejar
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

HOST = "127.0.0.1"
USERNAME = "goldenuser"
PASSWORD = "golden-pass-123"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind((HOST, 0))

        return probe.getsockname()[1]


class Client:
    def __init__(self, base: str):
        self.base = base
        self.token = ""
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, bytes]:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}

        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        request = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            method=method,
            headers=headers,
        )

        try:
            with self.opener.open(request, timeout=15) as response:
                return response.status, response.read()

        except urllib.error.HTTPError as error:
            return error.code, error.read()


def wait_ready(base: str, process: subprocess.Popen, *, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False

        try:
            with urllib.request.urlopen(f"{base}/health", timeout=3):
                return True

        except Exception:  # noqa: BLE001 - 还没起来
            time.sleep(0.4)

    return False


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="pa-golden-"))
    data_dir = work / "data"
    artifacts = data_dir / "artifacts"
    artifacts.mkdir(parents=True)

    (artifacts / "v1.md").write_text("# 报告\n\n第一版。\n", encoding="utf-8")
    (artifacts / "v2.md").write_text("# 报告\n\n第二版。\n新增一行。\n", encoding="utf-8")

    from src.config import build_paths
    from src.core.auth_store import AuthStore

    store = AuthStore(build_paths(data_dir).auth_users_db)
    store.init_db()
    store.bootstrap_owner(USERNAME, PASSWORD)

    port = free_port()
    base = f"http://{HOST}:{port}"

    env = dict(os.environ)
    env.update({"PERSONAL_AGENT_DATA_DIR": str(data_dir), "NO_PROXY": "*"})

    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "src.main:app",
         "--host", HOST, "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )

    steps: list[dict] = []
    client = Client(base)

    def step(name: str, method: str, path: str, body: dict | None = None) -> tuple[bool, dict]:
        status, raw = client.call(method, path, body)
        preview = raw[:160].decode("utf-8", "replace")
        steps.append({"step": name, "method": method, "path": path, "status": status})
        print(f"{name}: {status} {preview[:80]}")

        payload = {}

        try:
            payload = json.loads(raw)

        except json.JSONDecodeError:
            pass

        return status == 200, payload

    try:
        if not wait_ready(base, process):
            print("服务没起来")

            return 1

        ok, _ = step("health", "GET", "/health")

        if not ok:
            return 1

        ok, login_payload = step("login", "POST", "/auth/login", {"username": USERNAME, "password": PASSWORD})

        if not ok:
            return 1

        # 登录返回 bearer token（不是 cookie），后续请求必须带上
        client.token = str(login_payload.get("token") or "")

        if not client.token:
            print("登录没有返回 token")

            return 1

        ok, first = step("register-v1", "POST", "/api/artifacts/register", {"path": "v1.md", "title": "黄金路径 v1"})

        if not ok:
            return 1

        artifact_id = first.get("id", "")

        checks = [
            step("list", "GET", "/api/artifacts"),
            step("detail", "GET", f"/api/artifacts/{artifact_id}"),
            step("preview", "GET", f"/api/artifacts/{artifact_id}/preview"),
            step("download", "GET", f"/api/artifacts/{artifact_id}/content"),
        ]

        ok, second = step("register-v2", "POST", "/api/artifacts/register", {"path": "v2.md", "title": "黄金路径 v2"})
        checks.append((ok, second))

        checks.append(step("diff", "GET", f"/api/artifacts/{artifact_id}/diff/{second.get('id','')}"))
        checks.append(step("timeline", "GET", "/api/activity/timeline?limit=10"))

    finally:
        process.terminate()

        try:
            process.wait(timeout=10)

        except subprocess.TimeoutExpired:
            process.kill()

    passed = all(ok for ok, _ in checks) and len(steps) >= 9
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base": base,
        "steps": steps,
        "passed": passed,
    }

    out = Path(
        r"C:\Users\123\Documents\deepseek-harness\default-workspace"
        r"\pa-v125-evidence-20261108"
    )
    out.mkdir(parents=True, exist_ok=True)
    (out / "golden-path.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"steps={len(steps)} passed={passed}")
    print(f"证据：{out / 'golden-path.json'}")

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
