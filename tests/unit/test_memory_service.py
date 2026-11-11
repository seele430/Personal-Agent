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


if __name__ == "__main__":
    unittest.main()
