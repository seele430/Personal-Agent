"""冲突解析测试：谁算"当前有效的事实"。"""

import sqlite3
import tempfile
import unittest

from pathlib import Path

from src.memory.models import MemoryItem, utc_now
from src.memory.resolve import (
    ResolutionError,
    effective_items,
    explain,
    lineage,
    resolve_current,
    resolve_for_key,
    resolve_group,
)
from src.memory.store import MemoryStore


OWNER = "u-test"


def item(
    item_id,
    *,
    valid_from="2026-01-01T00:00:00+00:00",
    valid_to=None,
    trust="user_confirmed",
    created_at="2026-01-01T00:00:00+00:00",
    deleted_at=None,
    text="事实",
    key="k",
):
    return MemoryItem(
        id=item_id,
        owner_id=OWNER,
        kind="project_fact",
        layer="project",
        trust=trust,
        key=key,
        value="",
        text=text,
        source_ref="",
        source_doc_id=None,
        valid_from=valid_from,
        valid_to=valid_to,
        supersedes_id=None,
        conflict_group="g",
        created_at=created_at,
        updated_at=created_at,
        deleted_at=deleted_at,
    )


class TestResolveCurrent(unittest.TestCase):
    def test_newest_valid_from_wins(self):
        old = item(1, valid_from="2026-01-01T00:00:00+00:00", text="旧")
        new = item(2, valid_from="2026-06-01T00:00:00+00:00", text="新")

        resolution = resolve_current([old, new], at="2026-11-07T00:00:00+00:00")

        self.assertEqual(resolution.item.id, 2)
        self.assertIn("生效时间最新", resolution.reason)
        self.assertEqual([c.id for c in resolution.candidates], [2, 1])

    def test_expired_item_is_excluded(self):
        expired = item(
            1,
            valid_from="2026-01-01T00:00:00+00:00",
            valid_to="2026-03-01T00:00:00+00:00",
            text="已过期",
        )
        current = item(
            2,
            valid_from="2026-02-01T00:00:00+00:00",
            text="仍在用",
        )

        resolution = resolve_current([expired, current], at="2026-11-07T00:00:00+00:00")

        self.assertEqual(resolution.item.id, 2)

    def test_future_item_is_excluded(self):
        future = item(1, valid_from="2027-01-01T00:00:00+00:00", text="还没生效")
        current = item(2, valid_from="2026-01-01T00:00:00+00:00", text="当前")

        resolution = resolve_current([future, current], at="2026-11-07T00:00:00+00:00")

        self.assertEqual(resolution.item.id, 2)

    def test_deleted_item_is_excluded(self):
        deleted = item(1, deleted_at=utc_now(), text="删了")
        alive = item(2, text="还在")

        resolution = resolve_current([deleted, alive])

        self.assertEqual(resolution.item.id, 2)

    def test_same_time_higher_trust_wins(self):
        inferred = item(1, trust="model_inferred", text="模型猜的")
        confirmed = item(2, trust="user_confirmed", text="用户确认的")

        resolution = resolve_current([inferred, confirmed], at="2026-11-07T00:00:00+00:00")

        self.assertEqual(resolution.item.id, 2)
        self.assertIn("信任等级", resolution.reason)

    def test_same_time_and_trust_latest_write_wins(self):
        first = item(1, created_at="2026-01-01T00:00:00+00:00")
        second = item(2, created_at="2026-02-01T00:00:00+00:00")

        resolution = resolve_current([first, second], at="2026-11-07T00:00:00+00:00")

        self.assertEqual(resolution.item.id, 2)
        self.assertIn("写入顺序", resolution.reason)

    def test_no_candidates(self):
        resolution = resolve_current([], at="2026-11-07T00:00:00+00:00")

        self.assertFalse(resolution.found)
        self.assertEqual(resolution.reason, "没有生效中的条目")

    def test_single_candidate_reason(self):
        resolution = resolve_current([item(1)], at="2026-11-07T00:00:00+00:00")

        self.assertTrue(resolution.found)
        self.assertEqual(resolution.reason, "组内唯一生效事实")

    def test_effective_items_respects_at(self):
        early = item(1, valid_from="2026-01-01T00:00:00+00:00", valid_to="2026-02-01T00:00:00+00:00")
        late = item(2, valid_from="2026-02-01T00:00:00+00:00")

        self.assertEqual(
            [c.id for c in effective_items([early, late], at="2026-01-15T00:00:00+00:00")],
            [1],
        )
        self.assertEqual(
            [c.id for c in effective_items([early, late], at="2026-03-01T00:00:00+00:00")],
            [2],
        )


class TestStoreResolution(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.db = Path(self._temporary.name) / "knowledge.db"
        self.store = MemoryStore(self.db)

    def add(self, text, **kwargs):
        base = {
            "kind": "project_fact",
            "layer": "project",
            "trust": "user_confirmed",
            "key": "where",
        }
        base.update(kwargs)

        return self.store.add_item(OWNER, text=text, **base)

    def test_resolve_group_picks_winner_and_explains(self):
        self.add(
            "项目放在 C 盘",
            conflict_group="disk",
            valid_from="2026-01-01T00:00:00+00:00",
            valid_to="2026-06-01T00:00:00+00:00",
        )
        new = self.add("项目改放 E 盘", conflict_group="disk")

        resolution = resolve_group(self.store, "disk", owner_id=OWNER)
        report = explain(resolution)

        self.assertEqual(resolution.item.id, new.id)
        self.assertEqual(report["chosen_id"], new.id)
        self.assertEqual(len(report["candidates"]), 1)

    def test_resolve_for_key_without_group(self):
        self.add("v1 用 3.12", key="python")
        newer = self.add("v2 用 3.13", key="python")

        resolution = resolve_for_key(self.store, OWNER, kind="project_fact", key="python")

        self.assertEqual(resolution.item.id, newer.id)

    def test_empty_group_returns_not_found(self):
        resolution = resolve_group(self.store, "nope", owner_id=OWNER)

        self.assertFalse(resolution.found)

    def test_group_name_is_required(self):
        with self.assertRaises(ResolutionError):
            resolve_group(self.store, "  ", owner_id=OWNER)

    def test_lineage_returns_old_to_new(self):
        first = self.add("初版", key="chain")
        second = self.add("二版", key="chain", supersedes_id=first.id)
        third = self.add("三版", key="chain", supersedes_id=second.id)

        chain = lineage(self.store, third.id)

        self.assertEqual([node.id for node in chain], [first.id, second.id, third.id])

    def test_lineage_detects_cycle(self):
        first = self.add("A", key="cycle")
        second = self.add("B", key="cycle", supersedes_id=first.id)

        connection = sqlite3.connect(self.db)
        self.addCleanup(connection.close)

        with connection:
            connection.execute(
                "UPDATE memory_items SET supersedes_id = ? WHERE id = ?",
                (second.id, first.id),
            )

        with self.assertRaises(ResolutionError):
            lineage(self.store, second.id)


if __name__ == "__main__":
    unittest.main()
