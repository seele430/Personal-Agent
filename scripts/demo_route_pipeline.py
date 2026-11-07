"""W1.4 垂直切片：一句话 → 意图路由 → 平台沙箱 → chart.png → 一条 trace。

用法：
    python scripts/demo_route_pipeline.py
    python scripts/demo_route_pipeline.py --message "分析这个 CSV 的缺失值并生成一张图"

输出：
    - 真实生成的 chart.png（路径打印在 ARTIFACT=）
    - 一条 run 的 trajectory：route → approval → node → exec → artifact
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


ANALYSIS_SCRIPT = r"""
import csv
import os
import sys

os.environ.setdefault("MPLCONFIGDIR", os.path.join(os.getcwd(), ".mplconfig"))
os.environ.setdefault("MPLBACKEND", "Agg")

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

rows = [
    {"city": "Beijing", "temp": "21", "humidity": "40"},
    {"city": "Shanghai", "temp": "", "humidity": "65"},
    {"city": "Shenzhen", "temp": "28", "humidity": ""},
    {"city": "Chengdu", "temp": "", "humidity": ""},
    {"city": "Harbin", "temp": "12", "humidity": "55"},
]

with open("weather.csv", "w", newline="", encoding="utf-8") as handle:
    writer = csv.DictWriter(handle, fieldnames=["city", "temp", "humidity"])
    writer.writeheader()
    writer.writerows(rows)

missing = {}
for column in ("temp", "humidity"):
    missing[column] = sum(1 for row in rows if row[column] == "")

figure, axis = plt.subplots(figsize=(6, 3.5))
axis.bar(list(missing), list(missing.values()), color="#2b6cb0")
axis.set_title("Missing values per column")
axis.set_ylabel("count")
axis.set_ylim(0, max(missing.values()) + 1)

output = os.path.abspath("chart.png")
figure.savefig(output, dpi=120, bbox_inches="tight")
print("rows=%d" % len(rows))
print("missing=%s" % json.dumps(missing))
print("ARTIFACT=%s" % output)
""".replace("json.dumps", "__import__('json').dumps")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--message",
        default="分析这个 CSV 的缺失值，并生成一张图",
    )
    parser.add_argument("--data-dir", default=None)
    args = parser.parse_args()

    data_dir = Path(
        args.data_dir or tempfile.mkdtemp(prefix="pa-route-pipeline-")
    )
    data_dir.mkdir(parents=True, exist_ok=True)

    os.environ["PERSONAL_AGENT_DATA_DIR"] = str(data_dir)
    os.environ["PERSONAL_AGENT_SANDBOX_ROOT"] = str(data_dir / "sandboxes")
    os.environ["PERSONAL_AGENT_PLATFORM_REAPER"] = "0"
    os.environ["PERSONAL_AGENT_SANDBOX_GLOBAL_LIMIT"] = "0"
    # 演示脚本直接打匿名 /v1；R6 之后需要显式声明。
    os.environ.setdefault("PERSONAL_AGENT_ALLOW_ANON_V1", "1")

    from fastapi.testclient import TestClient

    from src.core.run_store import RunStore
    from src.main import app
    from src.routing import Router
    from src.routing.events import record_route_decision
    from src.routing.policy import approval_gate

    store = RunStore(data_dir / "chat_requests.db")

    run = store.create_run(owner_id="route-demo", request_id="route-demo")
    run_id = run["id"]

    def event(name: str, **kwargs):
        return store.append_event(run_id, event_type=name, **kwargs)

    event("run_started", status="running")

    decision = Router().decide(args.message)

    record_route_decision(store, run_id, decision)

    gate = approval_gate(decision)

    event(
        "approval_checked",
        status="required" if gate.requires_approval else "allowed",
        payload={"risk": decision.risk, "reason": gate.reason},
    )

    if not gate.allowed:
        if decision.decision == "clarify":
            event("clarification_requested", status="pending")
            print(
                json.dumps(
                    {"decision": "clarify", "candidates": decision.candidates},
                    ensure_ascii=False,
                )
            )
            return 0

        event("approval_requested", status="pending")
        print(
            json.dumps(
                {"decision": "approval_required", "risk": decision.risk},
                ensure_ascii=False,
            )
        )
        return 0

    client = TestClient(app)
    client.__enter__()

    try:
        created = client.post(
            "/v1/sandboxes",
            json={
                "tier": "tier1",
                "workspace_id": "ws_route_demo",
                "ttl_seconds": 600,
            },
            headers={"Idempotency-Key": f"route-demo-{int(time.time())}"},
        )

        if created.status_code >= 300:
            raise SystemExit(f"create failed: {created.status_code} {created.text}")

        sandbox = created.json()
        sandbox_id = sandbox["sandbox_id"]

        event(
            "node_selected",
            status="created",
            sandbox_id=sandbox_id,
            payload={"node_id": sandbox["node_id"]},
        )

        python = sys.executable

        accepted = client.post(
            f"/v1/sandboxes/{sandbox_id}/exec",
            json={
                "command": [python, "-c", ANALYSIS_SCRIPT],
                "timeout_ms": 120_000,
            },
        )

        if accepted.status_code >= 300:
            raise SystemExit(f"exec failed: {accepted.status_code} {accepted.text}")

        exec_id = accepted.json()["exec_id"]

        event("exec_queued", status="queued", sandbox_id=sandbox_id, exec_id=exec_id)
        event("exec_started", status="running", sandbox_id=sandbox_id, exec_id=exec_id)

        deadline = time.monotonic() + 180
        body = {}

        while time.monotonic() < deadline:
            body = client.get(f"/v1/exec/{exec_id}").json()

            if body["state"] in {"succeeded", "failed", "cancelled", "timed_out"}:
                break

            time.sleep(0.1)

        artifact = None

        for line in (body.get("stdout") or "").splitlines():
            if line.startswith("ARTIFACT="):
                artifact = line.split("=", 1)[1].strip()

        event(
            "exec_succeeded" if body["state"] == "succeeded" else "exec_failed",
            status=body["state"],
            sandbox_id=sandbox_id,
            exec_id=exec_id,
            payload={"exit_code": body.get("exit_code")},
        )

        if artifact and Path(artifact).is_file():
            target = data_dir / "chart.png"
            target.write_bytes(Path(artifact).read_bytes())

            event(
                "artifact",
                status="ready",
                sandbox_id=sandbox_id,
                exec_id=exec_id,
                payload={
                    "kind": "png",
                    "path": str(target),
                    "bytes": target.stat().st_size,
                },
            )
        else:
            event("artifact_missing", status="failed", exec_id=exec_id)

        event("run_finished", status="succeeded" if artifact else "failed")

        summary = {
            "run_id": run_id,
            "message": args.message,
            "intent": decision.intent,
            "confidence": decision.confidence,
            "risk": decision.risk,
            "sandbox_id": sandbox_id,
            "node_id": sandbox["node_id"],
            "exec_id": exec_id,
            "exec_state": body["state"],
            "artifact": str(data_dir / "chart.png"),
            "events": [item["event_type"] for item in store.list_events(run_id)],
        }

        print("ARTIFACT=" + str(data_dir / "chart.png"))
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if artifact else 1

    finally:
        client.__exit__(None, None, None)


if __name__ == "__main__":
    raise SystemExit(main())
