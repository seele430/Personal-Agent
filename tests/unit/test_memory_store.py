"""记忆条目存储测试：写入、有效期、检索、删除派生、审计。

每个用例一个临时 `knowledge.db`，不碰真实库。
"""

import tempfile
import unittest

from pathlib import Path

from src.memory.models import MemoryError, utc_now
from src.memory.store import MemoryStore


OWNER = "u-test"


def iso_days(offset: int) -> str:
    from datetime import datetime, timedelta, timezone

    moment = datetime.now(timezone.utc) + timedelta(days=offset)

    return moment.isoformat(timespec="seconds")


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.store = MemoryStore(Path(self._temporary.name) / "knowledge.db")

    def add(self, text, **kwargs):
        base = {
            "kind": "preference",
            "layer": "identity",
            "trust": "user_confirmed",
        }
        base.update(kwargs)

        return self.store.add_item(OWNER, text=text, **base)


class TestWriting(StoreTestCase):
    def test_add_item_defaults(self):
        item = self.add("用户偏好中文回答，术语保留英文", key="language")

        self.assertGreater(item.id, 0)
        self.assertEqual(item.owner_id, OWNER)
        self.assertEqual(item.kind, "preference")
        self.assertEqual(item.trust, "user_confirmed")
        self.assertTrue(item.valid_from)
        self.assertIsNone(item.valid_to)
        self.assertIsNone(item.deleted_at)
        self.assertTrue(item.is_effective())

    def test_validation_rejects_bad_fields(self):
        bad = (
            {"kind": "unknown"},
            {"layer": "global"},
            {"trust": "trusted"},
        )

        for kwargs in bad:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(MemoryError):
                    self.add("正文", **kwargs)

        with self.assertRaises(MemoryError):
            self.store.add_item("", text="正文", kind="preference", layer="identity", trust="user_confirmed")

        with self.assertRaises(MemoryError):
            self.add("   ")

    def test_validity_window_is_checked(self):
        with self.assertRaises(MemoryError):
            self.add("正文", valid_from=iso_days(1), valid_to=iso_days(0))

        with self.assertRaises(MemoryError):
            self.add("正文", valid_from="not-a-date")

    def test_supersedes_must_exist(self):
        with self.assertRaises(MemoryError):
            self.add("新事实", supersedes_id=9999)

    def test_get_by_key_returns_latest_live_item(self):
        self.add("旧偏好：简洁", key="style")
        newer = self.add("新偏好：详细", key="style")

        found = self.store.get_by_key(OWNER, kind="preference", key="style")

        self.assertEqual(found.id, newer.id)


class TestSearch(StoreTestCase):
    def test_chinese_fts_query(self):
        self.add("用户偏好中文回答，术语保留英文原词", key="language")

        result = self.store.search(OWNER, "中文回答")

        self.assertEqual(result["mode"], "fts")
        self.assertEqual(len(result["hits"]), 1)
        self.assertIn("中文回答", result["hits"][0].item.text)

    def test_short_query_falls_back_to_like(self):
        self.add("部署在 E 盘，避免占用 C 盘", key="path")

        result = self.store.search(OWNER, "占用")

        self.assertEqual(result["mode"], "like")
        self.assertEqual(len(result["hits"]), 1)
        self.assertIn("占用", result["hits"][0].item.text)

    def test_english_query(self):
        self.add("Prefers TypeScript after Python for tooling", key="lang")

        result = self.store.search(OWNER, "TypeScript")

        self.assertEqual(result["mode"], "fts")
        self.assertEqual(result["hits"][0].item.key, "lang")

    def test_filters_by_kind_and_layer(self):
        self.add("偏好：中文", kind="preference", layer="identity")
        self.add("项目：PA 用 SQLite", kind="project_fact", layer="project")

        only_facts = self.store.search(OWNER, "SQLite", kind="project_fact")
        only_identity = self.store.search(OWNER, "中文", layer="identity")

        self.assertEqual(len(only_facts["hits"]), 1)
        self.assertEqual(only_facts["hits"][0].item.kind, "project_fact")
        self.assertEqual(len(only_identity["hits"]), 1)

    def test_min_trust_filters_low_trust_items(self):
        self.add("模型猜的：用户喜欢周五", trust="model_inferred", key="guess")
        self.add("用户确认：周五不开会", trust="user_confirmed", key="confirmed")

        strict = self.store.search(OWNER, "周五", min_trust="platform_verified")
        loose = self.store.search(OWNER, "周五")

        self.assertEqual(len(strict["hits"]), 1)
        self.assertEqual(strict["hits"][0].item.trust, "user_confirmed")
        self.assertEqual(len(loose["hits"]), 2)

    def test_invalid_query_parameters(self):
        with self.assertRaises(ValueError):
            self.store.search(OWNER, "  ")

        with self.assertRaises(ValueError):
            self.store.search(OWNER, "x", k=0)

        with self.assertRaises(MemoryError):
            self.store.search("", "x")


class TestValidityAndDeletion(StoreTestCase):
    def test_expired_item_is_invisible_by_default(self):
        item = self.add("旧项目在 C 盘", key="where", valid_from=iso_days(-10), valid_to=iso_days(-1))

        self.assertFalse(item.is_effective())
        self.assertEqual(self.store.search(OWNER, "C 盘")["hits"], [])

        expired_view = self.store.search(OWNER, "C 盘", include_expired=True)

        self.assertEqual(len(expired_view["hits"]), 1)

    def test_future_item_is_invisible_until_effective(self):
        self.add("下季度切到 F 盘", key="future", valid_from=iso_days(1))

        self.assertEqual(self.store.search(OWNER, "F 盘")["hits"], [])

        future_view = self.store.search(
            OWNER, "F 盘", include_expired=True, include_future=True
        )

        self.assertEqual(len(future_view["hits"]), 1)

    def test_expire_item_keeps_history(self):
        item = self.add("旧事实", key="fact")

        expired = self.store.expire_item(item.id, reason="改主意")

        self.assertIsNotNone(expired.valid_to)
        self.assertEqual(self.store.search(OWNER, "旧事实")["hits"], [])
        self.assertIsNotNone(self.store.get_item(item.id))

    def test_delete_removes_fts_row_and_conflict_membership(self):
        item = self.add("待删除事实", key="gone", conflict_group="g1")

        self.assertEqual(self.store.fts_count(), 1)
        self.assertTrue(self.store.delete_item(item.id))

        self.assertIsNone(self.store.get_item(item.id).conflict_group or None)
        self.assertEqual(self.store.fts_count(), 0)
        self.assertEqual(self.store.search(OWNER, "待删除事实")["hits"], [])
        self.assertEqual(
            self.store.search(OWNER, "待删除事实", include_expired=True)["hits"],
            [],
        )

    def test_purge_removes_row_entirely(self):
        item = self.add("要物理删除", key="purge")

        self.assertTrue(self.store.delete_item(item.id, purge=True))

        self.assertIsNone(self.store.get_item(item.id))
        self.assertEqual(self.store.count_items(OWNER, include_deleted=True), 0)

    def test_delete_missing_item_returns_false(self):
        self.assertFalse(self.store.delete_item(424242))

    def test_events_are_recorded(self):
        item = self.add("审计事实", key="audit")
        self.store.expire_item(item.id)
        self.store.delete_item(item.id)

        actions = [event.action for event in self.store.events(item.id)]

        self.assertEqual(actions[:3], ["add", "expire", "delete"])


class TestCounting(StoreTestCase):
    def test_counts_respect_deleted_flag(self):
        first = self.add("事实一")
        self.add("事实二")

        self.assertEqual(self.store.count_items(OWNER), 2)
        self.store.delete_item(first.id)
        self.assertEqual(self.store.count_items(OWNER), 1)
        self.assertEqual(self.store.count_items(OWNER, include_deleted=True), 2)

    def test_list_items_limit_and_deleted_filter(self):
        self.add("一")
        item = self.add("二")
        self.store.delete_item(item.id)

        self.assertEqual(len(self.store.list_items(OWNER)), 1)
        self.assertEqual(
            len(self.store.list_items(OWNER, include_deleted=True)), 2
        )

        with self.assertRaises(ValueError):
            self.store.list_items(OWNER, limit=0)


if __name__ == "__main__":
    unittest.main()
