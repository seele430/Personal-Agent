"""M1 watcher CLI：处理一次 Kimi 结果，或按间隔轮询。"""

from __future__ import annotations

import argparse
import json
import sys
import time

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.collab.review_bus import DEFAULT_ROOT, process_once  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="review_watcher")
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args(argv)

    root = Path(args.root)

    if args.once:
        result = process_once(root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1

    try:
        while True:
            result = process_once(root)
            print(json.dumps(result, ensure_ascii=False))
            time.sleep(max(0.5, args.interval))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
