"""M2 watcher CLI：处理结果、确认发送草稿、超时/人工停止。

用法：

- ``--once``            处理一次（对账 → 处理结果 → 超时检查）
- ``--interval 5``      循环轮询（默认 5 秒）
- ``--confirm-send N``  人工确认后发送 round-N 草稿（唯一的发送入口）
- ``--stop``            人工停止当前循环
- ``--timeout 600``     等待结果超时秒数（自 request mtime 起算）
- ``--max-rounds 3``    同一复审对象的轮次上限

QQ 通知只在 ``PA_QQ_OWNER`` + OneBot 已配置时启用；未配置时静默跳过。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.collab import review_bus  # noqa: E402


def build_notifier():
    owner = int(os.getenv("PA_QQ_OWNER") or 0)

    if not owner:
        return None

    try:
        from src.qq.api import build_client
    except Exception:  # noqa: BLE001 - 通知是可选能力，导入失败就跳过
        return None

    client = build_client()

    if not client.configured():
        return None

    def notify(text: str) -> None:
        client.send_private_msg(owner, text)

    return notify


def run_once(root: Path, args, notifier) -> dict:
    if args.confirm_send is not None:
        return review_bus.confirm_send(root, args.confirm_send, notifier=notifier)

    if args.stop:
        return review_bus.stop(root, "manual", notifier=notifier)

    review_bus.reconcile(root, notifier=notifier)
    result = review_bus.process_once(
        root, notifier=notifier, max_rounds=args.max_rounds
    )
    timeout = review_bus.check_timeout(
        root, timeout=args.timeout, notifier=notifier
    )

    if timeout.get("action") == "stopped":
        return timeout

    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="review_watcher")
    parser.add_argument("--root", default=str(review_bus.DEFAULT_ROOT))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--confirm-send", type=int, default=None, metavar="N")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--timeout", type=float, default=review_bus.DEFAULT_TIMEOUT)
    parser.add_argument("--max-rounds", type=int, default=review_bus.MAX_ROUNDS)
    args = parser.parse_args(argv)

    root = Path(args.root)
    notifier = build_notifier()

    if args.once or args.confirm_send is not None or args.stop:
        result = run_once(root, args, notifier)
        print(json.dumps(result, ensure_ascii=False, indent=2))

        return 0 if result.get("ok") else 1

    try:
        while True:
            result = run_once(root, args, notifier)
            print(json.dumps(result, ensure_ascii=False))
            time.sleep(max(0.5, args.interval))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
