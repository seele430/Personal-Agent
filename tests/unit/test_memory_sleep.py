"""Sleep-time Compute Phase 1 测试。"""

import importlib.util
import json
import tempfile
import unittest

from pathlib import Path

from src.memory import sleep
from src.memory.store import MemoryStore
from src.memory.versions import MemoryVersions


AS_OF = "2026-12-01T00:00:00Z"
OWNER = "owner-sleep"


class SleepCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.store = MemoryStore(self.root / "memory.db")
        self.versions = MemoryVersions(self.store)

    def add(self, text="fact", **kwargs):
        params = {
            "kind": "project_fact",
            "layer": "project",
            "trust": "user_confirmed",
            "key": "k",
            "text": text,
        }
        params.update(kwargs)

        return self.store.add_item(OWNER, **params)

    def snapshot(self):
        return self.versions.create("test-base")

    def plan(self):
        snapshot = self.snapshot()

        return sleep.propose(self.store, OWNER, as_of=AS_OF, snapshot=snapshot), snapshot

    def test_exact_duplicate_keeps_newest(self):
        first = self.add("same")
        second = self.add("same")
        candidate, _ = self.plan()
        ops = [op for op in candidate["ops"] if op["reason"] == "exact_dedupe"]

        self.assertEqual([op["item_id"] for op in ops], [first.id])
        self.assertEqual(ops[0]["evidence_ids"], [second.id])

    def test_same_text_different_trust_not_deduped(self):
        self.add("same", trust="user_confirmed")
        self.add("same", trust="model_inferred")
        candidate, _ = self.plan()
        self.assertEqual([op for op in candidate["ops"] if op["reason"] == "exact_dedupe"], [])

    def test_same_text_different_source_not_deduped(self):
        self.add("same", source_ref="a")
        self.add("same", source_ref="b")
        candidate, _ = self.plan()
        self.assertEqual([op for op in candidate["ops"] if op["reason"] == "exact_dedupe"], [])

    def test_same_text_different_validity_not_deduped(self):
        self.add("same", valid_from="2026-01-01T00:00:00Z")
        self.add("same", valid_from="2026-02-01T00:00:00Z")
        candidate, _ = self.plan()
        self.assertEqual([op for op in candidate["ops"] if op["reason"] == "exact_dedupe"], [])

    def test_same_text_different_conflict_group_not_deduped(self):
        self.add("same", conflict_group="g1")
        self.add("same", conflict_group="g2")
        candidate, _ = self.plan()
        self.assertEqual([op for op in candidate["ops"] if op["reason"] == "exact_dedupe"], [])

    def test_same_key_different_text_not_expired(self):
        self.add("old", key="k")
        self.add("new", key="k")
        candidate, _ = self.plan()
        self.assertEqual(candidate["ops"], [])
        self.assertTrue(candidate["reports"]["possible_conflict"])

    def test_explicit_supersede_expires_old(self):
        old = self.add("old")
        self.add("new", supersedes_id=old.id)
        candidate, _ = self.plan()
        self.assertEqual([op["item_id"] for op in candidate["ops"]], [old.id])
        self.assertEqual(candidate["ops"][0]["reason"], "explicit_supersede")

    def test_future_supersede_does_not_expire(self):
        old = self.add("old")
        self.add("new", supersedes_id=old.id, valid_from="2026-12-02T00:00:00Z")
        candidate, _ = self.plan()
        self.assertEqual(candidate["ops"], [])

    def test_already_expired_only_reported(self):
        self.add("old", valid_from="2025-01-01T00:00:00Z", valid_to="2026-01-01T00:00:00Z")
        candidate, _ = self.plan()
        self.assertGreaterEqual(candidate["reports"]["already_expired"], 1)
        self.assertEqual(candidate["ops"], [])

    def test_deleted_not_in_candidate(self):
        item = self.add("same")
        self.store.delete_item(item.id)
        candidate, _ = self.plan()
        self.assertEqual(candidate["ops"], [])

    def test_cross_owner_supersede_reported(self):
        other = self.store.add_item(
            "owner-other", kind="project_fact", layer="project",
            trust="user_confirmed", key="x", text="other",
        )
        self.add("new", supersedes_id=other.id)
        candidate, _ = self.plan()
        self.assertEqual(candidate["ops"], [])
        self.assertTrue(candidate["reports"]["orphan_supersedes"])

    def test_base_sha_mismatch_rejected(self):
        candidate, snapshot = self.plan()
        tampered = dict(candidate)
        tampered["base_sha256"] = "0" * 64
        result = sleep.validate(self.store, tampered, snapshot)
        self.assertFalse(result["ok"])
        self.assertIn("base_sha256", result["errors"])

    def test_stale_item_hash_rejected(self):
        item = self.add("same")
        self.add("same")
        candidate, snapshot = self.plan()
        self.store.delete_item(item.id)
        result = sleep.validate(self.store, candidate, snapshot)
        self.assertFalse(result["ok"])

    def test_duplicate_op_rejected(self):
        item = self.add("same")
        self.add("same")
        candidate, snapshot = self.plan()
        candidate["ops"] = candidate["ops"] + candidate["ops"]
        body = {k: v for k, v in candidate.items() if k != "candidate_id"}
        candidate["candidate_id"] = sleep._sha(body)
        result = sleep.validate(self.store, candidate, snapshot)
        self.assertFalse(result["ok"])

    def test_apply_does_not_rewrite_content(self):
        first = self.add("same", source_ref="src")
        self.add("same", source_ref="src")
        candidate, snapshot = self.plan()
        before = self.store.get_item(first.id)
        sleep.apply(self.store, candidate, snapshot)
        after = self.store.get_item(first.id)
        self.assertEqual(before.text, after.text)
        self.assertEqual(before.trust, after.trust)
        self.assertEqual(before.key, after.key)
        self.assertIsNotNone(after.valid_to)

    def test_apply_writes_event(self):
        first = self.add("same")
        self.add("same")
        candidate, snapshot = self.plan()
        sleep.apply(self.store, candidate, snapshot)
        events = self.store.events(first.id)
        self.assertTrue(any(event.action == "expire" for event in events))

    def test_validate_failure_writes_nothing(self):
        item = self.add("same")
        self.add("same")
        candidate, snapshot = self.plan()
        candidate["base_sha256"] = "0" * 64

        with self.assertRaises(sleep.SleepError):
            sleep.apply(self.store, candidate, snapshot)

        self.assertIsNone(self.store.get_item(item.id).valid_to)

    def test_restore_items_and_fts_view(self):
        item = self.add("same")
        self.add("same")
        candidate, snapshot = self.plan()
        sleep.apply(self.store, candidate, snapshot)
        self.assertIsNotNone(self.store.get_item(item.id).valid_to)
        result = sleep.restore(self.store, snapshot.id)
        self.assertIsNone(self.store.get_item(item.id).valid_to)
        self.assertFalse(result["events_snapshot_included"])

    def test_ab_metrics(self):
        self.add("alpha project fact", key="a")
        self.add("beta project fact", key="b")
        queries = [
            {"query": "alpha", "expected_keys": ["a"]},
            {"query": "beta", "expected_keys": ["b"]},
        ]
        result = sleep.ab_retrieval(self.store, OWNER, queries)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["recall_at_5"], 1.0)
        self.assertGreater(result["mrr"], 0)

    def test_replica_freeze_source_unchanged(self):
        self.add("same")
        source = self.store.db_path
        replica = self.root / "replica.db"
        result = sleep.replica_freeze(source, replica)
        self.assertTrue(result["source_unchanged"])
        self.assertEqual(result["source_digest_before"], result["source_digest_after"])
        self.assertTrue(replica.is_file())

    def test_source_digest_sensitive_to_write(self):
        before = sleep.source_digest(self.store.db_path)
        self.add("new")
        after = sleep.source_digest(self.store.db_path)
        self.assertNotEqual(before, after)

    def test_cli_guard_rejects_production(self):
        spec = importlib.util.spec_from_file_location(
            "memory_sleep_cli",
            Path(__file__).resolve().parents[2] / "scripts" / "memory_sleep.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        from src import config

        with self.assertRaises(sleep.SleepError):
            module._guard(str(config.paths.knowledge_db))

    def test_candidate_id_is_canonical(self):
        self.add("same")
        self.add("same")
        candidate, _ = self.plan()
        body = {k: v for k, v in candidate.items() if k != "candidate_id"}
        self.assertEqual(candidate["candidate_id"], sleep._sha(body))



    def test_fixture_frozen_30x3(self):
        fixture = json.loads(
            (Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "memory" / "sleep_ab.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(len(fixture["contexts"]), 30)
        self.assertEqual(sum(len(context["queries"]) for context in fixture["contexts"]), 90)
        body = {key: value for key, value in fixture.items() if key != "sha256"}
        self.assertEqual(fixture["sha256"], sleep._sha(body))

    def test_fixture_ab_runs(self):
        fixture = json.loads(
            (Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "memory" / "sleep_ab.json")
            .read_text(encoding="utf-8")
        )
        owner = fixture["owner_id"]

        for spec in fixture["items"]:
            self.store.add_item(owner, **spec)

        queries = [query for context in fixture["contexts"] for query in context["queries"]]
        result = sleep.ab_retrieval(self.store, owner, queries)
        self.assertEqual(result["total"], 90)
        self.assertGreaterEqual(result["recall_at_5"], 0.9)


if __name__ == "__main__":
    unittest.main()
