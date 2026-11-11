"""实际操作弹性平台：创建沙箱 → 看调度分布 → 执行 → 取消 → 销毁。

用法：
    python scripts/try_platform.py
    python scripts/try_platform.py --base http://127.0.0.1:8000 --count 3
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid


def call(base: str, method: str, path: str, body: dict | None = None, headers: dict | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            **(headers or {}),
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--count", type=int, default=3)
    args = parser.parse_args()

    base = args.base.rstrip("/")
    created: list[str] = []

    print(f"== 1. 创建 {args.count} 个沙箱，观察调度 ==")
    for index in range(args.count):
        status, body = call(
            base,
            "POST",
            "/v1/sandboxes",
            {
                "tier": "tier1",
                "workspace_id": f"try-{index}",
                "ttl_seconds": 600,
            },
            headers={"Idempotency-Key": f"try-{uuid.uuid4().hex[:8]}"},
        )
        if status != 201:
            print("创建失败：", status, body)
            return 1
        created.append(body["sandbox_id"])
        print(f"  {body['sandbox_id']} -> node={body['node_id']}")

    print("\n== 2. 每个沙箱跑一条命令 ==")
    execs = []
    for sandbox_id in created:
        status, body = call(
            base,
            "POST",
            f"/v1/sandboxes/{sandbox_id}/exec",
            {
                "command": [sys.executable, "-c", "print('hello from sandbox')"],
                "timeout_ms": 60000,
            },
        )
        if status != 202:
            print("exec 失败：", status, body)
            return 1
        execs.append(body["exec_id"])
        print(f"  {sandbox_id} -> {body['exec_id']}")

    deadline = time.monotonic() + 120
    pending = set(execs)
    states: dict[str, str] = {}

    while pending and time.monotonic() < deadline:
        for exec_id in list(pending):
            status, body = call(base, "GET", f"/v1/exec/{exec_id}")
            if status == 200 and body.get("state") in {
                "succeeded", "failed", "cancelled", "timed_out",
            }:
                states[exec_id] = body["state"]
                pending.discard(exec_id)
        time.sleep(0.2)

    print("  终态：", json.dumps(states, ensure_ascii=False))

    print("\n== 3. 取消一个长任务 ==")
    status, body = call(
        base,
        "POST",
        f"/v1/sandboxes/{created[0]}/exec",
        {
            "command": [sys.executable, "-c", "import time; time.sleep(60)"],
            "timeout_ms": 120000,
        },
    )
    if status == 202:
        exec_id = body["exec_id"]
        time.sleep(1)
        call(base, "POST", f"/v1/exec/{exec_id}/cancel")
        for _ in range(100):
            status, body = call(base, "GET", f"/v1/exec/{exec_id}")
            if body.get("state") in {"cancelled", "failed", "timed_out"}:
                break
            time.sleep(0.1)
        print(f"  {exec_id} -> {body.get('state')} ({body.get('result_code')})")

    print("\n== 4. 销毁沙箱 ==")
    for sandbox_id in created:
        status, body = call(base, "POST", f"/v1/sandboxes/{sandbox_id}/destroy")
        print(f"  {sandbox_id} -> {body.get('state', status)}")

    print("\n完成。打开 http://127.0.0.1:5173 登录后点「平台」查看队列、节点与执行记录。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
