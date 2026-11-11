"""Sleep-time Compute Phase 1 测试。"""

import importlib.util
import json
import sqlite3
import tempfile
import unittest

from pathlib import Path

from src.memory import sleep
from src.memory.models import MemoryError
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

    def sql(self, statement, params=()):
        connection = sqlite3.connect(self.store.db_path)

        try:
            connection.execute(statement, params)
            connection.commit()
        finally:
            connection.close()

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
        result = sleep.ab_retrieval(self.store, OWNER, queries, as_of=AS_OF)
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
        result = sleep.ab_retrieval(self.store, owner, queries, as_of=fixture["as_of"])
        self.assertEqual(result["total"], 90)
        self.assertGreaterEqual(result["recall_at_5"], 0.9)



    def test_fixture_dedupe_then_ab_not_regressed(self):
        fixture = json.loads(
            (Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "memory" / "sleep_ab.json")
            .read_text(encoding="utf-8")
        )
        owner = fixture["owner_id"]

        for spec in fixture["items"]:
            self.store.add_item(owner, **spec)

        queries = [query for context in fixture["contexts"] for query in context["queries"]]
        snapshot = self.versions.create("fixture-base")
        candidate = sleep.propose(self.store, owner, as_of=fixture["as_of"], snapshot=snapshot)
        self.assertEqual(len(candidate["ops"]), 5)
        before = sleep.ab_retrieval(self.store, owner, queries, as_of=fixture["as_of"])
        sleep.apply(self.store, candidate, snapshot)
        after = sleep.ab_retrieval(self.store, owner, queries, as_of=fixture["as_of"])
        self.assertEqual(before["recall_at_5"], after["recall_at_5"])
        self.assertEqual(after["recall_at_5"], 1.0)



    def _forge(self, snapshot, ops):
        candidate = {
            "schema_version": sleep.SCHEMA_VERSION,
            "ruleset_version": sleep.RULESET_VERSION,
            "base_snapshot_id": snapshot.id,
            "base_sha256": snapshot.sha256,
            "owner_id": OWNER,
            "as_of": sleep.normalize_time(AS_OF, field="as_of"),
            "ops": ops,
            "reports": {},
        }
        candidate["candidate_id"] = sleep._sha(candidate)

        return candidate

    def test_forged_expire_without_duplicate_rejected(self):
        item = self.add("solo")
        snapshot = self.snapshot()
        candidate = self._forge(snapshot, [{
            "op": "expire", "reason": "exact_dedupe", "item_id": item.id,
            "expected_sha256": sleep.item_hash(item), "evidence_ids": [999],
        }])
        result = sleep.validate(self.store, candidate, snapshot)
        self.assertFalse(result["ok"])
        self.assertIn("semantic_mismatch", result["errors"])

        with self.assertRaises(sleep.SleepError):
            sleep.apply(self.store, candidate, snapshot)

        self.assertIsNone(self.store.get_item(item.id).valid_to)

    def test_supersede_cycle_fails_closed(self):
        first = self.add("a")
        second = self.add("b", supersedes_id=first.id)
        self.sql(
            "UPDATE memory_items SET supersedes_id = ? WHERE id = ?",
            (second.id, first.id),
        )
        snapshot = self.snapshot()
        candidate = sleep.propose(self.store, OWNER, as_of=AS_OF, snapshot=snapshot)
        self.assertEqual(candidate["ops"], [])
        self.assertEqual(sorted(candidate["reports"]["supersede_cycles"]), sorted([first.id, second.id]))

    def test_base_drift_rejected(self):
        self.add("same")
        self.add("same")
        candidate, snapshot = self.plan()
        self.add("new after snapshot")

        with self.assertRaises(sleep.SleepError) as caught:
            sleep.apply(self.store, candidate, snapshot)

        self.assertIn("base_mismatch", str(caught.exception))

    def test_target_changed_after_plan_rejected(self):
        item = self.add("same")
        self.add("same")
        candidate, snapshot = self.plan()
        self.sql(
            "UPDATE memory_items SET text = ? WHERE id = ?", ("changed", item.id)
        )

        with self.assertRaises(sleep.SleepError):
            sleep.apply(self.store, candidate, snapshot)

        self.assertIsNone(self.store.get_item(item.id).valid_to)

    def test_ab_regression_gate_fails(self):
        before = {"recall_at_5": 1.0, "mrr": 1.0}
        after = {"recall_at_5": 0.0, "mrr": 0.0}
        result = sleep.evaluate_ab(before, after)
        self.assertFalse(result["passed"])
        self.assertIn("recall_regressed", result["reasons"])

    def test_ab_latency_samples_recorded(self):
        self.add("alpha project fact", key="a")
        queries = [{"query": "alpha", "expected_keys": ["a"]}]
        result = sleep.ab_retrieval(self.store, OWNER, queries, as_of=AS_OF)
        self.assertEqual(len(result["latency"]["samples_ms"]), 1)
        self.assertIn("p50_ms", result["latency"])

    def test_restore_report_fields(self):
        item = self.add("same")
        self.add("same")
        candidate, snapshot = self.plan()
        sleep.apply(self.store, candidate, snapshot)
        result = sleep.restore(self.store, snapshot.id)
        self.assertIn("items_restored", result)
        self.assertTrue(result["fts_view_restored"])
        self.assertFalse(result["events_snapshot_included"])

    def test_source_digest_missing_table_fails_closed(self):
        empty = self.root / "empty.db"
        import sqlite3

        sqlite3.connect(empty).close()

        with self.assertRaises(sleep.SleepError):
            sleep.source_digest(empty)



    def test_group_metrics_fixture(self):
        fixture = json.loads(
            (Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "memory" / "sleep_ab_groups.json")
            .read_text(encoding="utf-8")
        )
        owner = fixture["owner_id"]

        ids = {}

        for spec in fixture["items"]:
            data = {key: value for key, value in spec.items() if key not in ("ref", "supersedes_ref")}

            if spec.get("supersedes_ref"):
                data["supersedes_id"] = ids[spec["supersedes_ref"]]

            item = self.store.add_item(owner, **data)
            ids[spec.get("ref") or spec["key"]] = item.id

            if spec["key"] in fixture["deleted_keys"]:
                self.store.delete_item(item.id)

        metrics = sleep.group_metrics(
            self.store, owner, fixture["groups"], as_of=fixture["as_of"]
        )
        self.assertEqual(metrics["recall"]["recall_at_5"], 1.0)
        self.assertEqual(metrics["conflict"]["conflict_top1"], 1.0)
        self.assertEqual(metrics["unanswerable"]["unanswerable_false_hits"], 0)
        self.assertEqual(metrics["deletion"]["revived"], 0)
        self.assertEqual(metrics["supersede"]["recall_at_5"], 1.0)
        self.assertEqual(metrics["expired"]["unanswerable_false_hits"], 0)
        self.assertEqual(metrics["future"]["unanswerable_false_hits"], 0)



    def test_metadata_drift_rejected(self):
        for field in ("created_at", "updated_at"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp:
                store = MemoryStore(Path(temp) / "memory.db")
                item = store.add_item(
                    OWNER, kind="project_fact", layer="project",
                    trust="user_confirmed", key="k", text="same",
                )
                store.add_item(
                    OWNER, kind="project_fact", layer="project",
                    trust="user_confirmed", key="k", text="same",
                )
                snapshot = MemoryVersions(store).create("drift-base")
                candidate = sleep.propose(store, OWNER, as_of=AS_OF, snapshot=snapshot)
                connection = sqlite3.connect(store.db_path)

                try:
                    connection.execute(
                        f"UPDATE memory_items SET {field} = ? WHERE id = ?",
                        ("2020-01-01T00:00:00+00:00", item.id),
                    )
                    connection.commit()
                finally:
                    connection.close()

                with self.assertRaises(sleep.SleepError) as caught:
                    sleep.apply(store, candidate, snapshot)

                self.assertIn("base_mismatch", str(caught.exception))

    def test_invalid_as_of_rejected(self):
        item = self.add("same")
        self.add("same")
        snapshot = self.snapshot()
        candidate = self._forge(snapshot, [{
            "op": "expire", "reason": "exact_dedupe", "item_id": item.id,
            "expected_sha256": sleep.item_hash(item), "evidence_ids": [item.id],
        }])
        candidate["as_of"] = "not-a-time"
        candidate["candidate_id"] = sleep._sha({k: v for k, v in candidate.items() if k != "candidate_id"})
        result = sleep.validate(self.store, candidate, snapshot)
        self.assertFalse(result["ok"])
        self.assertIn("as_of_invalid", result["errors"])

        with self.assertRaises(MemoryError):
            sleep.propose(self.store, OWNER, as_of="not-a-time", snapshot=snapshot)

    def test_group_gate_fails_on_safety_regression(self):
        before_groups = {"conflict": {"recall_at_5": 1.0, "mrr": 1.0, "conflict_top1": 1.0, "unanswerable_false_hits": 0, "revived": 0}}
        after_groups = {"conflict": {"recall_at_5": 1.0, "mrr": 1.0, "conflict_top1": 0.0, "unanswerable_false_hits": 2, "revived": 1}}
        result = sleep.evaluate_ab(
            {"recall_at_5": 1.0, "mrr": 1.0}, {"recall_at_5": 1.0, "mrr": 1.0},
            before_groups=before_groups, after_groups=after_groups,
        )
        self.assertFalse(result["passed"])
        self.assertIn("conflict_top1_regressed:conflict", result["reasons"])
        self.assertIn("unanswerable_false_hits:conflict", result["reasons"])
        self.assertIn("deleted_revived:conflict", result["reasons"])

    def test_ab_paired_rounds_recorded(self):
        self.add("alpha project fact", key="a")
        queries = [{"query": "alpha", "expected_keys": ["a"]}]
        result = sleep.ab_paired(self.store, self.store, OWNER, queries, as_of=AS_OF, rounds=2)
        self.assertEqual(result["rounds"], 2)
        self.assertEqual(result["order"], ["before", "after", "after", "before"])
        self.assertEqual(len(result["paired_deltas_ms"]), 4)
        self.assertEqual(len(result["before_samples_ms"]), 4)



    def test_as_of_none_rejected_everywhere(self):
        item = self.add("same")
        snapshot = self.snapshot()
        candidate = self._forge(snapshot, [{
            "op": "expire", "reason": "exact_dedupe", "item_id": item.id,
            "expected_sha256": sleep.item_hash(item), "evidence_ids": [item.id],
        }])

        for call in (
            lambda: sleep.propose(self.store, OWNER, as_of=None, snapshot=snapshot),
            lambda: sleep.ab_retrieval(self.store, OWNER, [], as_of=None),
            lambda: sleep.ab_paired(self.store, self.store, OWNER, [], as_of=None),
        ):
            with self.assertRaises(sleep.SleepError):
                call()

        bad = dict(candidate)
        bad["as_of"] = None
        bad["candidate_id"] = sleep._sha({k: v for k, v in bad.items() if k != "candidate_id"})
        result = sleep.validate(self.store, bad, snapshot)
        self.assertFalse(result["ok"])
        self.assertIn("as_of_invalid", result["errors"])

    def test_ab_retrieval_normalizes_timezone(self):
        self.add("alpha project fact", key="a")
        result = sleep.ab_retrieval(
            self.store, OWNER, [{"query": "alpha", "expected_keys": ["a"]}],
            as_of="2026-12-01T08:00:00+08:00",
        )
        self.assertEqual(result["as_of"], "2026-12-01T00:00:00+00:00")

    def test_baseline_false_hits_fail_even_if_after_zero(self):
        before_groups = {"unanswerable": {"recall_at_5": 0.0, "mrr": 0.0, "conflict_top1": None, "unanswerable_false_hits": 1, "revived": 0}}
        after_groups = {"unanswerable": {"recall_at_5": 0.0, "mrr": 0.0, "conflict_top1": None, "unanswerable_false_hits": 0, "revived": 0}}
        result = sleep.evaluate_ab(
            {"recall_at_5": 0.0, "mrr": 0.0}, {"recall_at_5": 0.0, "mrr": 0.0},
            before_groups=before_groups, after_groups=after_groups,
        )
        self.assertFalse(result["passed"])
        self.assertIn("baseline_unanswerable_false_hits:unanswerable", result["reasons"])

    def test_revived_counted_on_unanswerable(self):
        self.add("deleted project fact", key="deleted-key")
        fixture = {
            "groups": [{
                "name": "deletion",
                "queries": [{
                    "query": "deleted", "expected_keys": [],
                    "unanswerable": True, "forbidden_keys": ["deleted-key"],
                }],
            }],
        }
        metrics = sleep.group_metrics(self.store, OWNER, fixture["groups"], as_of=AS_OF)
        self.assertEqual(metrics["deletion"]["revived"], 1)

    def test_cli_baseline_only_and_invalid_time(self):
        spec = importlib.util.spec_from_file_location(
            "memory_sleep_cli",
            Path(__file__).resolve().parents[2] / "scripts" / "memory_sleep.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.add("alpha project fact", key="a")
        queries = self.root / "queries.json"
        queries.write_text(json.dumps({"queries": [{"query": "alpha", "expected_keys": ["a"]}]}), encoding="utf-8")

        code = module.main([
            "ab", "--replica-db", str(self.store.db_path), "--owner", OWNER,
            "--queries", str(queries), "--as-of", AS_OF,
        ])
        self.assertEqual(code, 0)
        bad = module.main([
            "ab", "--replica-db", str(self.store.db_path), "--owner", OWNER,
            "--queries", str(queries), "--as-of", "not-a-time",
        ])
        self.assertEqual(bad, 2)


if __name__ == "__main__":
    unittest.main()
