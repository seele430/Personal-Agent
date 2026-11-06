"""备份 → 破坏 → 恢复 的真实演练（只动临时目录）。"""

from __future__ import annotations

import hashlib
import json
import unittest

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.auth_api import get_auth_store
from src.core.auth_store import AuthStore
from src.core.backup import (
    BackupError,
    backup_data,
    restore_data,
    verify_backup,
)
from src.core.run_store import RunStore
from src.main import app
from tests.helper import isolate_paths


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

        self.password = "backup-restore-2026"
        AuthStore(self.data / "auth_users.db").bootstrap_owner("seele", self.password)

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
        self.assertEqual(len(report["manifest"]["databases"]), 2)  # chat + auth
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
        token = AuthStore(target / "auth_users.db").login("seele", self.password)
        self.assertTrue(token)
        self.assertTrue(
            (target / "sandboxes" / "sbx_demo" / "workspace" / "chart.png").is_file()
        )

    def test_tampered_artifact_is_rejected(self):
        manifest = backup_data(self.data, self.backups)
        backup_dir = Path(manifest["path"])
        chart = backup_dir / "sandboxes" / "sbx_demo" / "workspace" / "chart.png"

        chart.write_bytes(b"EVIL")

        report = verify_backup(backup_dir)

        self.assertFalse(report["ok"])
        self.assertTrue(any("不一致" in item for item in report["problems"]))

        with self.assertRaises(BackupError):
            restore_data(backup_dir, Path(self.temp.name) / "restore-tamper")

    def test_missing_artifact_is_rejected(self):
        manifest = backup_data(self.data, self.backups)
        backup_dir = Path(manifest["path"])

        (backup_dir / "sandboxes" / "sbx_demo" / "workspace" / "chart.png").unlink()

        report = verify_backup(backup_dir)

        self.assertFalse(report["ok"])
        self.assertTrue(any("文件缺失" in item for item in report["problems"]))

    def test_extra_artifact_is_rejected(self):
        manifest = backup_data(self.data, self.backups)
        backup_dir = Path(manifest["path"])

        (backup_dir / "sandboxes" / "sbx_demo" / "workspace" / "extra.bin").write_bytes(b"x")

        report = verify_backup(backup_dir)

        self.assertFalse(report["ok"])
        self.assertTrue(any("多出未登记文件" in item for item in report["problems"]))

    def test_renamed_artifact_is_rejected(self):
        manifest = backup_data(self.data, self.backups)
        backup_dir = Path(manifest["path"])
        workspace = backup_dir / "sandboxes" / "sbx_demo" / "workspace"

        (workspace / "chart.png").rename(workspace / "renamed.png")

        report = verify_backup(backup_dir)

        self.assertFalse(report["ok"])
        texts = " ".join(report["problems"])
        self.assertIn("文件缺失", texts)
        self.assertIn("多出未登记文件", texts)

    def test_manifest_records_per_file_hash(self):
        manifest = backup_data(self.data, self.backups)
        entries = manifest["directories"][0]["entries"]

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["path"], "sandboxes/sbx_demo/workspace/chart.png")
        self.assertEqual(entries[0]["sha256"], sha256(self.data / "sandboxes" / "sbx_demo" / "workspace" / "chart.png"))

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


    def test_restored_data_starts_full_app(self):
        """恢复后不是只查库：启动完整 FastAPI，owner 登录、/auth/me、成果文件都可用。"""

        manifest = backup_data(self.data, self.backups)
        target = Path(self.temp.name) / "app-restored"
        restore_data(Path(manifest["path"]), target)

        # 把全局 paths 指向恢复目录（isolate_paths 会替换所有已导入模块的绑定）。
        isolate_paths(self, SimpleNamespace(name=str(target)))
        get_auth_store.cache_clear()
        self.addCleanup(get_auth_store.cache_clear)

        client = TestClient(app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)

        login = client.post(
            "/auth/login",
            json={"username": "seele", "password": self.password},
        )
        self.assertEqual(login.status_code, 200, login.text)

        token = login.json()["token"]
        me = client.get(
            "/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(me.status_code, 200, me.text)

        chart = target / "sandboxes" / "sbx_demo" / "workspace" / "chart.png"
        self.assertEqual(chart.read_bytes(), b"PNG-DEMO")


    def test_legacy_manifest_is_supported_with_warning(self):
        """旧备份没有逐文件哈希：降级为数量/字节校验，并明确提示。"""

        manifest = backup_data(self.data, self.backups)
        backup_dir = Path(manifest["path"])
        manifest_path = backup_dir / "manifest.json"

        payload = json.loads(manifest_path.read_text(encoding="utf-8"))

        for item in payload["directories"]:
            item.pop("entries", None)

        manifest_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        report = verify_backup(backup_dir)

        self.assertTrue(report["ok"], report["problems"])
        self.assertTrue(any("旧格式" in item for item in report["warnings"]))

        target = Path(self.temp.name) / "legacy-restored"
        restored = restore_data(backup_dir, target)

        self.assertTrue(restored["warnings"])
        self.assertTrue(
            (target / "sandboxes" / "sbx_demo" / "workspace" / "chart.png").is_file()
        )

    def test_legacy_manifest_detects_count_mismatch(self):
        manifest = backup_data(self.data, self.backups)
        backup_dir = Path(manifest["path"])
        manifest_path = backup_dir / "manifest.json"
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))

        for item in payload["directories"]:
            item.pop("entries", None)
            item["files"] = item["files"] + 1

        manifest_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        report = verify_backup(backup_dir)

        self.assertFalse(report["ok"])
        self.assertTrue(any("旧格式" in item for item in report["problems"]))
if __name__ == "__main__":
    unittest.main()
