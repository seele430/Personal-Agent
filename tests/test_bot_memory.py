"""S3b bot_id 记忆隔离的测试：个人记忆与 Bot 独立记忆互不可见。"""

import unittest

from tempfile import TemporaryDirectory

from tests.helper import isolate_paths
from src.knowledge import KnowledgeStore, retrieve


class TestBotMemory(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        isolate_paths(self, self.temp)

        self.store = KnowledgeStore(self.temp.name + "/kb.db")

    def ingest(self, bot_id, text, title="doc"):
        return self.store.ingest_document(
            owner_id="alice",
            text=text,
            source_type="upload",
            title=title,
            bot_id=bot_id,
        )

    def test_personal_memory_only_visible_without_bot(self):
        self.ingest(None, "# 个人\n\n这是个人记忆，讲 asyncio。")

        # 个人记忆（bot_id=None）在默认检索时命中
        hits = retrieve(
            owner_id="alice", query="asyncio", store=self.store
        )["hits"]
        self.assertEqual(len(hits), 1)

        # 在 Bot 检索时不可见
        hits = retrieve(
            owner_id="alice", query="asyncio",
            store=self.store, bot_id="1",
        )["hits"]
        self.assertEqual(hits, [])

    def test_bot_memory_only_visible_to_that_bot(self):
        self.ingest("1", "# bot1\n\nbot1 的记忆，讲协程调度。")

        # bot1 检索命中
        hits = retrieve(
            owner_id="alice", query="协程调度",
            store=self.store, bot_id="1",
        )["hits"]
        self.assertEqual(len(hits), 1)

        # bot2 检索不到
        hits = retrieve(
            owner_id="alice", query="协程调度",
            store=self.store, bot_id="2",
        )["hits"]
        self.assertEqual(hits, [])

        # 个人检索（bot_id=None）也看不到
        hits = retrieve(
            owner_id="alice", query="协程调度", store=self.store
        )["hits"]
        self.assertEqual(hits, [])

    def test_memories_are_isolated_by_bot(self):
        self.ingest("1", "# b1\n\n内容甲，讲 asyncio。")
        self.ingest("2", "# b2\n\n内容乙，讲协程调度。")

        b1 = retrieve(
            owner_id="alice", query="asyncio",
            store=self.store, bot_id="1",
        )["hits"]
        b2 = retrieve(
            owner_id="alice", query="协程调度",
            store=self.store, bot_id="2",
        )["hits"]

        self.assertEqual(len(b1), 1)
        self.assertEqual(len(b2), 1)

        # 交叉检索命中 0
        self.assertEqual(
            retrieve(
                owner_id="alice", query="asyncio",
                store=self.store, bot_id="2",
            )["hits"],
            [],
        )
        self.assertEqual(
            retrieve(
                owner_id="alice", query="协程调度",
                store=self.store, bot_id="1",
            )["hits"],
            [],
        )


if __name__ == "__main__":
    unittest.main()
