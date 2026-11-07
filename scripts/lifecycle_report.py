"""数据生命周期：预演（默认）/ 执行（需 --apply --confirm）。

    # 预演：只打印候选，一行不删
    .\\venv/Scripts/python.exe scripts/lifecycle_report.py --data-dir <数据目录> --days 30

    # 执行：双开关
    .\\venv/Scripts/python.exe scripts/lifecycle_report.py --data-dir <数据目录> --days 30 --apply --confirm

退出码：0 成功；2 参数/确认错误；3 数据目录不存在。
"""

from __future__ import annotations

import argparse
import json
import sys

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.lifecycle.retention import (  # noqa: E402
    RetentionError,
    RetentionPolicy,
    apply,
    plan,
)


def data_dir_from(arg: str | None) -> Path:
    if arg:
        return Path(arg)

    from src.config import paths

    return paths.platform_db.parent


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="数据生命周期清理")
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--keep-last", type=int, default=50)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    base = data_dir_from(args.data_dir)

    if not base.is_dir():
        print(f"数据目录不存在：{base}")

        return 3

    if args.apply and not args.confirm:
        print("拒绝执行：--apply 必须同时给 --confirm")
        print("提示：先不带 --apply 跑一次看候选。")

        return 2

    try:
        policy = RetentionPolicy(days=args.days, keep_last=args.keep_last)

    except RetentionError as error:
        print(f"参数错误：{error}")

        return 2

    spans_db = base / "traces.db"
    platform_db = base / "platform.db"

    try:
        purge_plan = plan(
            spans_db=spans_db if spans_db.is_file() else None,
            platform_db=platform_db if platform_db.is_file() else None,
            policy=policy,
        )

    except RetentionError as error:
        print(f"清理计划失败：{error}")

        return 2

    if args.apply:
        result = apply(
            purge_plan,
            spans_db=spans_db if spans_db.is_file() else None,
            platform_db=platform_db if platform_db.is_file() else None,
            confirm=True,
        )
        summary = result.to_dict()
        mode = "已执行"

    else:
        summary = purge_plan.to_dict()
        summary["applied"] = False
        summary["deleted"] = 0
        mode = "预演（未删除任何数据）"

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))

    else:
        print(f"[{mode}] 保留期 {policy.days} 天，保留最近 {policy.keep_last} 条")

        for target in summary["targets"]:
            print(f"  {target['table']}: 候选 {target['count']} 条，cutoff={target['cutoff']}")

        print(f"  合计候选 {summary['total']} 条，已删除 {summary['deleted']} 条")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
