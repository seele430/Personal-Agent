"""D23 G2 全链路验收：一句话 → 3 个工具 → 平台沙箱 → 轨迹可见。

真实 HTTP/SSE、真实 uvicorn 进程、真实 tier1 沙箱子进程；模型是本地脚本替身
（确定性、不消耗额度），用 ``PERSONAL_AGENT_MAX_TOOL_CALLS_PER_TURN=3`` 打开
一次响应多个工具。

验收点：

1. 一条用户消息触发 3 次 ``tool_started`` / ``tool_finished``；
2. 数据库里 3 条 exec 全部 ``succeeded``，分别关联 3 个 tool step；
3. trajectory 里有 3 组 ``exec_started`` / ``exec_finished``；
4. ``scripts/trace_run.py`` 能直接把这整条链路打出来（CLI 面板）。

用法：

    .venv/Scripts/python.exe scripts/verify_g2_chain.py
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

EVIDENCE = ROOT / "docs" / "evidence" / "d23-g2-chain.json"

SERVER_CODE = """
import os, sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(os.environ["G2_ROOT"])
sys.path.insert(0, str(ROOT))

from fastapi import Request
from src import agent
from src.auth_api import get_current_user
from src.main import app


def current_user(request: Request):
    return {
        "id": request.headers.get("X-G2-User", "g2-user"),
        "role": "member",
        "username": "g2",
    }


app.dependency_overrides[get_current_user] = current_user


def host_handler(*args, **kwargs):
    raise AssertionError("宿主进程不应直接执行工具 handler")


agent.TOOL_REGISTRY.register(
    {
        "type": "function",
        "function": {
            "name": "g2_echo",
            "description": "G2 演示命令工具",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    policy="allow",
    handler=host_handler,
    execution={"kind": "command", "argv": [sys.executable, "-c", "print('g2-sandbox-ok')"]},
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


def delta(index, call_id, name, arguments="{}"):
    return SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def fake_create(**kwargs):
    messages = kwargs.get("messages") or []
    last = messages[-1] if messages else {}

    if last.get("role") == "user":
        CALLS["count"] += 1
        return iter(
            [
                chunk(
                    tool_calls=[
                        delta(0, "g2-c1", "list_dir"),
                        delta(1, "g2-c2", "g2_echo"),
                        delta(2, "g2-c3", "list_dir", '{"path": "."}'),
                    ],
                    finish="tool_calls",
                )
            ]
        )

    return iter([chunk(text="三个工具都跑完了", finish="stop")])


agent.client.chat.completions.create = fake_create

import uvicorn

uvicorn.run(
    app,
    host="127.0.0.1",
    port=int(os.environ["G2_PORT"]),
    log_level="warning",
)
"""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_health(port: int, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 60

    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("G2 服务提前退出")

        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=2
            ) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.1)

    raise RuntimeError("G2 服务启动超时")


def run_chat(port: int, request_id: str) -> tuple[int, list[str]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    body = json.dumps(
        {"request_id": request_id, "message": "把这几个工具都跑一遍", "stream": True}
    )
    conn.request(
        "POST",
        "/chat",
        body=body,
        headers={
            "Content-Type": "application/json",
            "X-G2-User": "g2-user",
        },
    )
    response = conn.getresponse()
    events: list[str] = []

    while True:
        line = response.readline()

        if not line:
            break

        text = line.decode("utf-8").strip()

        if text.startswith("event: "):
            events.append(text[len("event: "):])

        if text == "event: done":
            break

    conn.close()
    return response.status, events


def main() -> int:
    port = free_port()

    with tempfile.TemporaryDirectory(prefix="personal-agent-g2-") as raw:
        temp = Path(raw)
        data_dir = temp / "data"
        data_dir.mkdir()
        server_path = temp / "g2_server.py"
        server_path.write_text(SERVER_CODE, encoding="utf-8")
        log_path = ROOT / "docs" / "evidence" / "d23-g2-server.log"

        env = os.environ.copy()
        env.update(
            {
                "PERSONAL_AGENT_DATA_DIR": str(data_dir),
                "PERSONAL_AGENT_SANDBOX_ROOT": str(temp / "sandboxes"),
                "PERSONAL_AGENT_TEST_MODE": "1",
                "PERSONAL_AGENT_PLATFORM_REAPER": "0",
                "PERSONAL_AGENT_MAX_TOOL_CALLS_PER_TURN": "3",
                "OPENAI_API_KEY": "invalid-local-test-key",
                "OPENAI_BASE_URL": "https://api.deepseek.com",
                "MODEL": "deepseek-flash",
                "NTFY_SERVER": "http://127.0.0.1:1",
                "NTFY_TOPIC": "d23-g2-topic",
                "G2_ROOT": str(ROOT),
                "G2_PORT": str(port),
                "PYTHONPATH": str(ROOT),
                "PYTHONIOENCODING": "utf-8",
            }
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
                wait_health(port, server)
                status, events = run_chat(port, "d23_g2_chain")

                sys.path.insert(0, str(ROOT))
                from src.core.run_store import RunStore

                db_path = data_dir / "chat_requests.db"
                store = RunStore(db_path)
                run = store.find_run_by_request("d23_g2_chain", owner_id="g2-user")

                if run is None:
                    raise AssertionError("没有找到 run 记录")

                detail = store.get_run(run["id"], owner_id="g2-user")
                steps = detail["turns"][0]["steps"]
                tool_steps = [step for step in steps if step["kind"] == "tool"]
                execs = [
                    step["exec"] for step in tool_steps if step["exec"]
                ]
                events_rows = store.list_events(run["id"])
                exec_events = [
                    event for event in events_rows if event["exec_id"]
                ]

                trace = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "scripts" / "trace_run.py"),
                        "--db",
                        str(db_path),
                        run["id"],
                    ],
                    cwd=str(ROOT),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                )

                # stdout 理论上一定是 str；但有一次真实运行里拿到了 None
                # （demo 窗口 + 录屏占资源时偶发），演示脚本不该因此崩。
                trace_stdout = trace.stdout or ""
                trace_lines = len(trace_stdout.splitlines())

                report = {
                    "status": "PASS",
                    "http_status": status,
                    "tool_started_events": events.count("tool_started"),
                    "tool_finished_events": events.count("tool_finished"),
                    "run_id": run["id"],
                    "run_status": detail["status"],
                    "tool_steps": len(tool_steps),
                    "execs": [
                        {
                            "tool": item["tool_name"],
                            "status": item["status"],
                            "sandbox_id": item["sandbox_id"],
                        }
                        for item in execs
                    ],
                    "trajectory_exec_events": [
                        event["event_type"] for event in exec_events
                    ],
                    # D24：同一条 run 的 trace-id 应该一致，span 每次不同。
                    "traceparents": [
                        event["payload"]["traceparent"]
                        for event in exec_events
                        if event["event_type"] == "exec_started"
                    ],
                    "trace_exit_code": trace.returncode,
                    "trace_lines": trace_lines,
                }

                assert report["http_status"] == 200, report
                assert report["tool_started_events"] == 3, report
                assert report["tool_finished_events"] == 3, report
                assert report["tool_steps"] == 3, report
                assert len(report["execs"]) == 3, report
                assert all(item["status"] == "succeeded" for item in report["execs"]), report
                assert report["trajectory_exec_events"].count("exec_started") == 3, report
                assert report["trajectory_exec_events"].count("exec_finished") == 3, report
                assert report["trace_exit_code"] == 0, report

                trace_ids = {
                    item.split("-")[1] for item in report["traceparents"]
                }
                span_ids = {
                    item.split("-")[2] for item in report["traceparents"]
                }
                assert len(report["traceparents"]) == 3, report
                assert len(trace_ids) == 1, report
                assert len(span_ids) == 3, report

                EVIDENCE.write_text(
                    json.dumps(report, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(json.dumps(report, ensure_ascii=False, indent=2))
                print()
                print(trace_stdout)
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
