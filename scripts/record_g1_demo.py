"""录制约 60 秒真实 G1 操作；临时会员和数据库，不发外部通知。

使用项目现有模型配置，可能产生少量模型费用。不得输出或复制任何密钥。
"""

from __future__ import annotations

import http.client
import json
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.auth_store import AuthStore

EVIDENCE = ROOT / "docs" / "evidence"
BACKEND_PORT = 8000
FRONTEND_PORT = 5173


def ensure_port_free(port: int) -> None:
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError(f"端口 {port} 已被占用，未接管现有服务")


def wait_http(port: int, path: str, process: subprocess.Popen) -> None:
    for _ in range(200):
        if process.poll() is not None:
            raise RuntimeError(f"端口 {port} 的服务提前退出")
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", path)
            response = conn.getresponse()
            response.read()
            conn.close()
            if response.status == 200:
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"端口 {port} 的服务启动超时")


def stop_tree(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main() -> None:
    ensure_port_free(BACKEND_PORT)
    ensure_port_free(FRONTEND_PORT)
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    backend = frontend = recorder = None

    with tempfile.TemporaryDirectory(prefix=".g1-demo-", dir=ROOT) as temp:
        temp_path = Path(temp)
        username = "g1_demo"
        password = secrets.token_urlsafe(18)
        store = AuthStore(temp_path / "auth_users.db")
        owner = store.bootstrap_owner("g1_owner", secrets.token_urlsafe(18))
        invite = store.issue_invite(owner["id"], role="member", hours=1)
        member = store.register(username, password, invite)

        env = os.environ.copy()
        env.update({
            "PERSONAL_AGENT_DATA_DIR": temp,
            "NTFY_SERVER": "http://127.0.0.1:1",
            "NTFY_TOPIC": "g1-demo-no-delivery",
            "G1_FRONTEND_URL": f"http://127.0.0.1:{FRONTEND_PORT}",
            "G1_DEMO_USERNAME": username,
            "G1_DEMO_PASSWORD": password,
            "G1_VIDEO_DIR": str(temp_path / "videos"),
        })
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0

        with (
            (EVIDENCE / "d9-demo-backend.log").open("w", encoding="utf-8") as backend_log,
            (EVIDENCE / "d9-demo-frontend.log").open("w", encoding="utf-8") as frontend_log,
        ):
            try:
                backend = subprocess.Popen(
                    [
                        str(ROOT / ".venv" / "Scripts" / "python.exe"),
                        "-c",
                        "import uvicorn; from src.main import app; "
                        "uvicorn.run(app, host='127.0.0.1', port=8000, log_level='warning')",
                    ],
                    cwd=ROOT,
                    env=env,
                    stdout=backend_log,
                    stderr=subprocess.STDOUT,
                    creationflags=flags,
                )
                wait_http(BACKEND_PORT, "/health", backend)

                frontend = subprocess.Popen(
                    [
                        "npm.cmd", "run", "dev", "--",
                        "--host", "127.0.0.1", "--port", str(FRONTEND_PORT),
                        "--strictPort",
                    ],
                    cwd=ROOT / "frontend",
                    env=env,
                    stdout=frontend_log,
                    stderr=subprocess.STDOUT,
                    creationflags=flags,
                )
                wait_http(FRONTEND_PORT, "/login", frontend)

                recorder = subprocess.Popen(
                    [
                        shutil.which("node") or "node.cmd",
                        str(ROOT / "scripts" / "record_g1_demo.mjs"),
                    ],
                    cwd=ROOT,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=flags,
                )
                stdout, stderr = recorder.communicate(timeout=120)
                if recorder.returncode != 0:
                    raise RuntimeError(f"浏览器录屏失败：{stderr.strip()}")
                result = json.loads(stdout)

                temporary_video = Path(result.pop("temporary_video_path"))
                final_video = EVIDENCE / "d9-g1-demo.webm"
                shutil.move(str(temporary_video), final_video)

                with closing(sqlite3.connect(temp_path / "chat_requests.db")) as conn:
                    run_row = conn.execute(
                        "SELECT id, status FROM agent_runs ORDER BY created_at DESC LIMIT 1"
                    ).fetchone()
                    exec_rows = conn.execute(
                        """
                        SELECT e.tool_name, e.status FROM agent_execs e
                        JOIN agent_steps s ON s.id = e.step_id
                        JOIN agent_turns t ON t.id = s.turn_id
                        WHERE t.run_id = ?
                        """,
                        (run_row[0],),
                    ).fetchall()
                with closing(sqlite3.connect(temp_path / "reminders.db")) as conn:
                    reminders = conn.execute(
                        """
                        SELECT message, delivery, status FROM reminder_requests
                        WHERE user_id = ?
                        """,
                        (member["id"],),
                    ).fetchall()

                assert run_row[1] == "succeeded", run_row
                assert exec_rows == [("create_reminder", "succeeded")], exec_rows
                assert len(reminders) == 1, reminders
                assert reminders[0][0] == "喝水", reminders
                assert reminders[0][1] == "in_app", reminders
                assert 58_000 <= result["recorded_elapsed_ms"] <= 75_000, result

                result.update({
                    "status": "PASS",
                    "real_external_model_called": True,
                    "video_file": final_video.name,
                    "run_status": run_row[1],
                    "exec_status": exec_rows[0][1],
                    "temporary_reminders_created": len(reminders),
                    "external_notification_sent": False,
                    "temporary_services_stopped": True,
                })
                (EVIDENCE / "d9-recording.txt").write_text(
                    json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                print(json.dumps(result, ensure_ascii=False, indent=2))
            finally:
                stop_tree(recorder)
                stop_tree(frontend)
                stop_tree(backend)


if __name__ == "__main__":
    main()
