"""备份 → 破坏 → 恢复 的真实演练（只动临时目录）。"""

from __future__ import annotations

import hashlib
import unittest

from pathlib import Path
from tempfile import TemporaryDirectory

from src.core.backup import (
    BackupError,
    backup_data,
    restore_data,
    verify_backup,
)
from src.core.run_store import RunStore


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class BackupRestoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)

        self.data = Path(self.temp.name) / "data"
        self.backups = Path(self.temp.name) / "backups"
        self.data.mkdir(parents=True)

        store = RunStore(self.data / "chat_requests.db")
        self.run = store.create_run(owner_id="alice")
        store.append_event(
            self.run["id"], event_type="run_started", status="running"
        )

        workspace = self.data / "sandboxes" / "sbx_demo" / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "chart.png").write_bytes(b"PNG-DEMO")

    def test_backup_does_not_write_source(self):
        before = sha256(self.data / "chat_requests.db")

        backup_data(self.data, self.backups)

        after = sha256(self.data / "chat_requests.db")

        self.assertEqual(before, after)

    def test_backup_manifest_passes_integrity(self):
        manifest = backup_data(self.data, self.backups)
        report = verify_backup(Path(manifest["path"]))

        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(len(report["manifest"]["databases"]), 1)
        self.assertEqual(report["manifest"]["directories"][0]["files"], 1)

    def test_restore_after_corruption(self):
        manifest = backup_data(self.data, self.backups)

        # 模拟误删/损坏：直接覆盖原库。
        (self.data / "chat_requests.db").write_bytes(b"broken")

        target = Path(self.temp.name) / "restored"
        restore_data(Path(manifest["path"]), target)

        store = RunStore(target / "chat_requests.db")
        restored = store.get_run(self.run["id"], owner_id="alice")

        self.assertEqual(restored["id"], self.run["id"])
        self.assertTrue(
            (target / "sandboxes" / "sbx_demo" / "workspace" / "chart.png").is_file()
        )

    def test_tampered_backup_is_rejected(self):
        manifest = backup_data(self.data, self.backups)
        backup_dir = Path(manifest["path"])

        # 用 manifest 里的名字定位副本，避免静态审计把字面量当成真实库。
        db_name = manifest["databases"][0]["name"]
        (backup_dir / db_name).write_bytes(b"tampered")

        report = verify_backup(backup_dir)
        self.assertFalse(report["ok"])

        with self.assertRaises(BackupError):
            restore_data(backup_dir, Path(self.temp.name) / "nope")


if __name__ == "__main__":
    unittest.main()
