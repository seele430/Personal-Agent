"""版本快照测试：保存、校验、回滚、回滚后的可检索性。"""

import json
import sqlite3
import tempfile
import unittest

from pathlib import Path

from src.memory.store import MemoryStore
from src.memory.versions import MemoryVersions, SnapshotError


OWNER = "u-test"


class VersionTestCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.db = Path(self._temporary.name) / "knowledge.db"
        self.store = MemoryStore(self.db)
        self.versions = MemoryVersions(self.store)

    def add(self, text, **kwargs):
        base = {
            "kind": "project_fact",
            "layer": "project",
            "trust": "user_confirmed",
            "key": "k",
        }
        base.update(kwargs)

        return self.store.add_item(OWNER, text=text, **base)


class TestCreate(VersionTestCase):
    def test_create_captures_count_and_hash(self):
        self.add("事实一")
        self.add("事实二")

        snapshot = self.versions.create("初始")

        self.assertGreater(snapshot.id, 0)
        self.assertEqual(snapshot.item_count, 2)
        self.assertEqual(len(snapshot.sha256), 64)

    def test_same_content_same_hash(self):
        self.add("稳定事实")

        first = self.versions.create("a")
        second = self.versions.create("b")

        self.assertEqual(first.sha256, second.sha256)

    def test_changed_content_changes_hash(self):
        self.add("事实一")
        first = self.versions.create("a")
        self.add("事实二")
        second = self.versions.create("b")

        self.assertNotEqual(first.sha256, second.sha256)
        self.assertEqual(second.item_count, 2)

    def test_empty_store_can_be_snapshotted(self):
        snapshot = self.versions.create("空库")

        self.assertEqual(snapshot.item_count, 0)

    def test_label_is_required(self):
        for bad in ("", "   ", None):
            with self.subTest(label=bad):
                with self.assertRaises(SnapshotError):
                    self.versions.create(bad)


class TestRead(VersionTestCase):
    def test_list_is_newest_first(self):
        first = self.versions.create("一")
        second = self.versions.create("二")

        listed = self.versions.list_snapshots()

        self.assertEqual([snapshot.id for snapshot in listed], [second.id, first.id])

        with self.assertRaises(ValueError):
            self.versions.list_snapshots(limit=0)

    def test_get_returns_items(self):
        item = self.add("可读事实")

        snapshot = self.versions.create("s")
        loaded, items = self.versions.get(snapshot.id)

        self.assertEqual(loaded.id, snapshot.id)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], item.id)
        self.assertEqual(items[0]["text"], "可读事实")

    def test_missing_snapshot_raises(self):
        with self.assertRaises(SnapshotError):
            self.versions.get(9999)

    def test_tampered_payload_is_detected(self):
        snapshot = self.versions.create("s")

        connection = sqlite3.connect(self.db)
        self.addCleanup(connection.close)

        with connection:
            connection.execute(
                "UPDATE memory_snapshots SET payload = ? WHERE id = ?",
                (json.dumps({"version": 1, "items": []}), snapshot.id),
            )

        with self.assertRaises(SnapshotError):
            self.versions.get(snapshot.id)


class TestRestore(VersionTestCase):
    def test_restore_removes_items_added_later(self):
        self.add("保留的事实")
        snapshot = self.versions.create("before")

        extra = self.add("后来加的")
        self.assertIsNotNone(self.store.get_item(extra.id))

        stats = self.versions.restore(snapshot.id)

        self.assertEqual(stats["restored"], 1)
        self.assertEqual(self.store.count_items(OWNER), 1)
        self.assertIsNone(self.store.get_item(extra.id))

    def test_restore_brings_back_deleted_item_and_makes_it_searchable(self):
        item = self.add("重要事实：部署在 E 盘")
        snapshot = self.versions.create("before")
        self.store.delete_item(item.id)

        self.assertEqual(self.store.search(OWNER, "部署在")["hits"], [])

        self.versions.restore(snapshot.id)

        hits = self.store.search(OWNER, "部署在")["hits"]

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].item.id, item.id)

    def test_restore_preserves_ids_and_validity(self):
        item = self.add(
            "带有效期的条目",
            valid_from="2026-01-01T00:00:00+00:00",
            valid_to="2026-06-01T00:00:00+00:00",
        )
        snapshot = self.versions.create("s")

        self.versions.restore(snapshot.id)

        restored = self.store.get_item(item.id)

        self.assertIsNotNone(restored)
        self.assertEqual(restored.valid_from, "2026-01-01T00:00:00+00:00")
        self.assertEqual(restored.valid_to, "2026-06-01T00:00:00+00:00")

    def test_new_item_after_restore_gets_fresh_id(self):
        first = self.add("一")
        snapshot = self.versions.create("s")
        self.versions.restore(snapshot.id)

        fresh = self.add("二")

        self.assertGreater(fresh.id, first.id)

    def test_soft_deleted_item_is_restored_without_fts_row(self):
        item = self.add("已删除事实")
        self.store.delete_item(item.id)
        snapshot = self.versions.create("含已删除")

        self.versions.restore(snapshot.id)

        self.assertIsNotNone(self.store.get_item(item.id))
        self.assertEqual(self.store.fts_count(), 0)

    def test_diff_counts(self):
        self.add("一")
        first = self.versions.create("a")
        self.add("二")
        second = self.versions.create("b")

        diff = self.versions.diff_counts(first.id, second.id)

        self.assertEqual(diff["only_left"], 0)
        self.assertEqual(diff["only_right"], 1)
        self.assertEqual(diff["common"], 1)


if __name__ == "__main__":
    unittest.main()
