"""Qwen 运行目录监听 CLI：默认扫一次；--interval 显式循环。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.collab.qwen_watcher import scan_once  # noqa: E402
from src.collab.registry import ExternalTaskRegistry  # noqa: E402


DEFAULT_ROOT = ROOT / "data" / "qwen-runs"
DEFAULT_DB = ROOT / "data" / "qwen-registry.db"


def build_sender(enabled: bool):
    if not enabled:
        return None

    owner = os.environ.get("PA_QQ_OWNER")

    if not owner:
        raise SystemExit(json.dumps({"error": "missing_owner", "message": "PA_QQ_OWNER 未配置"}, ensure_ascii=False))

    from src.qq.onebot import OneBotClient

    client = OneBotClient(base_url=os.environ.get("PA_QQ_API", "http://127.0.0.1:3001"))

    return lambda text: client.send_private_msg(int(owner), text)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="qwen_watch")
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=None)
    parser.add_argument("--notify", action="store_true")

    args = parser.parse_args(argv)
    sender = build_sender(args.notify)
    registry = ExternalTaskRegistry(args.db)

    while True:
        report = scan_once(root=args.root, registry=registry, sender=sender)
        print(json.dumps(report, ensure_ascii=False))

        if args.interval is None:
            return 0 if not report["errors"] else 1

        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
