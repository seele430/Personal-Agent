"""通用任务引擎的 Source → Condition → Action 语义。

对应 DEVELOPMENT_PLAN 里 M2 的验收点：

- 首次检查只建立基线，不通知旧条目；
- 新增条目**恰好通知一次**（重复执行不重复通知）；
- 状态全部落在 SQLite：重来一遍不会重放已通知的条目；
- 数据源可插拔：注册表决定调度器跑什么。
"""

import sqlite3
import unittest

from contextlib import closing
from unittest.mock import patch

from tests.helper import build_temp_paths, isolated_paths
from src.core import source_registry
from src.core import monitor_registry
from src.core.source_registry import (
    UnknownSourceType,
    known_source_types,
    run_source,
)
from src.core.task_engine import (
    detect_new_items,
    init_task_db,
)
from src.monitors import http_text


class PipelineTestCase(unittest.TestCase):
    """公共装置：每个用例一个临时任务库。"""

    def setUp(self):
        self.db_path = None

        from tempfile import TemporaryDirectory

        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        self.db_path = build_temp_paths(self.temp).tasks_db

        isolated = isolated_paths(self.temp)
        isolated.__enter__()
        self.addCleanup(
            isolated.__exit__, None, None, None
        )

    def outbox(self):
        with closing(
            sqlite3.connect(self.db_path)
        ) as conn:
            conn.row_factory = sqlite3.Row

            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM notification_outbox"
                    " ORDER BY id"
                )
            ]

    def count(self, table, monitor_id):
        with closing(
            sqlite3.connect(self.db_path)
        ) as conn:
            return conn.execute(
                f"SELECT COUNT(*) FROM {table}"
                " WHERE monitor_id = ?",
                (monitor_id,),
            ).fetchone()[0]


class TestSourceRegistry(unittest.TestCase):
    """数据源注册表。"""

    def test_known_types_are_registered(self):
        types = known_source_types()

        self.assertIn("rss", types)
        self.assertIn("http_text", types)
        self.assertIn(
            source_registry.DEFAULT_SOURCE_TYPE,
            types,
        )

    def test_unknown_type_is_rejected(self):
        with self.assertRaises(UnknownSourceType):
            run_source(
                source_type="ghost",
                monitor_id="m1",
                source_url="https://example.com/",
            )

    def test_dispatch_uses_the_registered_handler(self):
        seen = []

        def fake_handler(monitor_id, source_url):
            seen.append((monitor_id, source_url))
            return 7

        with patch.dict(
            source_registry.SOURCE_HANDLERS,
            {"rss": fake_handler},
        ):
            result = run_source(
                source_type="rss",
                monitor_id="m1",
                source_url="https://example.com/",
            )

        self.assertEqual(result, 7)
        self.assertEqual(
            seen,
            [("m1", "https://example.com/")],
        )


class TestPipelineSemantics(PipelineTestCase):
    """基线 / 恰好一次 / 重启后不重放。"""

    def test_first_run_only_builds_baseline(self):
        init_task_db()

        new_items = detect_new_items(
            "m1",
            [
                {"id": "a", "title": "A"},
                {"id": "b", "title": "B"},
            ],
        )

        # 首次只建立基线：老条目一条都不通知
        self.assertEqual(new_items, [])
        self.assertEqual(self.outbox(), [])
        self.assertEqual(self.count("seen_items", "m1"), 2)
        self.assertEqual(self.count("monitor_state", "m1"), 1)

    def test_new_item_notifies_exactly_once(self):
        init_task_db()

        detect_new_items("m1", [{"id": "a", "title": "A"}])

        new_items = detect_new_items(
            "m1",
            [
                {"id": "a", "title": "A"},
                {"id": "b", "title": "B"},
            ],
        )

        self.assertEqual(len(new_items), 1)

        rows = self.outbox()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["item_id"], "b")
        self.assertEqual(rows[0]["status"], "pending")

        # 再扫一遍：已通知过的条目不会被重复通知
        again = detect_new_items(
            "m1",
            [
                {"id": "a", "title": "A"},
                {"id": "b", "title": "B"},
            ],
        )

        self.assertEqual(again, [])
        self.assertEqual(len(self.outbox()), 1)

    def test_state_lives_in_sqlite_so_restart_does_not_replay(
        self,
    ):
        init_task_db()

        detect_new_items("m1", [{"id": "a"}])
        detect_new_items(
            "m1", [{"id": "a"}, {"id": "b"}]
        )

        self.assertEqual(len(self.outbox()), 1)

        # "重启"后同一份扫描：去重依据全在 SQLite
        # （seen_items 记录已见条目），没有进程内缓存，
        # 所以不会重放已通知的条目。
        self.assertEqual(
            detect_new_items(
                "m1", [{"id": "a"}, {"id": "b"}]
            ),
            [],
        )

        self.assertEqual(len(self.outbox()), 1)
        self.assertEqual(self.count("seen_items", "m1"), 2)

    def test_two_monitors_do_not_share_baseline(self):
        init_task_db()

        detect_new_items("m1", [{"id": "a"}])
        detect_new_items("m2", [{"id": "a"}])

        # 第二个任务的首次运行同样是基线，不通知
        self.assertEqual(self.outbox(), [])


class TestHttpTextSource(PipelineTestCase):
    """第二个数据源的语义：内容变了才通知，且只通知一次。"""

    def test_content_change_notifies_once(self):
        bodies = [b"<html>hello</html>"]

        def fake_fetch(url, **kwargs):
            return bodies[-1]

        def scan():
            with patch.object(
                http_text, "fetch_bytes", fake_fetch
            ):
                return http_text.run_http_text_monitor(
                    "m_text", "https://example.com/"
                )

        # 第一次：建立基线
        self.assertEqual(scan(), 0)
        self.assertEqual(self.outbox(), [])

        # 内容变化：恰好一条通知
        bodies.append(b"<html>hello world</html>")
        self.assertEqual(scan(), 1)
        self.assertEqual(len(self.outbox()), 1)

        # 内容没变（只是重新抓了一次）：不通知
        self.assertEqual(scan(), 0)
        self.assertEqual(len(self.outbox()), 1)

        # 再次变化：再来一条
        bodies.append(b"<html>hello again</html>")
        self.assertEqual(scan(), 1)
        self.assertEqual(len(self.outbox()), 2)

    def test_whitespace_only_change_is_not_new(self):
        self.assertEqual(
            http_text.normalize_text(b"a\n\n b\t c "),
            "a b c",
        )

    def test_task_can_be_created_with_second_source_type(
        self,
    ):
        task_id = monitor_registry.create_monitor_task(
            "owner_001",
            "https://example.com/",
            30,
            "http_text",
        )

        self.assertTrue(
            task_id.startswith("http_text_")
        )

        tasks = monitor_registry.list_monitor_tasks(
            "owner_001"
        )

        self.assertEqual(tasks[0]["task_type"], "http_text")

    def test_unknown_source_type_is_rejected_on_create(
        self,
    ):
        with self.assertRaises(UnknownSourceType):
            monitor_registry.create_monitor_task(
                "owner_001",
                "https://example.com/",
                30,
                "ghost",
            )


if __name__ == "__main__":
    unittest.main()
