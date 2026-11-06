"""个人数据备份与恢复（SQLite 在线备份 + 完整性校验 + manifest）。

原则：
- 备份用 SQLite backup API，不直接复制写入中的数据库文件；
- 每个库做 integrity_check / foreign_key_check；
- manifest 记录哈希、字节数、schema 版本与目录统计；
- 恢复前先校验备份，恢复后再次校验。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time

from pathlib import Path

MANIFEST = "manifest.json"
FILE_DIRS = ("sandboxes", "artifacts")


class BackupError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)

    return digest.hexdigest()


def _connect_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _db_report(path: Path) -> dict:
    try:
        conn = _connect_readonly(path)
    except sqlite3.Error as exc:
        raise BackupError(f"打不开数据库 {path.name}: {exc}") from exc

    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        foreign = [row[0] for row in conn.execute("PRAGMA foreign_key_check")]
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
        ).fetchone()[0]
    finally:
        conn.close()

    return {
        "name": path.name,
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
        "integrity": integrity,
        "foreign_key_issues": foreign,
        "user_version": version,
        "tables": tables,
    }


def _copy_database(source: Path, target: Path) -> None:
    src = _connect_readonly(source)
    dst = sqlite3.connect(target)

    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()


def backup_data(data_dir: Path, backup_root: Path) -> dict:
    data_dir = Path(data_dir)
    backup_root = Path(backup_root)

    if not data_dir.is_dir():
        raise BackupError(f"数据目录不存在：{data_dir}")

    databases = sorted(
        path for path in data_dir.glob("*.db") if path.is_file()
    )

    if not databases:
        raise BackupError(f"数据目录里没有数据库：{data_dir}")

    target = backup_root / f"backup-{time.strftime('%Y%m%d-%H%M%S')}"
    target.mkdir(parents=True, exist_ok=False)

    database_reports = []

    for database in databases:
        destination = target / database.name
        _copy_database(database, destination)
        report = _db_report(destination)

        if report["integrity"] != "ok" or report["foreign_key_issues"]:
            raise BackupError(
                f"备份副本校验失败：{database.name} "
                f"integrity={report['integrity']} "
                f"fk={report['foreign_key_issues']}"
            )

        report["source_sha256"] = _sha256(database)
        database_reports.append(report)

    file_reports = []

    for name in FILE_DIRS:
        source_dir = data_dir / name

        if not source_dir.is_dir():
            continue

        destination_dir = target / name
        shutil.copytree(source_dir, destination_dir)

        files = [
            path for path in destination_dir.rglob("*") if path.is_file()
        ]

        file_reports.append(
            {
                "name": name,
                "files": len(files),
                "bytes": sum(path.stat().st_size for path in files),
            }
        )

    manifest = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": str(data_dir),
        "databases": database_reports,
        "directories": file_reports,
    }

    (target / MANIFEST).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    manifest["path"] = str(target)

    return manifest


def verify_backup(backup_dir: Path) -> dict:
    backup_dir = Path(backup_dir)
    manifest_path = backup_dir / MANIFEST

    if not manifest_path.is_file():
        raise BackupError(f"缺少 manifest：{manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    problems = []

    for item in manifest.get("databases", []):
        path = backup_dir / item["name"]

        if not path.is_file():
            problems.append(f"{item['name']}: 文件缺失")
            continue

        if _sha256(path) != item["sha256"]:
            problems.append(f"{item['name']}: 哈希不一致")
            continue

        report = _db_report(path)

        if report["integrity"] != "ok":
            problems.append(f"{item['name']}: integrity={report['integrity']}")

        if report["foreign_key_issues"]:
            problems.append(f"{item['name']}: 外键问题")

    return {"ok": not problems, "problems": problems, "manifest": manifest}


def restore_data(backup_dir: Path, target_dir: Path) -> dict:
    backup_dir = Path(backup_dir)
    target_dir = Path(target_dir)

    check = verify_backup(backup_dir)

    if not check["ok"]:
        raise BackupError("备份校验失败：" + "; ".join(check["problems"]))

    target_dir.mkdir(parents=True, exist_ok=True)
    restored = []

    for item in check["manifest"].get("databases", []):
        shutil.copy2(backup_dir / item["name"], target_dir / item["name"])
        restored.append(item["name"])

    for item in check["manifest"].get("directories", []):
        source = backup_dir / item["name"]
        destination = target_dir / item["name"]

        if destination.exists():
            shutil.rmtree(destination)

        shutil.copytree(source, destination)
        restored.append(item["name"] + "/")

    for name in restored:
        path = target_dir / name.rstrip("/")

        if path.suffix == ".db" and _db_report(path)["integrity"] != "ok":
            raise BackupError(f"恢复后校验失败：{name}")

    return {"restored": restored, "target": str(target_dir)}
