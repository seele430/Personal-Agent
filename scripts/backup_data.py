"""数据备份 / 恢复 / 校验。

用法：
    python scripts/backup_data.py backup  --data-dir "$env:TEMP\pa-demo" --backup-dir "$env:TEMP\pa-backups"
    python scripts/backup_data.py verify  --backup "$env:TEMP\pa-backups\backup-20261106-100000"
    python scripts/backup_data.py restore --backup <备份目录> --data-dir <新目录>

只读源库；恢复前先校验，恢复后再次 integrity_check。
"""

from __future__ import annotations

import argparse
import json
import sys

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.backup import (
    BackupError,
    backup_data,
    restore_data,
    verify_backup,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    backup = sub.add_parser("backup")
    backup.add_argument("--data-dir", required=True)
    backup.add_argument("--backup-dir", required=True)

    verify = sub.add_parser("verify")
    verify.add_argument("--backup", required=True)

    restore = sub.add_parser("restore")
    restore.add_argument("--backup", required=True)
    restore.add_argument("--data-dir", required=True)

    args = parser.parse_args()

    try:
        if args.command == "backup":
            payload = backup_data(Path(args.data_dir), Path(args.backup_dir))
        elif args.command == "verify":
            payload = verify_backup(Path(args.backup))
        else:
            payload = restore_data(Path(args.backup), Path(args.data_dir))
    except BackupError as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
