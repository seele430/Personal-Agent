"""记忆服务：同 key 更新、当前事实、owner 隔离。"""

import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from src.memory import service
from src.memory.store import MemoryStore


class MemoryServiceCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.store = MemoryStore(Path(self._temporary.name) / "memory.db")
        self.patch = patch("src.memory.service.get_memory_store", return_value=self.store)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_remember_same_key_supersedes(self):
        old = service.remember("u1", "喜欢绿茶", key="drink")
        new = service.remember("u1", "改为喜欢红茶", key="drink")
        self.assertEqual(new.supersedes_id, old.id)
        self.assertTrue(old.conflict_group)
        self.assertEqual(old.conflict_group, new.conflict_group)

    def test_old_query_returns_new_only(self):
        old = service.remember("u1", "喜欢绿茶", key="drink")
        new = service.remember("u1", "改为喜欢红茶", key="drink")
        for query in ("绿茶", "红茶"):
            hits = service.recall("u1", query)["hits"]
            self.assertEqual([hit.item.id for hit in hits], [new.id])
        self.assertEqual(service.get_item("u1", old.id).text, "喜欢绿茶")

    def test_no_key_does_not_merge(self):
        service.remember("u1", "事实一")
        service.remember("u1", "事实二")
        hits = service.recall("u1", "事实")["hits"]
        self.assertGreaterEqual(len(hits), 2)

    def test_owner_isolation(self):
        service.remember("u1", "甲的秘密", key="secret")
        self.assertEqual(service.recall("u2", "甲的秘密")["hits"], [])
        self.assertIsNone(service.get_item("u2", 1))

    def test_deleted_old_not_used_as_parent(self):
        old = service.remember("u1", "旧值", key="k")
        self.store.delete_item(old.id)
        new = service.remember("u1", "新值", key="k")
        self.assertIsNone(new.supersedes_id)

    def test_empty_text_rejected(self):
        with self.assertRaises(Exception):
            service.remember("u1", "   ")

    def test_recall_query_required(self):
        with self.assertRaises(Exception):
            service.recall("u1", "")



    def test_list_current_folds_versions(self):
        service.remember("u1", "绿茶", key="drink")
        service.remember("u1", "红茶", key="drink")
        listing = service.list_current("u1")
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["items"][0]["text"], "红茶")

    def test_list_current_pagination(self):
        for index in range(5):
            service.remember("u1", f"事实{index}", key=f"k{index}")
        page = service.list_current("u1", limit=2, offset=2)
        self.assertEqual(len(page["items"]), 2)
        self.assertEqual(page["total"], 5)
        self.assertTrue(page["has_more"])

    def test_history_from_any_anchor(self):
        old = service.remember("u1", "绿茶", key="drink")
        mid = service.remember("u1", "红茶", key="drink")
        new = service.remember("u1", "乌龙", key="drink")
        for anchor in (old.id, mid.id, new.id):
            result = service.history("u1", anchor)
            self.assertEqual([row["id"] for row in result["items"]], [old.id, mid.id, new.id])
            self.assertEqual(result["current_id"], new.id)

    def test_history_status_deleted(self):
        item = service.remember("u1", "旧", key="k")
        service.remember("u1", "新", key="k")
        self.store.delete_item(item.id)
        result = service.history("u1", item.id)
        self.assertEqual(result["items"][0]["version_status"], "deleted")

    def test_history_fork_rejected(self):
        root = service.remember("u1", "root", key="k")
        child = service.remember("u1", "child", key="k")
        connection = __import__("sqlite3").connect(self.store.db_path)
        connection.execute(
            "INSERT INTO memory_items (owner_id,kind,layer,trust,key,value,text,source_ref,"
            "valid_from,supersedes_id,conflict_group,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("u1", "project_fact", "project", "user_confirmed", "k2", "", "fork",
             "api:test", "2020-01-01T00:00:00+00:00", root.id, "g", "2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00"),
        )
        connection.commit()
        connection.close()

        with self.assertRaises(service.HistoryError) as caught:
            service.history("u1", root.id)

        self.assertEqual(caught.exception.code, "history_forked")

    def test_history_cross_owner_404(self):
        item = service.remember("u1", "秘密", key="k")

        with self.assertRaises(service.HistoryError) as caught:
            service.history("u2", item.id)

        self.assertEqual(caught.exception.code, "memory_not_found")

    def test_history_truncates_long_chain(self):
        first = service.remember("u1", "v0", key="k")

        for index in range(1, 52):
            service.remember("u1", f"v{index}", key="k")

        result = service.history("u1", first.id)
        self.assertEqual(len(result["items"]), 50)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["total_count"], 52)
        self.assertTrue(any(row["id"] == first.id for row in result["items"]))


if __name__ == "__main__":
    unittest.main()
