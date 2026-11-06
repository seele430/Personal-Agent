"""检查本机监听端口是否暴露到回环之外。

用法：
    python scripts/check_listeners.py            # 列出所有非回环监听
    python scripts/check_listeners.py --port 8000
    python scripts/check_listeners.py --port 8000 --warn-only

退出码：发现非回环监听且未加 --warn-only 时为 1。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.platform.netcheck import exposed, parse_netstat


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--warn-only", action="store_true")
    args = parser.parse_args()

    result = subprocess.run(
        ["netstat", "-ano"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )

    if result.returncode != 0:
        print(f"netstat 失败：{result.returncode}", file=sys.stderr)
        return 2

    listeners = parse_netstat(result.stdout)
    risky = exposed(listeners, args.port)

    payload = {
        "port": args.port,
        "listeners": [item.__dict__ for item in listeners if args.port is None or item.port == args.port],
        "exposed": [item.__dict__ for item in risky],
    }

    print(json.dumps(payload, ensure_ascii=False, indent=2))

    if not risky:
        return 0

    print(
        f"警告：{len(risky)} 个监听绑定在非回环地址" + ("（warn-only）" if args.warn_only else ""),
        file=sys.stderr,
    )

    return 0 if args.warn_only else 1


if __name__ == "__main__":
    raise SystemExit(main())
