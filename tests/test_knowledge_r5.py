"""知识库 R5（软过期 + 归档）的集成测试。

覆盖"能删、能过期、能重索引"里的后两半（删除已在 test_knowledge.py）：

1. **软过期**：文档可设过期时间，过期后检索不到，但记录仍在；
   清空过期时间即恢复可检索。
2. **归档**：同一 `source_ref` 的新内容进来，旧版本标记 `archived`
   并从检索候选剔除，但 `list_documents(include_archived=True)` 仍可见。

全部跑在临时数据库上，`isolate_paths` 保证不碰真实知识库。
"""

import unittest

from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory

from tests.helper import isolate_paths
from src.knowledge import (
    HashingEmbedder,
    KnowledgeStore,
    index_embeddings,
    ingest_monitor_item,
    retrieve,
)


def _past() -> str:
    return (
        datetime.now(timezone.utc) - timedelta(days=1)
    ).isoformat()


def _future() -> str:
    return (
        datetime.now(timezone.utc) + timedelta(days=1)
    ).isoformat()


class KnowledgeR5TestCase(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        isolate_paths(self, self.temp)

        self.store = KnowledgeStore(self.temp.name + "/kb.db")

    def ingest(self, owner="alice", item_id="item-1", body=None):
        return ingest_monitor_item(
            owner_id=owner,
            item_id=item_id,
            body=body or "# 标题\n\n这是正文，包含 asyncio 与协程。",
            title="标题",
            store=self.store,
        )


class TestSoftExpiry(KnowledgeR5TestCase):

    def test_expired_document_is_not_retrieved(self):
        document = self.ingest()

        self.store.set_expiry(
            owner_id="alice",
            document_id=document["id"],
            expires_at=_past(),
        )

        hits = retrieve(
            owner_id="alice", query="协程", store=self.store
        )["hits"]

        self.assertEqual(hits, [])

    def test_future_expiry_still_retrievable(self):
        document = self.ingest()

        self.store.set_expiry(
            owner_id="alice",
            document_id=document["id"],
            expires_at=_future(),
        )

        hits = retrieve(
            owner_id="alice", query="协程", store=self.store
        )["hits"]

        self.assertEqual(len(hits), 1)

    def test_no_expiry_means_never_expires(self):
        self.ingest()

        hits = retrieve(
            owner_id="alice", query="协程", store=self.store
        )["hits"]

        self.assertEqual(len(hits), 1)

    def test_clearing_expiry_restores_retrieval(self):
        document = self.ingest()

        self.store.set_expiry(
            owner_id="alice",
            document_id=document["id"],
            expires_at=_past(),
        )
        self.assertEqual(
            retrieve(
                owner_id="alice", query="协程", store=self.store
            )["hits"],
            [],
        )

        self.store.set_expiry(
            owner_id="alice",
            document_id=document["id"],
            expires_at=None,
        )
        self.assertEqual(
            len(
                retrieve(
                    owner_id="alice", query="协程", store=self.store
                )["hits"]
            ),
            1,
        )

    def test_set_expiry_on_other_owner_is_noop(self):
        document = self.ingest(owner="alice")

        self.assertFalse(
            self.store.set_expiry(
                owner_id="bob",
                document_id=document["id"],
                expires_at=_past(),
            )
        )

        # alice 的文档没被改动，仍可检索
        self.assertEqual(
            len(
                retrieve(
                    owner_id="alice", query="协程", store=self.store
                )["hits"]
            ),
            1,
        )

    def test_vector_search_also_filters_expired(self):
        document = self.ingest()
        embedder = HashingEmbedder()
        index_embeddings(
            owner_id="alice", store=self.store, embedder=embedder
        )

        hits = retrieve(
            owner_id="alice",
            query="协程",
            mode="vector",
            store=self.store,
            embedder=embedder,
        )["hits"]
        self.assertEqual(len(hits), 1)

        self.store.set_expiry(
            owner_id="alice",
            document_id=document["id"],
            expires_at=_past(),
        )

        hits = retrieve(
            owner_id="alice",
            query="协程",
            mode="vector",
            store=self.store,
            embedder=embedder,
        )["hits"]
        self.assertEqual(hits, [])


class TestArchive(KnowledgeR5TestCase):

    def test_new_version_archives_old(self):
        first = self.ingest(body="# v1\n\n第一版内容，讲 asyncio。")
        second = self.ingest(body="# v2\n\n第二版内容，讲协程调度。")

        self.assertTrue(first["created"])
        self.assertTrue(second["created"])
        self.assertNotEqual(first["id"], second["id"])

        # 默认列表只含活跃文档：只剩 v2
        active = self.store.list_documents("alice")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["id"], second["id"])

        # 带 archived 能看到两个
        all_docs = self.store.list_documents(
            "alice", include_archived=True
        )
        self.assertEqual(len(all_docs), 2)

    def test_archived_document_is_not_retrieved(self):
        self.ingest(body="# v1\n\n第一版，讲 asyncio。")
        self.ingest(body="# v2\n\n第二版，讲协程调度。")

        # v1 的独有词不应再命中（v1 已归档）
        hits = retrieve(
            owner_id="alice", query="asyncio", store=self.store
        )["hits"]
        self.assertEqual(hits, [])

        # v2 的独有词仍能命中
        hits = retrieve(
            owner_id="alice", query="协程调度", store=self.store
        )["hits"]
        self.assertEqual(len(hits), 1)

    def test_idempotent_ingest_does_not_archive(self):
        first = self.ingest(body="# v1\n\n同一份内容。")
        again = self.ingest(body="# v1\n\n同一份内容。")

        self.assertFalse(again["created"])
        self.assertEqual(first["id"], again["id"])

        # 幂等返回不该触发归档，仍只有 1 条
        self.assertEqual(
            len(
                self.store.list_documents(
                    "alice", include_archived=True
                )
            ),
            1,
        )

    def test_archive_is_scoped_to_source_ref(self):
        self.ingest(item_id="item-a", body="# A\n\n内容 A 讲 asyncio。")
        self.ingest(item_id="item-b", body="# B\n\n内容 B 讲协程。")

        # 不同 source_ref 互不归档，两条都在
        self.assertEqual(
            len(self.store.list_documents("alice")), 2
        )

    def test_archive_is_scoped_to_owner(self):
        self.ingest(owner="alice", body="# v1\n\n第一份内容。")
        self.ingest(owner="bob", body="# v2\n\n第二份内容。")

        # 不同 owner 的同一 source_ref 不应互相归档
        self.assertEqual(
            len(self.store.list_documents("alice")), 1
        )
        self.assertEqual(
            len(self.store.list_documents("bob")), 1
        )


if __name__ == "__main__":
    unittest.main()
