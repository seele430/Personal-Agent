"""Qwen Runner CLI：run / status / list。"""

from __future__ import annotations

import argparse
import json
import sys
import uuid

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.collab.qwen_runner import QwenRunner, RunSpec, RunSpecError, status_of  # noqa: E402
from src.collab.qwen_pipeline import PipelineError, finalize_run  # noqa: E402
from src.collab.registry import ExternalTaskRegistry  # noqa: E402


DEFAULT_ROOT = ROOT / "data" / "qwen-runs"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="qwen_task")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run")
    run.add_argument("--prompt", help="不安全：会进入进程 argv；建议用 stdin 或 --prompt-file")
    run.add_argument("--prompt-file")
    run.add_argument("--model", default="qwen3.8-flash")
    run.add_argument("--approval-mode", default="plan", choices=("plan", "yolo"))
    run.add_argument("--expect", action="append", default=[])
    run.add_argument("--allow-empty", action="store_true")
    run.add_argument("--max-wall-time", default="60s")
    run.add_argument("--timeout", type=float, default=90.0)
    run.add_argument("--transport", default="stdin", choices=("stdin", "arg"))
    run.add_argument("--no-safe-mode", action="store_true")
    run.add_argument("--bare", action="store_true")
    run.add_argument("--allow-no-artifacts", action="store_true")
    run.add_argument("--root", default=str(DEFAULT_ROOT))

    status = sub.add_parser("status")
    status.add_argument("run_id")
    status.add_argument("--root", default=str(DEFAULT_ROOT))

    finalize = sub.add_parser("finalize")
    finalize.add_argument("run_id")
    finalize.add_argument("--expect", action="append", default=[])
    finalize.add_argument("--db", default=None)
    finalize.add_argument("--notify", action="store_true")
    finalize.add_argument("--root", default=str(DEFAULT_ROOT))

    listing = sub.add_parser("list")
    listing.add_argument("--root", default=str(DEFAULT_ROOT))

    args = parser.parse_args(argv)

    if args.command == "run":
        if args.prompt_file:
            prompt = Path(args.prompt_file).read_text(encoding="utf-8")
        elif args.prompt:
            prompt = args.prompt
        else:
            prompt = sys.stdin.read()

        try:
            spec = RunSpec(
            run_id=str(uuid.uuid4()),
            provider_session_id=str(uuid.uuid4()),
            prompt=prompt,
            model=args.model,
            approval_mode=args.approval_mode,
            expected_artifacts={path: args.allow_empty for path in args.expect},
            max_wall_time=args.max_wall_time,
            timeout_seconds=args.timeout,
            prompt_transport=args.transport,
            safe_mode=not args.no_safe_mode,
            bare=args.bare,
            allow_no_artifacts=args.allow_no_artifacts,
            )
            state = QwenRunner(root=args.root).execute(spec)
        except RunSpecError as error:
            print(json.dumps({"error": error.code, "message": str(error)}, ensure_ascii=False))

            return 2

        print(json.dumps(state, ensure_ascii=False, indent=2))

        return 0 if state.get("state") == "completed_unverified" else 1

    if args.command == "finalize":
        sender = None

        if args.notify:
            import os

            from src.qq.onebot import OneBotClient

            owner = os.environ.get("PA_QQ_OWNER")

            if not owner:
                print(json.dumps({"error": "missing_owner", "message": "PA_QQ_OWNER 未配置"}, ensure_ascii=False))

                return 2

            client = OneBotClient(base_url=os.environ.get("PA_QQ_API", "http://127.0.0.1:3001"))
            sender = lambda text: client.send_private_msg(int(owner), text)  # noqa: E731

        try:
            report = finalize_run(
                root=args.root,
                run_id=args.run_id,
                registry=ExternalTaskRegistry(args.db),
                expected=tuple(args.expect),
                sender=sender,
            )
        except (PipelineError, RunSpecError) as error:
            print(json.dumps({"error": getattr(error, "code", "error"), "message": str(error)}, ensure_ascii=False))

            return 2

        print(json.dumps(report, ensure_ascii=False, indent=2))

        return 0 if report["state"] == "succeeded" else 1

    if args.command == "status":
        try:
            state = status_of(args.root, args.run_id)
        except RunSpecError as error:
            print(json.dumps({"error": error.code}, ensure_ascii=False))

            return 2

        if state is None:
            print(json.dumps({"error": "not_found", "run_id": args.run_id}))
            return 2

        print(json.dumps(state, ensure_ascii=False, indent=2))

        return 0

    root = Path(args.root)
    items = []
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "billed_known": True}

    if root.exists():
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue

            try:
                state = status_of(root, entry.name)
            except RunSpecError:
                continue

            state = state or {}
            usage = state.get("usage") or {}
            items.append({
                "run_id": entry.name,
                "provider_session_id": state.get("provider_session_id"),
                "state": state.get("state"),
                "reason": state.get("reason"),
                "requested_model": state.get("requested_model"),
                "observed_model": state.get("observed_model"),
                "manifest_path": state.get("manifest_path"),
                "usage": usage,
                "duration_ms": state.get("duration_ms"),
            })

            for key in ("input_tokens", "output_tokens", "total_tokens"):
                value = usage.get(key)

                if isinstance(value, int):
                    totals[key] += value
                else:
                    totals["billed_known"] = False

    print(json.dumps({"runs": items, "totals": totals}, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
