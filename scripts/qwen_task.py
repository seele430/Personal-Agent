"""Qwen Runner CLI：run / status / list。"""

from __future__ import annotations

import argparse
import json
import sys
import uuid

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.collab.qwen_runner import QwenRunner, RunSpec, status_of  # noqa: E402


DEFAULT_ROOT = ROOT / "data" / "qwen-runs"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="qwen_task")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run")
    run.add_argument("--prompt", required=True)
    run.add_argument("--model", default="qwen3.8-flash")
    run.add_argument("--approval-mode", default="plan", choices=("plan", "yolo"))
    run.add_argument("--expect", action="append", default=[])
    run.add_argument("--allow-empty", action="store_true")
    run.add_argument("--max-wall-time", default="60s")
    run.add_argument("--timeout", type=float, default=90.0)
    run.add_argument("--transport", default="arg", choices=("stdin", "arg"))
    run.add_argument("--no-safe-mode", action="store_true")
    run.add_argument("--bare", action="store_true")
    run.add_argument("--root", default=str(DEFAULT_ROOT))

    status = sub.add_parser("status")
    status.add_argument("run_id")
    status.add_argument("--root", default=str(DEFAULT_ROOT))

    listing = sub.add_parser("list")
    listing.add_argument("--root", default=str(DEFAULT_ROOT))

    args = parser.parse_args(argv)

    if args.command == "run":
        spec = RunSpec(
            run_id=str(uuid.uuid4()),
            provider_session_id=str(uuid.uuid4()),
            prompt=args.prompt,
            model=args.model,
            approval_mode=args.approval_mode,
            expected_artifacts={path: args.allow_empty for path in args.expect},
            max_wall_time=args.max_wall_time,
            timeout_seconds=args.timeout,
            prompt_transport=args.transport,
            safe_mode=not args.no_safe_mode,
            bare=args.bare,
        )
        state = QwenRunner(root=args.root).execute(spec)
        print(json.dumps(state, ensure_ascii=False, indent=2))

        return 0 if state.get("state") == "completed_unverified" else 1

    if args.command == "status":
        state = status_of(args.root, args.run_id)

        if state is None:
            print(json.dumps({"error": "not_found", "run_id": args.run_id}))
            return 2

        print(json.dumps(state, ensure_ascii=False, indent=2))

        return 0

    root = Path(args.root)
    items = []

    if root.exists():
        for entry in sorted(root.iterdir()):
            if entry.is_dir():
                state = status_of(root, entry.name)
                items.append({
                    "run_id": entry.name,
                    "state": (state or {}).get("state"),
                    "requested_model": (state or {}).get("requested_model"),
                })

    print(json.dumps(items, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
