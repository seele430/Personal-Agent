"""知识库（R1 摄取/索引 + R2 BM25 检索）的单元测试。

全部跑在临时数据库上：`isolate_paths` 保证连默认构造的 store
也落在临时目录，不会碰到真实知识库。

重点覆盖四类容易出事的地方：

1. **幂等**：同一份内容重复摄取不能产生第二篇文档；
2. **隔离**：检索与计数都必须按 owner 收窄，不能串；
3. **一致性**：删除文档后，片段与 FTS 索引不能留悬挂；
4. **中文与短查询**：trigram 需要 ≥3 字符，1–2 字符必须走
   逐词 AND 的字面量兜底，而不是"整串 LIKE"（那会 0 命中）。
"""

import sqlite3
import unittest

from contextlib import closing
from tempfile import TemporaryDirectory

from tests.helper import isolate_paths
from src.knowledge import (
    KnowledgeStore,
    chunk_text,
    ingest_monitor_item,
    ingest_summary,
    ingest_upload,
    retrieve,
)
from src.knowledge.store import content_hash


class KnowledgeTestCase(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        # 既隔离全局路径，也用显式路径构造（双保险）
        isolate_paths(self, self.temp)

        self.store = KnowledgeStore(
            self.temp.name + "/kb.db"
        )

    def ingest(self, owner="alice", name="a", text=None):
        return ingest_upload(
            owner_id=owner,
            filename=name,
            text=text or "# 标题\n\n这是正文内容，包含 asyncio 与协程。",
            store=self.store,
        )


class TestChunker(KnowledgeTestCase):

    def test_short_blocks_are_merged(self):
        chunks = chunk_text("# 一\n\n短\n\n# 二\n\n也不长")

        # 太碎的片段会被合并，避免检索命中半句话
        self.assertEqual(len(chunks), 1)

    def test_long_block_is_windowed(self):
        chunks = chunk_text("啊" * 1500)

        self.assertGreater(len(chunks), 1)

        for chunk in chunks:
            self.assertLessEqual(len(chunk), 600)

    def test_blank_text_yields_no_chunks(self):
        self.assertEqual(chunk_text("   \n\n  "), [])


class TestIngest(KnowledgeTestCase):

    def test_same_content_is_idempotent(self):
        first = self.ingest()
        second = self.ingest(name="另一个文件名")

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["chunks"], 0)

        self.assertEqual(
            len(self.store.list_documents("alice")), 1
        )

    def test_different_content_creates_new_document(self):
        self.ingest()
        self.ingest(name="b", text="# 别的\n\n完全不同的内容。")

        self.assertEqual(
            len(self.store.list_documents("alice")), 2
        )

    def test_hash_ignores_line_ending_and_spacing(self):
        a = content_hash("第一行\r\n\r\n第二行  结尾  ")
        b = content_hash("第一行\n\n第二行 结尾")

        self.assertEqual(a, b)

    def test_empty_content_is_rejected(self):
        with self.assertRaises(ValueError):
            self.ingest(text="   ")

    def test_unknown_source_type_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.ingest_document(
                owner_id="alice",
                text="内容",
                source_type="ghost",
            )

    def test_monitor_and_summary_entries(self):
        monitor = ingest_monitor_item(
            owner_id="alice",
            item_id="item-1",
            body="Python 3.15 发布了，包含新的解释器优化。",
            title="Python 3.15",
            url="https://example.com/a",
            store=self.store,
        )
        summary = ingest_summary(
            owner_id="alice",
            request_id="req_0001",
            text="用户问了三件事：提醒、额度、监控。",
            store=self.store,
        )

        self.assertTrue(monitor["created"])
        self.assertTrue(summary["created"])

        rows = self.store.list_documents("alice")

        self.assertEqual(
            sorted(row["source_type"] for row in rows),
            ["monitor", "summary"],
        )


class TestIsolation(KnowledgeTestCase):

    def test_search_never_crosses_owners(self):
        self.ingest(
            owner="alice",
            text="# 私有\n\nalice 的量子计算实验记录。",
        )
        self.ingest(
            owner="bob",
            text="# 私有\n\nbob 的渲染管线笔记。",
        )

        bob_hits = retrieve(
            owner_id="bob",
            query="量子计算",
            store=self.store,
        )["hits"]

        alice_hits = retrieve(
            owner_id="alice",
            query="渲染管线",
            store=self.store,
        )["hits"]

        self.assertEqual(bob_hits, [])
        self.assertEqual(alice_hits, [])

    def test_counts_are_per_owner(self):
        self.ingest(owner="alice")
        self.ingest(owner="bob")

        self.assertEqual(self.store.count_chunks("alice"), 1)
        self.assertEqual(self.store.count_chunks("bob"), 1)
        self.assertEqual(self.store.count_chunks(), 2)

    def test_cannot_delete_other_owners_document(self):
        document = self.ingest(owner="alice")

        self.assertFalse(
            self.store.delete_document(
                owner_id="bob",
                document_id=document["id"],
            )
        )

        self.assertEqual(self.store.count_chunks("alice"), 1)


class TestSearch(KnowledgeTestCase):

    def setUp(self):
        super().setUp()

        self.ingest(
            text=(
                "# 性能分析\n\n"
                "用 cProfile 分析 asyncio 服务的瓶颈与协程调度，"
                "注意事件循环的阻塞点。"
            )
        )

    def test_long_query_uses_fts(self):
        outcome = retrieve(
            owner_id="alice",
            query="事件循环",
            store=self.store,
        )

        self.assertEqual(outcome["mode"], "bm25")
        # 实际走的通道在 channels 里（bm25 通道如实标注 fts/like）
        self.assertEqual(outcome["channels"]["bm25"], "fts")
        self.assertEqual(len(outcome["hits"]), 1)
        self.assertEqual(outcome["hits"][0]["rank"], 1)

    def test_two_char_query_falls_back_to_literal(self):
        outcome = retrieve(
            owner_id="alice",
            query="协程",
            store=self.store,
        )

        # trigram 至少要 3 个字符，2 字必须走兜底
        self.assertEqual(outcome["channels"]["bm25"], "like")
        self.assertEqual(len(outcome["hits"]), 1)

    def test_mixed_query_keeps_short_term(self):
        """含 2 字中文词的混合查询不能因为兜底写法而 0 命中。"""

        outcome = retrieve(
            owner_id="alice",
            query="asyncio 瓶颈",
            store=self.store,
        )

        self.assertEqual(outcome["channels"]["bm25"], "like")
        self.assertEqual(len(outcome["hits"]), 1)

    def test_terms_are_anded(self):
        outcome = retrieve(
            owner_id="alice",
            query="cProfile 不存在词",
            store=self.store,
        )

        self.assertEqual(outcome["hits"], [])

    def test_fts_special_characters_do_not_break(self):
        for query in ('*', '(', '"', 'AND', 'a OR b'):
            with self.subTest(query=query):
                outcome = retrieve(
                    owner_id="alice",
                    query=query,
                    store=self.store,
                )

                self.assertIn(
                    outcome["channels"]["bm25"], ("fts", "like")
                )

    def test_empty_query_is_rejected(self):
        with self.assertRaises(ValueError):
            retrieve(
                owner_id="alice",
                query="   ",
                store=self.store,
            )

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            retrieve(
                owner_id="alice",
                query="事件循环",
                mode="ghost",
                store=self.store,
            )


class TestAuditAndCascade(KnowledgeTestCase):

    def test_retrieval_is_audited_without_plaintext(self):
        self.ingest()

        retrieve(
            owner_id="alice",
            query="协程",
            store=self.store,
        )

        with closing(
            sqlite3.connect(self.store.db_path)
        ) as conn:
            conn.row_factory = sqlite3.Row

            row = conn.execute(
                """
                SELECT user_id, mode, k, latency_ms,
                       top_chunk_ids, query_sha256
                FROM knowledge_retrievals
                """
            ).fetchone()

        self.assertEqual(row["user_id"], "alice")
        self.assertEqual(row["k"], 5)
        self.assertGreaterEqual(row["latency_ms"], 0)
        self.assertTrue(row["top_chunk_ids"])
        # query 只存哈希，不落明文
        self.assertEqual(len(row["query_sha256"]), 64)
        self.assertNotIn("协程", row["query_sha256"])

    def test_delete_removes_chunks_and_index(self):
        document = self.ingest()

        self.assertEqual(self.store.count_chunks(), 1)

        self.assertTrue(
            self.store.delete_document(
                owner_id="alice",
                document_id=document["id"],
            )
        )

        self.assertEqual(self.store.count_chunks(), 0)

        # 索引里不能留悬挂：删完再检索不能再命中
        outcome = retrieve(
            owner_id="alice",
            query="协程",
            store=self.store,
        )

        self.assertEqual(outcome["hits"], [])

        with closing(
            sqlite3.connect(self.store.db_path)
        ) as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM knowledge_chunks_fts"
            ).fetchone()[0]

        self.assertEqual(remaining, 0)


if __name__ == "__main__":
    unittest.main()
