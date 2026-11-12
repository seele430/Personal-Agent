"""M2 watcher CLI：处理结果、确认发送草稿、超时/人工停止。

用法：

- ``--once``            处理一次（对账 → 处理结果 → 超时检查）
- ``--interval 5``      循环轮询（默认 5 秒）
- ``--confirm-send N``  人工确认后发送 round-N 草稿（唯一的发送入口）
- ``--start-loop TARGET`` 开新复审对象（pass/stopped 之后），loop_round 归零
- ``--stop``            人工停止当前循环
- ``--timeout 600``     等待结果超时秒数（自 request mtime 起算）
- ``--max-rounds 3``    同一复审对象的轮次上限

QQ 通知在 `.env` 里 `PA_QQ_OWNER` + `PA_ONEBOT_URL` 都配好时启用（复用
`src/core/qq_notify.send_qq_notification`）；NapCat 不在线时发送失败只记 stderr。
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
    """QQ 通知：读项目 .env，复用 src/core/qq_notify；未配置返回 None。

    NapCat（OneBot）不在线时发送会抛 OneBotError，由 review_bus 记录为
    notify_failed，不影响状态机。
    """

    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    owner = (os.getenv("PA_QQ_OWNER") or "").strip()

    if not owner.isdigit() or int(owner) <= 0:
        return None

    if not (os.getenv("PA_ONEBOT_URL") or "").strip():
        return None

    try:
        from src.core.qq_notify import send_qq_notification
    except Exception:  # noqa: BLE001 - 通知是可选能力，导入失败就跳过
        return None

    return send_qq_notification


def run_once(root: Path, args, notifier) -> dict:
    if getattr(args, "start_loop", None):
        return review_bus.start_loop(root, args.start_loop)

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
    parser.add_argument("--start-loop", default=None, metavar="TARGET")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--timeout", type=float, default=review_bus.DEFAULT_TIMEOUT)
    parser.add_argument("--max-rounds", type=int, default=review_bus.MAX_ROUNDS)
    args = parser.parse_args(argv)

    root = Path(args.root)
    notifier = build_notifier()

    if args.once or args.confirm_send is not None or args.stop or args.start_loop:
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
