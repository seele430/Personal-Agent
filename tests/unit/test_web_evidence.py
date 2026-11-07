"""Evidence Store 单元测试。

每个用例一个临时目录里的 evidence.db，不碰项目真实库
（受保护运行器也会拦）。
"""

import tempfile
import threading
import unittest

from pathlib import Path

from src.web.evidence import (
    EvidenceError,
    EvidenceStore,
    get_evidence_store,
    normalize_url,
    reset_store,
)


TEXT_A = "PagedAttention stores the key-value cache in non-contiguous blocks."
TEXT_B = "vLLM is a serving engine that uses PagedAttention for high throughput."


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.store = EvidenceStore(Path(self._temporary.name) / "evidence.db")


class TestSourceRegistration(StoreTestCase):
    def test_source_is_stored_with_hash(self):
        record = self.store.add_source(
            url="https://arxiv.org/abs/2309.06180",
            final_url="https://arxiv.org/abs/2309.06180",
            text=TEXT_A,
            title="PagedAttention",
            method="trafilatura",
            byte_length=1234,
        )

        self.assertGreater(record.id, 0)
        self.assertEqual(record.char_count, len("".join(TEXT_A.split())))
        self.assertEqual(record.title, "PagedAttention")
        self.assertEqual(self.store.count_sources(), 1)
        self.assertEqual(self.store.get_text(record.id), TEXT_A)

    def test_same_url_and_text_is_deduplicated(self):
        first = self.store.add_source(url="https://a.org/x", text=TEXT_A)
        second = self.store.add_source(url="https://a.org/x", text=TEXT_A)

        self.assertEqual(first.id, second.id)
        self.assertEqual(self.store.count_sources(), 1)

    def test_same_text_from_different_url_is_a_second_evidence(self):
        first = self.store.add_source(url="https://a.org/x", text=TEXT_A)
        second = self.store.add_source(url="https://b.org/y", text=TEXT_A)

        self.assertNotEqual(first.id, second.id)
        self.assertEqual(self.store.count_sources(), 2)
        # 正文哈希相同，说明是同一份内容
        self.assertEqual(first.text_sha256, second.text_sha256)
        self.assertEqual(
            self.store.find_by_text_sha(first.text_sha256).id, first.id
        )

    def test_empty_text_is_rejected(self):
        for text in ("", "   \n", None):
            with self.subTest(text=text):
                with self.assertRaises(EvidenceError):
                    self.store.add_source(url="https://a.org/x", text=text)

    def test_invalid_status_is_rejected(self):
        with self.assertRaises(EvidenceError):
            self.store.add_source(url="https://a.org/x", text=TEXT_A, http_status=0)

    def test_unknown_source_raises_instead_of_returning_empty(self):
        with self.assertRaises(EvidenceError):
            self.store.get_text(9999)

        self.assertIsNone(self.store.get_source(9999))


class TestUrlNormalization(unittest.TestCase):
    def test_fragment_and_default_port_are_removed(self):
        self.assertEqual(
            normalize_url("HTTPS://Example.ORG:443/a/b?q=1#frag"),
            "https://example.org/a/b?q=1",
        )

    def test_query_string_is_kept(self):
        self.assertEqual(
            normalize_url("https://e.org/search?q=a&p=2"),
            "https://e.org/search?q=a&p=2",
        )

    def test_credentials_and_junk_are_rejected(self):
        for bad in (
            "https://user:pass@e.org/x",
            "javascript:alert(1)",
            "",
            "not a url",
        ):
            with self.subTest(url=bad):
                with self.assertRaises(ValueError):
                    normalize_url(bad)


class TestCitations(StoreTestCase):
    def test_citation_binds_to_existing_evidence(self):
        record = self.store.add_source(url="https://a.org/x", text=TEXT_A)

        citation = self.store.add_citation(
            report_key="r1",
            claim_key="c1",
            source_id=record.id,
            quote="non-contiguous blocks",
        )

        self.assertEqual(citation.source_id, record.id)
        self.assertEqual(len(self.store.citations_for("r1")), 1)

    def test_citation_to_missing_evidence_fails(self):
        with self.assertRaises(EvidenceError):
            self.store.add_citation(
                report_key="r1", claim_key="c1", source_id=424242
            )

    def test_duplicate_citation_is_idempotent(self):
        record = self.store.add_source(url="https://a.org/x", text=TEXT_A)

        first = self.store.add_citation(
            report_key="r1", claim_key="c1", source_id=record.id
        )
        second = self.store.add_citation(
            report_key="r1", claim_key="c1", source_id=record.id
        )

        self.assertEqual(first.id, second.id)
        self.assertEqual(len(self.store.citations_for("r1")), 1)


class TestClearAndConcurrency(StoreTestCase):
    def test_clear_removes_evidence_and_citations(self):
        record = self.store.add_source(url="https://a.org/x", text=TEXT_A)
        self.store.add_citation(
            report_key="r1", claim_key="c1", source_id=record.id
        )

        removed = self.store.clear()

        self.assertEqual(removed, 1)
        self.assertEqual(self.store.count_sources(), 0)
        self.assertEqual(self.store.citations_for("r1"), [])

    def test_concurrent_writes_do_not_collide(self):
        errors = []

        def worker(index):
            try:
                self.store.add_source(
                    url=f"https://e.org/{index}",
                    text=f"正文 {index} " * 20,
                )

            except Exception as error:  # noqa: BLE001
                errors.append(error)

        threads = [
            threading.Thread(target=worker, args=(index,))
            for index in range(8)
        ]

        for thread in threads:
            thread.start()

        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(self.store.count_sources(), 8)


class TestStoreLocation(unittest.TestCase):
    """单例必须跟着 config.paths 走，避免测试写进真实库。"""

    def test_singleton_follows_configured_data_dir(self):
        from tests.helper import isolated_paths

        with tempfile.TemporaryDirectory() as first:
            with isolated_paths(first):
                reset_store()
                store = get_evidence_store()
                store.add_source(url="https://a.org/x", text=TEXT_A)

                self.assertEqual(store.count_sources(), 1)
                self.assertTrue(
                    str(store.db_path).startswith(str(Path(first)))
                )

        with tempfile.TemporaryDirectory() as second:
            with isolated_paths(second):
                reset_store()
                fresh = get_evidence_store()

                self.assertEqual(fresh.count_sources(), 0)
                self.assertIn("evidence.db", fresh.db_path.name)

        reset_store()


if __name__ == "__main__":
    unittest.main()
