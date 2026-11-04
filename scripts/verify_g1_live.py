"""G1 真实模型 HTTP 验收；临时身份和数据库，不发送外部通知。

使用项目现有模型配置，可能产生少量模型费用。不得输出或复制任何密钥。
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import sqlite3
import subprocess
import tempfile
import time
import uuid
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER_CODE = r"""
from src.auth_api import get_current_user
from src.main import app
import os
import uvicorn

app.dependency_overrides[get_current_user] = lambda: {
    "id": "g1-live-local-user", "role": "member", "username": "g1-local"
}
uvicorn.run(app, host="127.0.0.1", port=int(os.environ["TEST_PORT"]), log_level="warning")
"""


def request(port, method, path, payload=None, timeout=90):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    body = json.dumps(payload, ensure_ascii=False) if payload is not None else None
    conn.request(
        method, path, body=body.encode("utf-8") if body else None,
        headers={"Content-Type": "application/json; charset=utf-8"} if body else {},
    )
    return conn, conn.getresponse()


def main():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="personal-agent-g1-live-") as temp:
        request_id = "g1_live_" + uuid.uuid4().hex
        env = os.environ.copy()
        env.update({
            "PERSONAL_AGENT_DATA_DIR": temp,
            "NTFY_SERVER": "http://127.0.0.1:1",
            "NTFY_TOPIC": "g1-local-no-delivery",
            "TEST_PORT": str(port),
        })
        log_path = ROOT / "docs" / "evidence" / "d9-live-server.log"
        with log_path.open("w+", encoding="utf-8") as log:
            process = subprocess.Popen(
                [str(ROOT / ".venv" / "Scripts" / "python.exe"), "-c", SERVER_CODE],
                cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
            )
            try:
                for _ in range(100):
                    if process.poll() is not None:
                        raise RuntimeError("G1 服务提前退出")
                    try:
                        conn, response = request(port, "GET", "/health", timeout=2)
                        response.read()
                        conn.close()
                        if response.status == 200:
                            break
                    except OSError:
                        time.sleep(0.05)
                else:
                    raise RuntimeError("G1 服务启动超时")

                question = "请使用工具创建一个 5 分钟后的提醒，内容是“喝水”。创建后简短确认。"
                conn, response = request(port, "POST", "/chat", {
                    "request_id": request_id, "message": question, "stream": True,
                })
                assert response.status == 200, response.read().decode("utf-8")
                events = []
                data_lines = []
                while True:
                    raw = response.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8").rstrip("\r\n")
                    if line.startswith("data:"):
                        data_lines.append(line[5:].strip())
                    elif not line and data_lines:
                        events.append(json.loads("\n".join(data_lines)))
                        data_lines.clear()
                conn.close()
                if data_lines:
                    events.append(json.loads("\n".join(data_lines)))

                types = [event["type"] for event in events]
                done = next((event for event in events if event["type"] == "done"), None)
                tool = next((event for event in events if event["type"] == "tool_finished"), None)
                assert done is not None, events
                assert tool is not None, events
                assert tool["result"].get("success") is True, events

                db_path = Path(temp) / "chat_requests.db"
                with closing(sqlite3.connect(db_path)) as conn:
                    run_row = conn.execute(
                        "SELECT id, status FROM agent_runs WHERE request_id = ?",
                        (request_id,),
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
                with closing(sqlite3.connect(Path(temp) / "reminders.db")) as conn:
                    reminder_count = conn.execute(
                        """
                        SELECT COUNT(*) FROM reminder_requests
                        WHERE user_id = 'g1-live-local-user' AND delivery = 'in_app'
                        """
                    ).fetchone()[0]
                assert run_row[1] == "succeeded"
                assert exec_rows == [("create_reminder", "succeeded")]
                assert reminder_count == 1

                print(json.dumps({
                    "status": "PASS",
                    "real_external_model_called": True,
                    "question": question,
                    "event_sequence": types,
                    "answer": done["reply"],
                    "tool_name": tool["tool"],
                    "tool_result": tool["result"],
                    "run_status": run_row[1],
                    "exec_status": exec_rows[0][1],
                    "reminders_created_in_temporary_db": reminder_count,
                    "external_notification_sent": False,
                }, ensure_ascii=False, indent=2))
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                time.sleep(0.1)
                if process.returncode not in (0, 1, -15):
                    log.seek(0)
                    print(log.read(), file=__import__("sys").stderr)


if __name__ == "__main__":
    main()
