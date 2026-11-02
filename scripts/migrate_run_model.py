"""在旧库副本上执行 D6 迁移或从指定备份恢复（JSON 输出）。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.run_migration import migrate_legacy_database, restore_backup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    migrate = subparsers.add_parser("migrate", help="备份并迁移旧库副本")
    migrate.add_argument("--database", type=Path, required=True)
    migrate.add_argument("--backup-dir", type=Path, required=True)
    migrate.add_argument(
        "--confirm-copy",
        action="store_true",
        help="确认 database 是临时旧库副本，而不是真实数据目录",
    )

    rollback = subparsers.add_parser("rollback", help="从迁移前备份完整恢复")
    rollback.add_argument("--database", type=Path, required=True)
    rollback.add_argument("--backup", type=Path, required=True)
    rollback.add_argument("--confirm-copy", action="store_true")
    args = parser.parse_args()

    if not args.confirm_copy:
        parser.error("必须显式传入 --confirm-copy；禁止直接操作真实数据目录")

    if args.command == "migrate":
        result = migrate_legacy_database(args.database, args.backup_dir)
    else:
        restored = restore_backup(args.database, args.backup)
        result = {
            "status": "restored",
            "database": str(args.database.resolve()),
            "backup": str(args.backup.resolve()),
            "legacy_rows": restored,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
