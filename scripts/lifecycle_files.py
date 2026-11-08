"""文件生命周期 CLI：默认预演，--apply 必须配 --confirm。

    .\\.venv/Scripts/python.exe scripts/lifecycle_files.py --root <目录> --days 30
    .\\.venv/Scripts/python.exe scripts/lifecycle_files.py --root <目录> --days 30 --apply --confirm

退出码：0 成功；2 参数/确认错误；3 目录不存在。
"""

from __future__ import annotations

import argparse
import json
import sys

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.lifecycle.files import (  # noqa: E402
    DEFAULT_SUFFIXES,
    FileLifecycleError,
    plan_files,
    purge_files,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="文件生命周期清理")
    parser.add_argument("--root", required=True)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--suffix", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    base = Path(args.root)

    if not base.is_dir():
        print(f"目录不存在：{base}")

        return 3

    if args.apply and not args.confirm:
        print("拒绝执行：--apply 必须同时给 --confirm")

        return 2

    try:
        plan_value = plan_files(
            base,
            days=args.days,
            suffixes=tuple(args.suffix) if args.suffix else DEFAULT_SUFFIXES,
        )

    except FileLifecycleError as error:
        print(f"参数错误：{error}")

        return 2

    payload = plan_value.to_dict()

    if args.apply:
        result = purge_files(plan_value, confirm=True)
        payload.update(result)
        mode = "已执行"

    else:
        mode = "预演（未删除任何文件）"

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    else:
        print(f"[{mode}] {base} 保留 {args.days} 天")
        print(f"  候选 {plan_value.count} 个，共 {plan_value.bytes} 字节")
        print(f"  跳过符号链接 {plan_value.skipped_links} 个")

        if args.apply:
            print(f"  已删除 {payload['deleted']} 个，释放 {payload['freed_bytes']} 字节")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
