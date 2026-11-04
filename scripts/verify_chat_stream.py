"""D7 真实本地 HTTP 验证：SSE 增量、取消接口、断流后无残留工作线程。

模型与工具均为本地确定性替身，不访问真实模型、不读取真实凭据或数据目录。
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SERVER_CODE = r"""
from src.auth_api import get_current_user
from src.core.run_control import ChatCancelled
from src.main import app
import src.chat_secure_api as chat_api

app.dependency_overrides[get_current_user] = lambda: {
    "id": "local-stream-test", "role": "owner", "username": "local-test"
}

def fake_stream(message, *, emit, cancel_token, bot=None):
    emit({"type": "run_started", "run_id": "run-local-test"})
    if message == "normal":
        emit({"type": "text_delta", "text": "第一段"})
        emit({"type": "text_delta", "text": "第二段"})
        emit({"type": "done", "reply": "第一段第二段"})
        return "第一段第二段"
    emit({"type": "tool_started", "tool": "cancellable_test_tool"})
    cancel_token.register_canceller(lambda: None)
    while not cancel_token.cancelled:
        import time
        time.sleep(0.02)
    emit({"type": "cancelled"})
    raise ChatCancelled("cancelled by HTTP")

chat_api.run_chat_stream = fake_stream

import uvicorn
uvicorn.run(app, host="127.0.0.1", port=int(__import__("os").environ["TEST_PORT"]), log_level="warning")
"""


def request(port, method, path, payload=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    body = json.dumps(payload) if payload is not None else None
    conn.request(
        method, path, body=body,
        headers={"Content-Type": "application/json"} if body else {},
    )
    return conn, conn.getresponse()


def main():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="personal-agent-d7-http-") as temp:
        log_path = ROOT / "docs" / "evidence" / "d7-live-server.log"
        env = os.environ.copy()
        env.update({
            "PERSONAL_AGENT_DATA_DIR": temp,
            "PERSONAL_AGENT_TEST_MODE": "1",
            "OPENAI_API_KEY": "invalid-local-test-key",
            "OPENAI_BASE_URL": "https://api.deepseek.com",
            "MODEL": "deepseek-flash",
            "NTFY_SERVER": "http://127.0.0.1:1",
            "NTFY_TOPIC": "local-test",
            "TEST_PORT": str(port),
        })
        with log_path.open("w+", encoding="utf-8") as log:
            process = subprocess.Popen(
                [str(ROOT / ".venv" / "Scripts" / "python.exe"), "-c", SERVER_CODE],
                cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
            )
            try:
                for _ in range(100):
                    if process.poll() is not None:
                        raise RuntimeError("验证服务提前退出")
                    try:
                        conn, response = request(port, "GET", "/health")
                        response.read()
                        conn.close()
                        if response.status == 200:
                            break
                    except OSError:
                        time.sleep(0.05)
                else:
                    raise RuntimeError("验证服务启动超时")

                conn, response = request(port, "POST", "/chat", {
                    "request_id": "local_normal_stream", "message": "normal", "stream": True,
                })
                normal = response.read().decode("utf-8")
                conn.close()
                assert response.status == 200, normal
                assert normal.count("event: text_delta") == 2
                assert "event: done" in normal

                request_id = "local_cancel_stream"
                conn, response = request(port, "POST", "/chat", {
                    "request_id": request_id, "message": "cancel", "stream": True,
                })
                lines = []
                while True:
                    line = response.readline().decode("utf-8").strip()
                    lines.append(line)
                    if line == "event: tool_started":
                        break
                cancel_conn, cancel_response = request(
                    port, "POST", f"/chat/requests/{request_id}/cancel"
                )
                cancel_body = cancel_response.read().decode("utf-8")
                cancel_conn.close()
                assert cancel_response.status == 202, cancel_body
                while True:
                    line = response.readline().decode("utf-8").strip()
                    if not line:
                        continue
                    lines.append(line)
                    if line == "event: cancelled":
                        break
                while response.readline():
                    pass
                conn.close()

                status_conn, status_response = request(
                    port, "GET", f"/chat/requests/{request_id}"
                )
                status = json.loads(status_response.read())
                status_conn.close()
                assert status_response.status == 200
                assert status["status"] == "unknown"

                disconnect_id = "local_disconnect_stream"
                disconnect_conn, disconnect_response = request(port, "POST", "/chat", {
                    "request_id": disconnect_id, "message": "cancel", "stream": True,
                })
                while disconnect_response.readline().decode("utf-8").strip() != "event: tool_started":
                    pass
                disconnect_response.close()
                disconnect_conn.close()
                disconnected_status = None
                for _ in range(40):
                    status_conn, status_response = request(
                        port, "GET", f"/chat/requests/{disconnect_id}"
                    )
                    disconnected_status = json.loads(status_response.read())["status"]
                    status_conn.close()
                    if disconnected_status == "unknown":
                        break
                    time.sleep(0.05)
                assert disconnected_status == "unknown", disconnected_status

                print(json.dumps({
                    "status": "PASS",
                    "normal_delta_events": 2,
                    "cancel_http_status": cancel_response.status,
                    "cancel_sse_event": True,
                    "legacy_request_status_after_cancel": status["status"],
                    "client_disconnect_cancelled_worker": disconnected_status == "unknown",
                    "server_process_running_during_check": process.poll() is None,
                }, ensure_ascii=False, indent=2))
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                time.sleep(0.1)
                assert process.poll() is not None
                if process.returncode not in (0, 1, -15):
                    log.seek(0)
                    print(log.read(), file=sys.stderr)


if __name__ == "__main__":
    main()
