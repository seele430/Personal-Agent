"""外部任务注册表 CLI：登记 / 列表 / 查看。

    .\\.venv/Scripts/python.exe scripts/collab_tasks.py register --provider kimi --external-id K-1 --run-id r1
    .\\.venv/Scripts/python.exe scripts/collab_tasks.py list --provider kimi
    .\\.venv/Scripts/python.exe scripts/collab_tasks.py show --provider kimi --external-id K-1

退出码：0 成功；2 参数/状态非法；3 任务不存在。
"""

from __future__ import annotations

import argparse
import json
import sys

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.collab.registry import (  # noqa: E402
    STATES,
    ExternalTaskRegistry,
    RegistryError,
    TaskNotFound,
)


def default_db() -> Path:
    from src.config import paths

    return paths.platform_db.parent / "external-tasks.db"


def build_registry(db: str | None) -> ExternalTaskRegistry:
    return ExternalTaskRegistry(Path(db) if db else default_db())


def render(task) -> str:
    artifacts = ",".join(task.artifacts) or "-"

    return (
        f"{task.provider}/{task.external_id}\t{task.state}\t"
        f"run={task.run_id}\tartifacts={artifacts}\t{task.updated_at}"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="外部任务注册表")
    parser.add_argument("--db", default="", help="覆盖数据库路径（测试用）")
    sub = parser.add_subparsers(dest="command", required=True)

    register = sub.add_parser("register")
    register.add_argument("--provider", required=True)
    register.add_argument("--external-id", required=True)
    register.add_argument("--run-id", required=True)
    register.add_argument("--state", default="submitted")
    register.add_argument("--artifact", action="append", default=[])
    register.add_argument("--json", action="store_true")

    listing = sub.add_parser("list")
    listing.add_argument("--provider", default="")
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--json", action="store_true")

    show = sub.add_parser("show")
    show.add_argument("--provider", required=True)
    show.add_argument("--external-id", required=True)
    show.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    registry = build_registry(args.db or None)

    try:
        if args.command == "register":
            task = registry.register(
                args.provider,
                args.external_id,
                run_id=args.run_id,
                state=args.state,
                artifacts=tuple(args.artifact),
            )
            payload = task.to_dict()

        elif args.command == "list":
            tasks = registry.list(
                provider=args.provider or None, limit=args.limit
            )
            payload = [task.to_dict() for task in tasks]

        else:
            payload = registry.get(args.provider, args.external_id).to_dict()

    except TaskNotFound:
        print(f"任务不存在：{args.provider}/{args.external_id}")

        return 3

    except RegistryError as error:
        print(f"参数错误：{error}")

        return 2

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    elif args.command == "list":
        if not payload:
            print("暂无外部任务")

        for item in payload:
            print(
                f"{item['provider']}/{item['external_id']}\t{item['state']}\t"
                f"run={item['run_id']}\t{','.join(item['artifacts']) or '-'}"
            )

    else:
        print(render(_as_task(payload)))

    return 0


class _AsTask:
    """把 dict 包成 render() 需要的属性访问。"""

    def __init__(self, payload: dict):
        self.provider = payload["provider"]
        self.external_id = payload["external_id"]
        self.state = payload["state"]
        self.run_id = payload["run_id"]
        self.artifacts = tuple(payload.get("artifacts") or ())
        self.updated_at = payload.get("updated_at", "")


def _as_task(payload: dict) -> _AsTask:
    return _AsTask(payload)


if __name__ == "__main__":
    raise SystemExit(main())
