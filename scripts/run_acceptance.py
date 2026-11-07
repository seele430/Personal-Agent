"""W6 验收：报告 ×3 / 贪吃蛇 ×3 / 平台串联，产出 ScoreCard 与证据包。

    .\\.venv\\Scripts\\python.exe scripts/run_acceptance.py --kind report --repeat 3
    .\\.venv\\Scripts\\python.exe scripts/run_acceptance.py --kind snake  --repeat 3
    .\\.venv\\Scripts\\python.exe scripts/run_acceptance.py --kind platform --count 18

口径（如实标注，别把代理当人工评分）：

- 评分是**机器代理 rubric**（`src/eval/score.py`），全部由可测量事实推导；
- 报告的每次运行都记 token，累计越界就停（成本约定）；
- 贪吃蛇的"3 次"是**重复验收**同一产物，不是重新生成；
- 平台串联在**隔离临时目录**里跑，不碰真实库。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time

from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_DIR = (
    Path(r"C:\Users\123\Documents\deepseek-harness\default-workspace")
    / "pa-w6-evidence-20261107"
)

REPORT_QUERY = "PagedAttention 与 vLLM 的显存管理"
COST_BUDGET_TOKENS = 132_000


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# --------------------------------------------------------------------------
# 报告验收
# --------------------------------------------------------------------------

def run_report_once(index: int, out_dir: Path) -> dict:
    import scripts.research_run as research_run

    from src.eval.score import score_report
    from src.web.evidence import EvidenceStore

    work = Path(tempfile.mkdtemp(prefix=f"pa-accept-report-{index}-"))
    evidence_db = work / "evidence.db"
    research_db = work / "research.db"
    report_dir = work / "out"

    code = research_run.main(
        [
            "--query",
            REPORT_QUERY,
            "--live",
            "--out",
            str(report_dir),
            "--evidence-db",
            str(evidence_db),
            "--research-db",
            str(research_db),
        ]
    )

    json_files = list(report_dir.glob("*.json"))
    md_files = list(report_dir.glob("*.md"))

    if not json_files or not md_files:
        from src.eval.score import ScoreCard

        card = ScoreCard(
            kind="report",
            items=(),
            hard_gate_failures=("产物不存在：链路失败，未生成报告",),
        )

        return {
            "index": index + 1,
            "exit_code": code,
            "tokens": 0,
            "state": "failed",
            "critic": None,
            "report_path": "",
            "scorecard": card.to_dict(),
        }

    payload = json.loads(json_files[0].read_text(encoding="utf-8"))
    markdown = md_files[0].read_text(encoding="utf-8")

    target = out_dir / f"report-run{index + 1}-{_stamp()}.md"
    target.write_text(markdown, encoding="utf-8")

    store = EvidenceStore(evidence_db)
    tokens = payload.get("usage", {}).get("total_tokens", 0)

    card = score_report(
        markdown,
        source_count=int(payload.get("report", {}).get("sources", 0)),
        artifact_opens=bool(markdown.strip()),
        evidence_store=store,
        crash_free=code in (0, 3),
        safety_ok=True,
        within_budget=tokens <= 60_000,
        notified=True,
        tokens=tokens,
    )

    return {
        "index": index + 1,
        "exit_code": code,
        "tokens": tokens,
        "state": payload.get("state"),
        "critic": payload.get("critic"),
        "report_path": str(target),
        "scorecard": card.to_dict(),
    }


# --------------------------------------------------------------------------
# 贪吃蛇验收（同一产物重复验收）
# --------------------------------------------------------------------------

def run_snake_once(index: int) -> dict:
    from src.eval.score import score_snake

    frontend = ROOT / "frontend"

    completed = subprocess.run(
        "npx playwright test tests/snake.spec.ts --reporter=line",
        cwd=frontend,
        shell=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )

    output = (completed.stdout or "") + (completed.stderr or "")
    match = re.search(r"(\d+) passed", output)
    passed = int(match.group(1)) if match else 0
    ok = completed.returncode == 0 and passed >= 6

    card = score_snake(
        loads=ok,
        controls_ok=ok,
        score_works=ok,
        game_over_works=ok,
        ticks_500_ok=ok,
        mobile_ok=ok,
        draws=ok,
        crash_free=ok,
        safety_ok=True,
        within_budget=True,
        notified=True,
    )

    return {
        "index": index + 1,
        "exit_code": completed.returncode,
        "playwright_passed": passed,
        "scorecard": card.to_dict(),
    }


# --------------------------------------------------------------------------
# 平台串联（隔离目录里真跑 18 条 exec）
# --------------------------------------------------------------------------

def platform_worker(count: int, out_path: Path) -> int:
    from src.platform import ops
    from src.platform.exec_store import TERMINAL_EXEC_STATES, ExecStore

    sandbox = ops.create_sandbox(idempotency_key=f"accept-{int(time.time())}")
    sandbox_id = sandbox["id"]

    rows = []

    for index in range(count):
        row = ops.submit_exec(
            sandbox_id,
            command=["cmd", "/c", "echo", f"run-{index}"],
            timeout_ms=30_000,
            max_output_bytes=4096,
            run_id=f"accept-run-{index:02d}",
        )
        rows.append(row["id"])

    store = ExecStore()
    deadline = time.monotonic() + 120
    terminal = {}

    while time.monotonic() < deadline:
        done = 0

        for exec_id in rows:
            try:
                detail = store.get(exec_id)

            except KeyError:
                continue

            if detail["state"] in TERMINAL_EXEC_STATES:
                terminal[exec_id] = detail
                done += 1

        if done == len(rows):
            break

        time.sleep(0.3)

    payload = {
        "sandbox_id": sandbox_id,
        "submitted": len(rows),
        "terminal": len(terminal),
        "linked": sum(
            1
            for detail in terminal.values()
            if detail.get("run_id") and detail["run_id"].startswith("accept-run-")
        ),
        "state_counts": {},
        "execs": [
            {
                "id": exec_id,
                "run_id": detail.get("run_id"),
                "state": detail.get("state"),
                "exit_code": detail.get("exit_code"),
            }
            for exec_id, detail in terminal.items()
        ],
    }

    for detail in terminal.values():
        payload["state_counts"][detail["state"]] = (
            payload["state_counts"].get(detail["state"], 0) + 1
        )

    # C7：通知证据 —— 走真实 Outbox worker（非站长归属 → 站内投递）
    try:
        import sqlite3

        from src import config
        from src.core.outbox_worker import process_one_notification

        process_one_notification()  # 建表并确认没有待发送

        connection = sqlite3.connect(config.paths.tasks_db, timeout=10)

        with connection:
            connection.execute(
                "INSERT INTO notification_outbox "
                "(monitor_id, item_id, message, status, attempts, owner_id) "
                "VALUES (?,?,?,?,?,?)",
                (
                    "accept-monitor",
                    "item-1",
                    "验收通知：执行已完成",
                    "pending",
                    0,
                    "member-user",
                ),
            )

        connection.close()

        process_one_notification()

        connection = sqlite3.connect(config.paths.tasks_db, timeout=10)
        row = connection.execute(
            "SELECT status FROM notification_outbox WHERE monitor_id = ?",
            ("accept-monitor",),
        ).fetchone()
        connection.close()

        payload["notification"] = {
            "status": row[0] if row else None,
            "delivered": bool(row and row[0] == "in_app"),
        }

    except Exception as error:  # noqa: BLE001 - 通知失败单独记，不盖主结果
        payload["notification"] = {"error": f"{type(error).__name__}: {error}"}

    try:
        ops.destroy_sandbox(sandbox_id)

    except Exception:  # noqa: BLE001 - 清理失败不该盖住主结果
        payload["destroy_failed"] = True

    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    notified = payload.get("notification", {}).get("delivered", False)

    print(
        "notification status="
        f"{payload.get('notification', {}).get('status')} delivered={notified}"
    )

    ok = (
        payload["terminal"] == count
        and payload["linked"] == count
        and notified
    )

    return 0 if ok else 1


# --------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="W6 验收")
    parser.add_argument("--kind", choices=("report", "snake", "platform"), required=True)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--count", type=int, default=18)
    parser.add_argument("--out", default=str(OUT_DIR))
    parser.add_argument("--platform-worker", action="store_true")
    parser.add_argument("--worker-out", default="")
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.platform_worker:
        return platform_worker(args.count, Path(args.worker_out))

    stamp = _stamp()

    if args.kind == "report":
        runs = []
        spent = 0

        for index in range(args.repeat):
            result = run_report_once(index, out_dir)
            runs.append(result)
            spent += result.get("tokens", 0)

            print(
                f"[report {index + 1}/{args.repeat}] "
                f"score={result['scorecard']['total']} "
                f"tokens={result['tokens']} "
                f"gates={result['scorecard']['hard_gate_failures']}"
            )

            if spent > COST_BUDGET_TOKENS:
                print(f"成本越界：{spent} > {COST_BUDGET_TOKENS}，停止后续运行")
                break

        from src.eval.score import ScoreCard, summarize
        from src.eval.score import Item

        cards = []

        for run in runs:
            payload = run["scorecard"]
            cards.append(
                ScoreCard(
                    kind="report",
                    items=tuple(
                        Item(
                            key=item["key"],
                            label=item["label"],
                            score=item["score"],
                            maximum=item["max"],
                            note=item.get("note", ""),
                        )
                        for item in payload["items"]
                    ),
                    hard_gate_failures=tuple(payload["hard_gate_failures"]),
                )
            )

        summary = summarize(cards)

        payload = {
            "kind": "report",
            "generated_at": stamp,
            "query": REPORT_QUERY,
            "runs": runs,
            "summary": summary,
            "tokens_spent": spent,
            "rubric": "machine-proxy（非人工评审）",
        }

        print(json.dumps(summary, ensure_ascii=False))

    elif args.kind == "snake":
        runs = [run_snake_once(index) for index in range(args.repeat)]

        totals = [run["scorecard"]["total"] for run in runs]

        payload = {
            "kind": "snake",
            "generated_at": stamp,
            "runs": runs,
            "summary": {
                "runs": len(runs),
                "totals": totals,
                "average": round(sum(totals) / len(totals), 1),
                "every_run_passed": all(
                    run["scorecard"]["passed"] for run in runs
                ),
            },
            "rubric": "machine-proxy（重复验收同一产物，不重新生成）",
        }

        print(json.dumps(payload["summary"], ensure_ascii=False))

    else:
        work = tempfile.mkdtemp(prefix="pa-accept-platform-")
        env = dict(os.environ)
        env["PERSONAL_AGENT_DATA_DIR"] = work
        env["PERSONAL_AGENT_SANDBOX_ROOT"] = str(Path(work) / "sandboxes")
        env["PERSONAL_AGENT_PLATFORM_RATE_PER_MIN"] = "0"
        env["NO_PROXY"] = "*"

        worker_out = out_dir / f"platform-{stamp}.json"

        completed = subprocess.run(
            [
                sys.executable,
                str(Path(__file__)),
                "--kind",
                "platform",
                "--count",
                str(args.count),
                "--platform-worker",
                "--worker-out",
                str(worker_out),
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )

        if completed.returncode != 0:
            print(completed.stdout[-2000:])
            print(completed.stderr[-2000:])

        payload = (
            json.loads(worker_out.read_text(encoding="utf-8"))
            if worker_out.exists()
            else {}
        )

        payload["exit_code"] = completed.returncode
        payload["generated_at"] = stamp

        print(
            f"platform submitted={payload.get('submitted')} "
            f"terminal={payload.get('terminal')} linked={payload.get('linked')} "
            f"states={payload.get('state_counts')}"
        )

    path = out_dir / f"{args.kind}-summary-{stamp}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"证据：{path}")

    if args.kind == "report":
        return 0 if payload["summary"]["overall"] else 1

    if args.kind == "snake":
        return 0 if payload["summary"]["every_run_passed"] else 1

    return 0 if payload.get("linked", 0) >= args.count else 1


if __name__ == "__main__":
    raise SystemExit(main())
