"""跨 Agent 工件接力 CLI：export / run（薄封装）。"""

from __future__ import annotations

import argparse
import json
import os
import sys

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.collab.handoff import (  # noqa: E402
    HandoffError,
    export_handoff,
    run_handoff,
)
from src.collab.registry import ExternalTaskRegistry  # noqa: E402
from src.collab.relay import RelayRunner  # noqa: E402
from src.collab.run_store import RunStore  # noqa: E402
from src.collab.providers import build_providers  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="agent_handoff")
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export")
    export.add_argument("--run-id", required=True)
    export.add_argument("--instruction")
    export.add_argument("--instruction-file")
    export.add_argument("--qwen-root", default=str(ROOT / "data" / "qwen-runs"))
    export.add_argument("--registry-db", default=str(ROOT / "data" / "qwen-registry.db"))
    export.add_argument("--handoff-root", default=str(ROOT / "data" / "handoffs"))

    run = sub.add_parser("run")
    run.add_argument("--handoff-id", required=True)
    run.add_argument("--chain", default="ark,deepseek")
    run.add_argument("--handoff-root", default=str(ROOT / "data" / "handoffs"))
    run.add_argument("--db", default=os.getenv("PA_COLLAB_DB") or str(ROOT / "data" / "collab-runs.db"))
    run.add_argument("--artifacts", default=os.getenv("PA_COLLAB_ARTIFACTS") or str(ROOT / "data" / "collab-artifacts"))

    args = parser.parse_args(argv)

    try:
        if args.command == "export":
            if args.instruction_file:
                instruction = Path(args.instruction_file).read_text(encoding="utf-8")
            elif args.instruction:
                instruction = args.instruction
            else:
                instruction = sys.stdin.read()

            package = export_handoff(
                args.run_id,
                instruction,
                root=args.qwen_root,
                registry=ExternalTaskRegistry(args.registry_db),
                out_dir=args.handoff_root,
            )
            print(json.dumps(package, ensure_ascii=False, indent=2))

            return 0

        chain = [name.strip() for name in args.chain.split(",") if name.strip()]
        store = RunStore(args.db)
        runner = RelayRunner(
            store=store,
            providers=build_providers(chain),
            artifact_root=args.artifacts,
        )
        report = run_handoff(
            args.handoff_id,
            chain,
            handoff_root=args.handoff_root,
            store=store,
            runner=runner,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))

        return 0 if report["child_state"] == "succeeded" else 1
    except HandoffError as error:
        print(json.dumps({"error": error.code, "message": str(error)}, ensure_ascii=False))

        return 2


if __name__ == "__main__":
    raise SystemExit(main())
