"""D11 真实 HTTP / SSE 验收：取消与断流必须清掉 runner 进程树。

三条断言，全部对真实进程和真实 HTTP 生效（模型调用是本地替身）：

1. **显式取消**：POST /chat 流式请求 → 工具 running → POST cancel → 平台 exec
   落 cancelled，探针的父进程与子进程都不再存活。
2. **直接断流**：读到 tool_started 就关闭连接 → worker 收到取消 → 同样清树。
3. **不误杀**：验收脚本自己起的无关进程必须还活着（只清平台拥有的进程）。

同时检查 trajectory 里「取消请求」先于「最终 cancelled」，且不存在其它终态。

用法：

    .venv/Scripts/python.exe scripts/verify_d11_cancel.py
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EVIDENCE = ROOT / "docs" / "evidence" / "d11-live-cancel.json"

# 探针：派生「父 + 子」两个进程，把 PID 写进唯一文件名，然后长眠。
PROBE_SOURCE = "\n".join(
    [
        "import json, os, subprocess, sys, time",
        "from pathlib import Path",
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])",
        "target = Path(sys.argv[1])",
        "target = target.with_name(target.name + '.' + str(os.getpid()) + '.json')",
        "target.write_text(",
        "    json.dumps({'parent': os.getpid(), 'child': child.pid}),",
        "    encoding='utf-8',",
        ")",
        "time.sleep(60)",
    ]
)

SERVER_CODE = """
import json, os, sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(os.environ["D11_ROOT"])
sys.path.insert(0, str(ROOT))

from src import agent
from src.auth_api import get_current_user
from src.main import app
from fastapi import Request as _Request

# 两个场景各用一个用户：取消会保留预算预留，同一个用户第二次请求会被
# 个人预算挡住（429 user_budget_exceeded），与取消本身无关。
def current_user(request: _Request):
    return {
        "id": request.headers.get("X-D11-User", "d11-live-user"),
        "role": "member",
        "username": "d11-live",
    }


app.dependency_overrides[get_current_user] = current_user

PROBE = os.environ["D11_PROBE_TOOL"]
PID_FILE = os.environ["D11_PID_FILE"]
PROBE_SOURCE = Path(os.environ["D11_PROBE_SOURCE"]).read_text(encoding="utf-8")


def host_handler(*args, **kwargs):
    raise AssertionError("宿主进程不应直接执行工具 handler")


agent.TOOL_REGISTRY.register(
    {
        "type": "function",
        "function": {
            "name": PROBE,
            "description": "D11 live probe",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    policy="allow",
    handler=host_handler,
    execution={"kind": "command", "argv": [sys.executable, "-c", PROBE_SOURCE, PID_FILE]},
)

CALLS = {"count": 0}


def chunk(text=None, finish=None, tool_calls=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=text, tool_calls=tool_calls or []),
                finish_reason=finish,
            )
        ]
    )


def tool_delta(name=None, arguments=None, call_id=None):
    return SimpleNamespace(
        index=0,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def fake_create(**kwargs):
    # 第一次调用（最后一条消息是用户输入）发起工具调用；
    # 之后拿到 tool 结果的调用直接给最终答复。用消息角色判断，不能用调用
    # 次数奇偶 —— 取消会让第二次调用根本不发生。
    messages = kwargs.get("messages") or []
    last = messages[-1] if messages else {}
    if last.get("role") == "user":
        return iter(
            [
                chunk(tool_calls=[tool_delta(name=PROBE, call_id="call-live")]),
                chunk(tool_calls=[tool_delta(arguments="{}")], finish="tool_calls"),
            ]
        )
    return iter([chunk(text="探针已完成", finish="stop")])


agent.client.chat.completions.create = fake_create

import uvicorn

uvicorn.run(
    app,
    host="127.0.0.1",
    port=int(os.environ["D11_PORT"]),
    log_level="warning",
)
"""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(
    port: int,
    method: str,
    path: str,
    payload: dict | None = None,
    user: str | None = None,
):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    body = json.dumps(payload) if payload is not None else None
    headers = {"Content-Type": "application/json"} if body else {}
    if user is not None:
        headers["X-D11-User"] = user
    conn.request(method, path, body=body, headers=headers)
    return conn, conn.getresponse()


def wait_event(response, name: str, *, limit: int = 400) -> None:
    seen: list[str] = []

    for _ in range(limit):
        line = response.readline()

        if not line:
            raise AssertionError(
                f"SSE 提前结束（status={response.status}），没等到 {name}；"
                f"已收到：{seen}"
            )

        text = line.decode("utf-8").strip()
        seen.append(text)

        if text == f"event: {name}":
            return

    raise AssertionError(f"SSE 里没有出现 {name}；已收到：{seen}")


def wait_pids(directory: Path, timeout: float = 30.0) -> tuple[Path, dict]:
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        files = sorted(directory.glob("pids.json.*.json"), key=lambda p: p.stat().st_mtime)

        if files:
            path = files[-1]
            try:
                return path, json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass

        time.sleep(0.02)

    raise AssertionError("探针没有写出 PID 文件")


def poll_request_status(
    port: int, request_id: str, wanted: str, user: str, timeout: float = 30.0
):
    deadline = time.monotonic() + timeout
    status = None

    while time.monotonic() < deadline:
        conn, response = request(
            port, "GET", f"/chat/requests/{request_id}", user=user
        )
        body = json.loads(response.read().decode("utf-8"))
        conn.close()
        status = body.get("status")

        if status == wanted:
            return status

        time.sleep(0.05)

    raise AssertionError(f"请求状态没有变成 {wanted}，当前 {status}")


def run_query(db_path: Path, sql: str, parameters=()):
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, parameters).fetchall()]


def scenario_explicit_cancel(port: int, request_id: str, pid_dir: Path, user: str) -> dict:
    conn, response = request(
        port,
        "POST",
        "/chat",
        {"request_id": request_id, "message": "跑探针", "stream": True},
        user=user,
    )

    wait_event(response, "tool_started")
    pid_path, pids = wait_pids(pid_dir)

    cancel_conn, cancel_response = request(
        port, "POST", f"/chat/requests/{request_id}/cancel", user=user
    )
    cancel_status = cancel_response.status
    cancel_response.read()
    cancel_conn.close()

    wait_event(response, "cancelled")

    while response.readline():
        pass

    conn.close()

    status = poll_request_status(port, request_id, "unknown", user)

    return {
        "request_id": request_id,
        "path": "explicit-cancel",
        "cancel_http_status": cancel_status,
        "request_status_after_cancel": status,
        "pid_file": pid_path.name,
        "pids": pids,
    }


def scenario_disconnect(port: int, request_id: str, pid_dir: Path, user: str) -> dict:
    before = {path.name for path in pid_dir.glob("pids.json.*.json")}

    conn, response = request(
        port,
        "POST",
        "/chat",
        {"request_id": request_id, "message": "跑探针", "stream": True},
        user=user,
    )

    wait_event(response, "tool_started")

    deadline = time.monotonic() + 30
    pid_path = None
    pids = None

    while time.monotonic() < deadline:
        for path in pid_dir.glob("pids.json.*.json"):
            if path.name in before:
                continue
            try:
                pids = json.loads(path.read_text(encoding="utf-8"))
                pid_path = path
                break
            except (OSError, json.JSONDecodeError):
                continue
        if pid_path is not None:
            break
        time.sleep(0.02)

    if pid_path is None:
        raise AssertionError("断流场景没有拿到探针 PID")

    # 直接断开连接：取消必须由 watch_disconnect 传播，而不是靠 HTTP 关闭。
    response.close()
    conn.close()

    status = poll_request_status(port, request_id, "unknown", user)

    return {
        "request_id": request_id,
        "path": "client-disconnect",
        "request_status_after_disconnect": status,
        "pid_file": pid_path.name,
        "pids": pids,
    }


def collect(run_request_id: str, db_path: Path, pids: dict, decoy_pid: int) -> dict:
    runs = run_query(
        db_path,
        "SELECT id, status FROM agent_runs WHERE request_id = ?",
        (run_request_id,),
    )
    run_id = runs[0]["id"] if runs else None

    execs = run_query(
        db_path,
        """
        SELECT e.id, e.status, e.error_code, e.sandbox_id, e.started_at, e.finished_at
        FROM agent_execs e
        JOIN agent_steps s ON s.id = e.step_id
        JOIN agent_turns t ON t.id = s.turn_id
        WHERE t.run_id = ?
        """,
        (run_id,),
    )

    events = run_query(
        db_path,
        """
        SELECT sequence, event_type, status, run_id, step_id, exec_id, sandbox_id
        FROM agent_trajectory_events WHERE run_id = ? ORDER BY sequence
        """,
        (run_id,),
    )

    from src.platform.process_tree import process_is_alive

    return {
        "run_id": run_id,
        "run_status": runs[0]["status"] if runs else None,
        "execs": execs,
        "events": events,
        "parent_alive": process_is_alive(pids["parent"]),
        "child_alive": process_is_alive(pids["child"]),
        "unrelated_process_alive": process_is_alive(decoy_pid),
    }


def main() -> int:
    port = free_port()
    vent = os.environ.copy()

    with tempfile.TemporaryDirectory(prefix="personal-agent-d11-live-") as raw:
        temp = Path(raw)
        data_dir = temp / "data"
        data_dir.mkdir()
        pid_dir = temp / "pids"
        pid_dir.mkdir()
        pid_file = pid_dir / "pids.json"
        server_path = temp / "d11_server.py"
        server_path.write_text(SERVER_CODE, encoding="utf-8")
        probe_path = temp / "d11_probe.py"
        probe_path.write_text(PROBE_SOURCE, encoding="utf-8")

        log_path = ROOT / "docs" / "evidence" / "d11-live-server.log"

        env = vent
        env.update(
            {
                "PERSONAL_AGENT_DATA_DIR": str(data_dir),
                "PERSONAL_AGENT_TEST_MODE": "1",
                "PERSONAL_AGENT_SANDBOX_ROOT": str(temp / "sandboxes"),
                "OPENAI_API_KEY": "invalid-local-test-key",
                "OPENAI_BASE_URL": "https://api.deepseek.com",
                "MODEL": "deepseek-flash",
                "NTFY_SERVER": "http://127.0.0.1:1",
                "NTFY_TOPIC": "d11-live-topic",
                "D11_ROOT": str(ROOT),
                "D11_PORT": str(port),
                "D11_PID_FILE": str(pid_file),
                "D11_PROBE_TOOL": "tier1_probe_tree",
                "D11_PROBE_SOURCE": str(probe_path),
            }
        )

        # 无关进程：清理只能动平台拥有的进程，不能顺手杀掉别人。
        decoy = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        with log_path.open("w+", encoding="utf-8") as log:
            server = subprocess.Popen(
                [sys.executable, str(server_path)],
                cwd=str(ROOT),
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )

            try:
                for _ in range(200):
                    if server.poll() is not None:
                        raise RuntimeError("验收服务提前退出")
                    try:
                        conn, response = request(port, "GET", "/health")
                        response.read()
                        conn.close()
                        if response.status == 200:
                            break
                    except OSError:
                        time.sleep(0.05)
                else:
                    raise RuntimeError("验收服务启动超时")

                first = scenario_explicit_cancel(
                    port, "d11_live_cancel", pid_dir, user="d11-live-user-1"
                )
                second = scenario_disconnect(
                    port, "d11_live_disconnect", pid_dir, user="d11-live-user-2"
                )
                time.sleep(0.5)

                db_path = data_dir / "chat_requests.db"

                report = {
                    "status": "PASS",
                    "server_port": port,
                    "scenarios": [],
                }

                for item in (first, second):
                    detail = collect(
                        item["request_id"], db_path, item["pids"], decoy.pid
                    )
                    item.update(detail)
                    report["scenarios"].append(item)

                for item in report["scenarios"]:
                    assert item["run_status"] == "cancelled", item
                    assert len(item["execs"]) == 1, item
                    assert item["execs"][0]["status"] == "cancelled", item
                    assert item["execs"][0]["error_code"] == "EXEC_CANCELLED", item
                    assert item["execs"][0]["sandbox_id"], item

                    # 只看这条 exec 的事件：D14 之后 run 级事件也进轨迹，
                    # 但「取消请求必须先于终态」这条规则只针对 exec 自己。
                    exec_id = item["execs"][0]["id"]
                    sequence = [
                        (event["event_type"], event["status"])
                        for event in item["events"]
                        if event["exec_id"] == exec_id
                    ]
                    assert sequence == [
                        ("exec_started", "running"),
                        ("exec_cancel_requested", "cancel_requested"),
                        ("exec_finished", "cancelled"),
                    ], sequence

                    assert item["parent_alive"] is False, item
                    assert item["child_alive"] is False, item
                    assert item["unrelated_process_alive"] is True, item

                EVIDENCE.write_text(
                    json.dumps(report, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0

            finally:
                decoy.kill()
                decoy.wait(timeout=10)

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
