"""Sleep-time Compute Phase 1 CLI：只在显式临时副本上工作。"""

from __future__ import annotations

import argparse
import json
import sys

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import config  # noqa: E402
from src.memory.sleep import (  # noqa: E402
    SleepError, ab_retrieval, apply, propose, replica_freeze, restore, validate,
)
from src.memory.store import MemoryStore  # noqa: E402
from src.memory.versions import MemoryVersions  # noqa: E402


def _guard(path: str) -> Path:
    replica = Path(path).expanduser().resolve()
    production = Path(config.paths.knowledge_db).expanduser().resolve()

    if replica == production:
        raise SleepError("拒绝直接操作生产 knowledge.db；请显式指定临时副本")

    if not replica.is_file():
        raise SleepError("副本不存在")

    return replica


def _store(path: str) -> MemoryStore:
    return MemoryStore(_guard(path))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="memory_sleep")
    sub = parser.add_subparsers(dest="command", required=True)

    freeze = sub.add_parser("replica-freeze")
    freeze.add_argument("--source-db", required=True)
    freeze.add_argument("--replica-db", required=True)

    plan = sub.add_parser("plan")
    plan.add_argument("--replica-db", required=True)
    plan.add_argument("--owner", required=True)
    plan.add_argument("--as-of", required=True)
    plan.add_argument("--out", required=True)

    check = sub.add_parser("validate")
    check.add_argument("--replica-db", required=True)
    check.add_argument("--candidate", required=True)

    publish = sub.add_parser("publish")
    publish.add_argument("--replica-db", required=True)
    publish.add_argument("--candidate", required=True)

    rollback = sub.add_parser("rollback")
    rollback.add_argument("--replica-db", required=True)
    rollback.add_argument("--snapshot-id", type=int, required=True)

    ab = sub.add_parser("ab")
    ab.add_argument("--replica-db", required=True)
    ab.add_argument("--owner", required=True)
    ab.add_argument("--queries", required=True)
    ab.add_argument("--candidate")

    args = parser.parse_args(argv)

    try:
        if args.command == "replica-freeze":
            print(json.dumps(replica_freeze(args.source_db, args.replica_db), ensure_ascii=False, indent=2))
            return 0

        store = _store(args.replica_db)

        if args.command == "plan":
            snapshot = MemoryVersions(store).create("sleep-m-base")
            candidate = propose(store, args.owner, as_of=args.as_of, snapshot=snapshot)
            Path(args.out).write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({
                "candidate_id": candidate["candidate_id"],
                "base_snapshot_id": snapshot.id,
                "base_sha256": snapshot.sha256,
                "ops": len(candidate["ops"]),
                "reports": candidate["reports"],
            }, ensure_ascii=False, indent=2))
            return 0

        if args.command == "rollback":
            print(json.dumps(restore(store, args.snapshot_id), ensure_ascii=False, indent=2))
            return 0

        candidate = json.loads(Path(args.candidate).read_text(encoding="utf-8"))
        snapshot_id = candidate["base_snapshot_id"]
        snapshot, _ = MemoryVersions(store).get(snapshot_id)

        if args.command == "validate":
            print(json.dumps(validate(store, candidate, snapshot), ensure_ascii=False, indent=2))
            return 0

        if args.command == "publish":
            print(json.dumps(apply(store, candidate, snapshot), ensure_ascii=False, indent=2))
            return 0

        if args.command == "ab":
            queries = json.loads(Path(args.queries).read_text(encoding="utf-8"))["queries"]
            before = ab_retrieval(store, args.owner, queries)

            if args.candidate:
                apply(store, candidate, snapshot)

            after = ab_retrieval(store, args.owner, queries)
            print(json.dumps({"before": before, "after": after}, ensure_ascii=False, indent=2))
            return 0

    except SleepError as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 2

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
