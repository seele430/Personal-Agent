"""W7 故障矩阵：六类故障 + 10 次通知重放 + 对账 + 成本账本。

    .\\.venv\\Scripts\\python.exe scripts/run_fault_matrix.py

全部本地、不调模型（0 token）。证据落 `pa-w7-evidence-20261107/`。
"""

from __future__ import annotations

import json
import sys
import tempfile

from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.artifacts.registry import ArtifactRegistry  # noqa: E402
from src.cost.ledger import CostLedger, Price  # noqa: E402
from src.faults.matrix import fault_notification_fail, run_matrix  # noqa: E402
from src.faults.reconcile import reconcile_artifacts, reconcile_outbox  # noqa: E402


OUT_DIR = (
    Path(r"C:\Users\123\Documents\deepseek-harness\default-workspace")
    / "pa-w7-evidence-20261107"
)

REPLAYS = 10


def replay_notifications(times: int = REPLAYS) -> dict:
    """把通知故障重放 N 次：每次都必须"只送达一次"。"""

    deliveries = []

    for index in range(times):
        outcome = fault_notification_fail()
        evidence = outcome.get("evidence", {})
        deliveries.append(
            {
                "round": index + 1,
                "recovered": outcome.get("recovered"),
                "delivered_times": evidence.get("delivered_times"),
                "final_status": evidence.get("final_status"),
            }
        )

    duplicates = [item for item in deliveries if item["delivered_times"] != 1]
    missing = [item for item in deliveries if item["final_status"] != "sent"]

    return {
        "replays": times,
        "duplicates": len(duplicates),
        "not_delivered": len(missing),
        "passed": not duplicates and not missing,
        "details": deliveries,
    }


def reconcile_demo() -> dict:
    """产物缺文件 + 孤儿文件 + Outbox 卡死，各造一个再对账。"""

    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder) / "artifacts"
        root.mkdir()

        registry = ArtifactRegistry(Path(folder) / "a.db", root=root)

        (root / "kept.txt").write_bytes(b"k")
        (root / "gone.txt").write_bytes(b"g")
        kept = registry.register("u1", "kept.txt")
        gone = registry.register("u1", "gone.txt")
        (root / "gone.txt").unlink()
        (root / "stray.bin").write_bytes(b"z")

        report = reconcile_artifacts(registry)

        assert kept.id, "kept 应存在"
        assert gone.id in report.missing, "缺失文件应被报告"
        assert "stray.bin" in report.orphan_files, "孤儿文件应被报告"
        assert (root / "stray.bin").exists(), "孤儿文件不能被删除"

        outbox = reconcile_outbox(Path(folder) / "no-such.db")

        return {
            "artifacts": report.to_dict(),
            "outbox_noop": outbox.to_dict(),
            "passed": True,
        }


def build_cost_ledger() -> dict:
    """从 W6 验收证据里建账：失败运行与重跑都算钱。"""

    ledger = CostLedger(price=None)  # 单价未知：只报 token

    evidence_dir = (
        Path(r"C:\Users\123\Documents\deepseek-harness\default-workspace")
        / "pa-w6-evidence-20261107"
    )

    report_summary = sorted(evidence_dir.glob("report-summary-*.json"))

    if report_summary:
        payload = json.loads(report_summary[-1].read_text(encoding="utf-8"))

        for run in payload.get("runs", []):
            ledger.record(
                f"report-{run.get('index')}",
                kind="report",
                tokens_in=int(run.get("tokens", 0)),
                accepted=run.get("scorecard", {}).get("passed", False),
                note=str(run.get("state", "")),
            )

    snake_summary = sorted(evidence_dir.glob("snake-summary-*.json"))

    if snake_summary:
        payload = json.loads(snake_summary[-1].read_text(encoding="utf-8"))

        for run in payload.get("runs", []):
            ledger.record(
                f"snake-{run.get('index')}",
                kind="snake",
                tokens_in=0,
                accepted=bool(run.get("scorecard", {}).get("passed")),
            )

    # 改进后重跑（来源 9 条，未达标 → 失败成本也要计入）
    ledger.record(
        "report-improved",
        kind="report",
        tokens_in=21965,
        accepted=False,
        note="来源 9 < 10，partial",
    )

    summary = ledger.summary()
    summary["priced_example"] = CostLedger(price=Price(2.0, 8.0))
    summary.pop("priced_example", None)

    return summary


def main(argv=None) -> int:
    print("运行六类故障矩阵…")
    matrix = run_matrix()

    print(f"六类：{sum(1 for item in matrix if item['recovered'])}/6 可恢复")

    print(f"重放通知故障 {REPLAYS} 次…")
    replay = replay_notifications()

    print(f"重复通知：{replay['duplicates']}，未送达：{replay['not_delivered']}")

    reconcile = reconcile_demo()
    cost = build_cost_ledger()

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "faults": matrix,
        "faults_recovered": sum(1 for item in matrix if item["recovered"]),
        "notification_replay": replay,
        "reconcile": reconcile,
        "cost": cost,
        "tokens_spent": 0,
    }

    out_dir = OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"fault-matrix-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"成本：runs={cost['runs']} accepted={cost['accepted_runs']} "
        f"tokens={cost['total_tokens']} "
        f"tokens_per_accepted={cost['tokens_per_accepted_artifact']}"
    )
    print(f"证据：{path}")

    ok = (
        payload["faults_recovered"] == 6
        and replay["passed"]
        and reconcile["passed"]
        and cost["accepted_runs"] > 0
    )

    print("PASS" if ok else "FAIL")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
