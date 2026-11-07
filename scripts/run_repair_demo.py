"""跑一次真实的代码修复闭环（会调用模型，产生 token 费用）。

    .\\.venv\\Scripts\\python.exe scripts/run_repair_demo.py --fixture off_by_one

默认每次修复 1 次模型调用，最多 3 轮；把每一轮的测试输出摘录与 token 用量
写进 `pa-w5-evidence-20261107/`，不打印任何密钥。

退出码：0 修好；1 没修好（如实记失败，不重试到过为止）；2 参数问题。
"""

from __future__ import annotations

import argparse
import json
import sys

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.repair.fixtures import get_fixture, list_fixtures  # noqa: E402
from src.repair.loop import RepairLoop  # noqa: E402


OUT_DIR = (
    Path(r"C:\Users\123\Documents\deepseek-harness\default-workspace")
    / "pa-w5-evidence-20261107"
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="真实代码修复闭环演示")
    parser.add_argument("--fixture", default="off_by_one")
    parser.add_argument("--max-rounds", type=int, default=3)
    parser.add_argument("--out", default=str(OUT_DIR))
    args = parser.parse_args(argv)

    if args.fixture not in list_fixtures():
        parser.error(f"未知夹具 {args.fixture}，可选 {list_fixtures()}")

    fixture = get_fixture(args.fixture)

    loop = RepairLoop(fixture, max_rounds=args.max_rounds)

    started = datetime.now(timezone.utc)
    result = loop.run()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"repair-{args.fixture}-{stamp}.json"

    payload = {
        "generated_at": started.isoformat(),
        "fixture": fixture.name,
        "description": fixture.description,
        "result": result.to_dict(),
    }

    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"fixture={result.fixture} success={result.success} "
        f"rounds={result.rounds} score={result.score:.2f}"
    )

    for attempt in result.attempts:
        mark = "OK" if attempt.ok else "FAIL"
        print(
            f"  轮 {attempt.round_index}: {mark} "
            f"failures={attempt.failures} errors={attempt.errors} "
            f"changed={list(attempt.changed_files)}"
        )

    if result.error:
        print(f"  error: {result.error}")

    print(f"证据：{path}")

    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
