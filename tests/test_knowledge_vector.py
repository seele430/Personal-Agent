"""向量与融合（R3）的测试。

全部离线：用确定性的 `HashingEmbedder` 和一个可预期的 `FakeEmbedder`，
不联网、不花钱。

重点覆盖四类容易出事的地方：

1. **确定性**：内置 hash() 会按 PYTHONHASHSEED 随机化，用它做"确定性向量"
   会让缓存与评测不可复现 —— 所以这里断言两次构造得到完全相同的向量；
2. **只算一次**：`index_embeddings` 重复调用不能重复调用 embedder，
   同一段文本跨用户也要命中内容缓存（但检索仍然隔离）；
3. **隔离下推**：向量检索必须先按 owner 收窄候选，不能先取全局 top-k；
4. **RRF 是名次融合**：分数必须等于 Σ 1/(60+rank)，且两个通道都认可的
   文档要排在只有一个通道认可的前面。
"""

import sqlite3
import unittest

from contextlib import closing
from tempfile import TemporaryDirectory

from tests.helper import isolate_paths
from src.knowledge import (
    HashingEmbedder,
    KnowledgeStore,
    RRF_K,
    cosine,
    fuse_rrf,
    index_embeddings,
    ingest_upload,
    retrieve,
)
from src.knowledge.embed import (
    Embedder,
    blob_vector,
    normalize_vector,
    vector_blob,
)


TEXT_A = "# 性能\n\n用 cProfile 分析 asyncio 的瓶颈与协程调度。"
TEXT_B = "# 渲染\n\n渲染管线笔记，与向量检索无关。"


class FakeEmbedder(Embedder):
    """可预期的 3 维向量：按关键词给权重，方便断言排序。

    同时记录每次 embed 的输入，用来断言"没有重复计算"。
    """

    name = "fake-3"
    dim = 3

    def __init__(self):
        self.calls = []

    def embed(self, texts):
        texts = list(texts)
        self.calls.append(texts)

        return [self._vector(text) for text in texts]

    @staticmethod
    def _vector(text):
        vector = [0.0, 0.0, 0.0]

        if "asyncio" in text:
            vector[0] = 1.0
        if "瓶颈" in text:
            vector[1] = 1.0
        if "渲染" in text:
            vector[2] = 1.0

        if not any(vector):
            vector[0] = 0.1

        return vector


class KnowledgeVectorCase(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        isolate_paths(self, self.temp)

        self.store = KnowledgeStore(
            self.temp.name + "/kb.db"
        )

    def ingest(self, owner="alice", text=TEXT_A, name="a"):
        return ingest_upload(
            owner_id=owner,
            filename=name,
            text=text,
            store=self.store,
        )

    def index(self, owner="alice", embedder=None):
        return index_embeddings(
            owner_id=owner,
            store=self.store,
            embedder=embedder or FakeEmbedder(),
        )


class TestHashingEmbedder(unittest.TestCase):
    """确定性词法向量。"""

    def test_same_text_gives_identical_vector(self):
        first = HashingEmbedder().embed_one(TEXT_A)
        second = HashingEmbedder().embed_one(TEXT_A)

        # 跨实例一致：内置 hash() 做不到这一点
        self.assertEqual(first, second)

    def test_vector_is_l2_normalized(self):
        vector = HashingEmbedder().embed_one(TEXT_A)

        norm = sum(value * value for value in vector) ** 0.5

        self.assertAlmostEqual(norm, 1.0, places=6)

    def test_dimension_and_name(self):
        embedder = HashingEmbedder(dim=64, ngram=3)

        self.assertEqual(embedder.dim, 64)
        self.assertEqual(embedder.name, "hashing-3gram-64")
        self.assertEqual(len(embedder.embed_one("甲乙丙丁")), 64)

    def test_different_text_gives_different_vector(self):
        self.assertNotEqual(
            HashingEmbedder().embed_one(TEXT_A),
            HashingEmbedder().embed_one(TEXT_B),
        )

    def test_empty_text_is_safe(self):
        vector = HashingEmbedder().embed_one("")

        self.assertEqual(len(vector), HashingEmbedder().dim)
        self.assertEqual(sum(vector), 0.0)

    def test_invalid_parameters_rejected(self):
        for kwargs in ({"dim": 2}, {"dim": 8, "ngram": 0}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    HashingEmbedder(**kwargs)


class TestVectorMath(unittest.TestCase):

    def test_blob_round_trip(self):
        vector = [0.5, -0.25, 1.0]

        restored = blob_vector(vector_blob(vector))

        self.assertEqual(len(restored), 3)

        for expected, actual in zip(vector, restored):
            self.assertAlmostEqual(expected, actual, places=6)

    def test_cosine_basics(self):
        self.assertAlmostEqual(
            cosine([1.0, 0.0], [1.0, 0.0]), 1.0
        )
        self.assertAlmostEqual(
            cosine([1.0, 0.0], [0.0, 1.0]), 0.0
        )
        self.assertAlmostEqual(
            cosine([1.0, 0.0], [-1.0, 0.0]), -1.0
        )
        self.assertEqual(cosine([0.0, 0.0], [1.0, 0.0]), 0.0)

    def test_cosine_rejects_dimension_mismatch(self):
        with self.assertRaises(ValueError):
            cosine([1.0], [1.0, 2.0])

    def test_normalize_keeps_zero_vector(self):
        self.assertEqual(normalize_vector([0.0, 0.0]), [0.0, 0.0])


class TestIndexing(KnowledgeVectorCase):
    """建索引：只补缺 + 内容级缓存。"""

    def test_embeds_each_unique_text_once(self):
        self.ingest(name="a")
        self.ingest(name="b", text=TEXT_B)

        embedder = FakeEmbedder()

        result = index_embeddings(
            owner_id="alice",
            store=self.store,
            embedder=embedder,
        )

        self.assertEqual(result["embedded"], 2)
        self.assertEqual(result["chunks"], 2)
        self.assertEqual(len(embedder.calls), 1)
        self.assertEqual(
            self.store.count_embeddings(
                owner_id="alice", model="fake-3"
            ),
            2,
        )

    def test_second_run_is_free(self):
        self.ingest()
        index_embeddings(
            owner_id="alice",
            store=self.store,
            embedder=FakeEmbedder(),
        )

        embedder = FakeEmbedder()

        result = index_embeddings(
            owner_id="alice",
            store=self.store,
            embedder=embedder,
        )

        self.assertEqual(result["embedded"], 0)
        self.assertEqual(result["chunks"], 0)
        self.assertEqual(embedder.calls, [])

    def test_identical_content_across_owners_hits_cache(self):
        self.ingest(owner="alice", name="a")

        alice = FakeEmbedder()
        index_embeddings(
            owner_id="alice",
            store=self.store,
            embedder=alice,
        )
        # embedder 拿到的是**片段文本**（分块后标题标记已被切掉）
        self.assertEqual(len(alice.calls), 1)
        self.assertIn("asyncio", alice.calls[0][0])

        # bob 摄取同一段文本：内容缓存命中，不再算一次
        self.ingest(owner="bob", name="b", text=TEXT_A)

        bob = FakeEmbedder()
        result = index_embeddings(
            owner_id="bob",
            store=self.store,
            embedder=bob,
        )

        self.assertEqual(result["embedded"], 0)
        self.assertEqual(result["cached"], 1)
        self.assertEqual(bob.calls, [])

        # 但检索仍然各看各的
        self.assertTrue(
            self.store.count_embeddings(
                owner_id="bob", model="fake-3"
            )
        )

    def test_only_missing_chunks_are_returned(self):
        self.ingest(owner="alice", name="a")
        self.ingest(owner="bob", name="b", text=TEXT_B)

        pending = self.store.chunks_missing_embedding(
            owner_id="bob", model="fake-3"
        )

        self.assertEqual(len(pending), 1)
        self.assertIn("渲染", pending[0]["text"])


class TestVectorSearch(KnowledgeVectorCase):

    def test_nearest_chunk_wins(self):
        self.ingest(name="a", text=TEXT_A)
        self.ingest(name="b", text=TEXT_B)
        self.index()

        outcome = retrieve(
            owner_id="alice",
            query="asyncio 瓶颈",
            mode="vector",
            store=self.store,
            embedder=FakeEmbedder(),
        )

        self.assertEqual(outcome["mode"], "vector")
        self.assertEqual(outcome["hits"][0]["rank"], 1)
        self.assertIn("asyncio", outcome["hits"][0]["text"])
        self.assertGreater(
            outcome["hits"][0]["score"],
            outcome["hits"][1]["score"],
        )

    def test_vectors_never_cross_owners(self):
        self.ingest(owner="alice", name="a", text=TEXT_A)
        self.ingest(owner="bob", name="b", text=TEXT_B)

        index_embeddings(
            owner_id="alice",
            store=self.store,
            embedder=FakeEmbedder(),
        )
        index_embeddings(
            owner_id="bob",
            store=self.store,
            embedder=FakeEmbedder(),
        )

        # bob 用 alice 的关键词检索：alice 的向量不该出现
        outcome = retrieve(
            owner_id="bob",
            query="asyncio 瓶颈",
            mode="vector",
            store=self.store,
            embedder=FakeEmbedder(),
        )

        for hit in outcome["hits"]:
            self.assertIn("渲染", hit["text"])

    def test_empty_index_returns_nothing(self):
        self.ingest()

        outcome = retrieve(
            owner_id="alice",
            query="asyncio 瓶颈",
            mode="vector",
            store=self.store,
            embedder=FakeEmbedder(),
        )

        self.assertEqual(outcome["hits"], [])

    def test_dimension_mismatch_rows_are_skipped(self):
        self.ingest()
        self.index()

        blob = vector_blob([0.1] * 9)

        with closing(
            sqlite3.connect(self.store.db_path)
        ) as conn:
            conn.execute(
                """
                UPDATE knowledge_embeddings
                SET dim = 9, vector = ?
                """,
                (blob,),
            )
            conn.commit()

        outcome = retrieve(
            owner_id="alice",
            query="asyncio 瓶颈",
            mode="vector",
            store=self.store,
            embedder=FakeEmbedder(),
        )

        # 维度对不上就跳过，而不是抛异常（模型换过、写入半途失败都可能）
        self.assertEqual(outcome["hits"], [])

    def test_deleting_document_cascades_to_vectors(self):
        document = self.ingest()
        self.index()

        self.assertEqual(self.store.count_embeddings(), 1)

        self.store.delete_document(
            owner_id="alice",
            document_id=document["id"],
        )

        self.assertEqual(self.store.count_embeddings(), 0)


class TestHybrid(KnowledgeVectorCase):

    def test_rrf_uses_ranks_not_scores(self):
        both = {"chunk_id": 1, "text": "两个通道都有"}
        one = {"chunk_id": 2, "text": "只有一个通道"}

        fused = fuse_rrf(
            ("bm25", [both, one]),
            ("vector", [both]),
        )

        self.assertEqual([hit["chunk_id"] for hit in fused], [1, 2])

        expected = (
            1.0 / (RRF_K + 1) + 1.0 / (RRF_K + 1)
        )

        self.assertAlmostEqual(fused[0]["score"], expected)
        self.assertAlmostEqual(
            fused[1]["score"], 1.0 / (RRF_K + 2)
        )
        self.assertEqual(fused[0]["channels"], ["bm25", "vector"])
        self.assertEqual(fused[1]["channels"], ["bm25"])

    def test_rrf_leaves_no_hit_behind(self):
        fused = fuse_rrf(
            ("bm25", [{"chunk_id": 1, "text": "a"}]),
            ("vector", [{"chunk_id": 2, "text": "b"}]),
        )

        self.assertEqual(
            sorted(hit["chunk_id"] for hit in fused),
            [1, 2],
        )
        self.assertEqual(
            [hit["rank"] for hit in fused], [1, 2]
        )

    def test_hybrid_keeps_channel_provenance(self):
        self.ingest(name="a", text=TEXT_A)
        self.ingest(name="b", text=TEXT_B)
        self.index()

        outcome = retrieve(
            owner_id="alice",
            query="asyncio 瓶颈",
            mode="hybrid",
            store=self.store,
            embedder=FakeEmbedder(),
        )

        self.assertEqual(outcome["mode"], "hybrid")
        self.assertEqual(outcome["channels"]["vector"], "fake-3")
        self.assertIn("bm25", outcome["channels"])

        first = outcome["hits"][0]

        self.assertIn("bm25", first["channels"])
        self.assertIn("vector", first["channels"])

    def test_hybrid_is_audited_with_its_own_mode(self):
        self.ingest()
        self.index()

        retrieve(
            owner_id="alice",
            query="asyncio 瓶颈",
            mode="hybrid",
            store=self.store,
            embedder=FakeEmbedder(),
            request_id="req_hybrid0001",
        )

        audit = self.store.list_retrievals("alice")[0]

        self.assertEqual(audit["mode"], "hybrid")
        self.assertTrue(audit["top_chunk_ids"])
        self.assertEqual(
            audit["request_id"], "req_hybrid0001"
        )

    def test_hybrid_survives_an_empty_vector_channel(self):
        self.ingest()

        # 没有建向量索引：向量通道为空，RRF 必须退回 BM25 的结果
        outcome = retrieve(
            owner_id="alice",
            query="asyncio 瓶颈",
            mode="hybrid",
            store=self.store,
            embedder=FakeEmbedder(),
        )

        self.assertTrue(outcome["hits"])
        self.assertEqual(
            outcome["hits"][0]["channels"], ["bm25"]
        )


class TestBuildEmbedder(unittest.TestCase):
    """配置挑选逻辑（不联网）。"""

    def test_default_is_hashing(self):
        from src.knowledge.embed import build_embedder

        embedder = build_embedder(environ={})

        self.assertEqual(embedder.name, "hashing-2gram-256")

    def test_key_switches_to_openai_compatible(self):
        from src.knowledge.embed import (
            build_embedder,
            OpenAICompatibleEmbedder,
        )

        embedder = build_embedder(
            environ={
                "EMBEDDING_API_KEY": "k",
                "EMBEDDING_MODEL": "BAAI/bge-m3",
                "EMBEDDING_BASE_URL": "https://example.invalid/v1",
            }
        )

        self.assertIsInstance(
            embedder, OpenAICompatibleEmbedder
        )
        self.assertEqual(embedder.name, "openai:BAAI/bge-m3")

    def test_unknown_provider_is_rejected(self):
        from src.knowledge.embed import build_embedder

        with self.assertRaises(ValueError):
            build_embedder(
                "ghost", environ={}
            )


if __name__ == "__main__":
    unittest.main()
