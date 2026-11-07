"""打印最近活动时间线（只读）。

    .\\venv/Scripts/python.exe scripts/activity_timeline.py --data-dir <数据目录> --limit 20
    .\\venv/Scripts/python.exe scripts/activity_timeline.py --json
"""

from __future__ import annotations

import argparse
import json
import sys

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.activity.timeline import TimelineError, build_timeline  # noqa: E402


def data_dir_from(arg: str | None) -> Path:
    if arg:
        return Path(arg)

    from src.config import paths

    return paths.platform_db.parent


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="最近活动时间线")
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    base = data_dir_from(args.data_dir)

    try:
        events = build_timeline(
            traces_db=base / "traces.db",
            platform_db=base / "platform.db",
            artifacts_db=base / "artifacts.db",
            limit=args.limit,
        )

    except TimelineError as error:
        print(f"参数错误：{error}")

        return 2

    payload = [event.to_dict() for event in events]

    if args.json:
        print(json.dumps({"events": payload, "count": len(payload)}, ensure_ascii=False, indent=2))

        return 0

    if not events:
        print("暂无活动记录")

        return 0

    for event in events:
        stamp = event.ts[:19].replace("T", " ")
        print(f"{stamp}  {event.source:<8} {event.status:<10} {event.title}")

    print(f"\n共 {len(events)} 条（上限 {args.limit}）")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
