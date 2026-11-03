"""查看一次 run 的轨迹：run / turn / step / exec / 审批 / trajectory 事件。

用法：

    python scripts/trace_run.py <run_id>
    python scripts/trace_run.py --request-id <request_id> [--owner <user_id>]
    python scripts/trace_run.py --json <run_id>
    python scripts/trace_run.py --db <chat_requests.db> <run_id>

只读：不会修改数据库，也不会重放任何 exec。
"""

from __future__ import annotations

import argparse
import json
import sys

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.run_store import RunStore  # noqa: E402
from src.core.trajectory_view import collect_run, render  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trace_run",
        description="查看一次 run 的 step / exec / 审批 / 事件轨迹。",
    )
    parser.add_argument("run_id", nargs="?", help="run id（run_ 开头）")
    parser.add_argument(
        "--request-id", default=None, help="按聊天 request_id 查 run"
    )
    parser.add_argument(
        "--owner", default=None, help="配合 --request-id 校验归属"
    )
    parser.add_argument(
        "--json", action="store_true", help="输出 JSON（便于脚本处理）"
    )
    parser.add_argument(
        "--db", default=None, help="覆盖 chat_requests.db 路径（只读排查用）"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = RunStore(args.db) if args.db else RunStore()

    run_id = args.run_id

    if run_id is None and args.request_id:
        run = store.find_run_by_request(args.request_id, owner_id=args.owner)

        if run is None:
            print(f"找不到 request 对应的 run：{args.request_id}", file=sys.stderr)
            return 2

        run_id = run["id"]

    if run_id is None:
        print("需要 run_id 或 --request-id", file=sys.stderr)
        return 2

    try:
        data = collect_run(store, run_id, owner_id=args.owner)
    except KeyError:
        print(f"找不到 run：{run_id}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        print(render(data), end="")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
