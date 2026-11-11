"""Sleep 运行记录存储测试。"""

import importlib.util
import json
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from src.memory.store import MemoryStore
from src.sleep_store import SleepRunStore


class SleepRunStoreCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.store = SleepRunStore(Path(self._temporary.name) / "sleep_runs.db")

    def test_add_and_get(self):
        run_id = self.store.add_run(
            owner_id="u1", mode="plan", candidate_id="c1", ops=[
                {"op": "expire", "reason": "exact_dedupe", "item_id": 1,
                 "item_key": "k1", "evidence_ids": [2], "evidence_keys": ["k1"]},
            ],
            metrics={"recall_before": 1.0, "recall_after": 1.0},
        )
        run = self.store.get_run("u1", run_id)
        self.assertEqual(run["mode"], "plan")
        self.assertEqual(run["ops"][0]["item_key"], "k1")
        self.assertNotIn("owner_id", run)

    def test_owner_isolation(self):
        run_id = self.store.add_run(owner_id="u1", mode="plan")
        self.assertIsNone(self.store.get_run("u2", run_id))
        self.assertEqual(self.store.list_runs("u2")["total"], 0)

    def test_filters_and_pagination(self):
        self.store.add_run(owner_id="u1", mode="plan")
        self.store.add_run(owner_id="u1", mode="ab", gate_status="failed", execution_status="succeeded")
        self.assertEqual(self.store.list_runs("u1", mode="ab")["total"], 1)
        self.assertEqual(self.store.list_runs("u1", gate_status="failed")["total"], 1)
        page = self.store.list_runs("u1", limit=1, offset=0)
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["total"], 2)

    def test_invalid_filters(self):
        with self.assertRaises(ValueError):
            self.store.list_runs("u1", mode="bad")

        with self.assertRaises(ValueError):
            self.store.list_runs("u1", limit=0)

    def test_summary_truncation_marks_degraded(self):
        ops = [
            {"op": "expire", "reason": "exact_dedupe", "item_id": i, "item_key": "k" * 300}
            for i in range(5)
        ]
        run_id = self.store.add_run(owner_id="u1", mode="plan", ops=ops)
        run = self.store.get_run("u1", run_id)
        self.assertEqual(run["record_status"], "degraded")
        self.assertEqual(run["ops_count"], 5)



    def test_cli_plan_records_once(self):
        spec = importlib.util.spec_from_file_location(
            "memory_sleep_cli",
            Path(__file__).resolve().parents[2] / "scripts" / "memory_sleep.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        db = Path(self._temporary.name) / "memory.db"
        memory = MemoryStore(db)
        memory.add_item(
            "u1", kind="project_fact", layer="project",
            trust="user_confirmed", key="k", text="alpha project fact",
        )
        out = Path(self._temporary.name) / "candidate.json"

        with patch.object(module, "SleepRunStore", return_value=self.store):
            code = module.main([
                "plan", "--replica-db", str(db), "--owner", "u1",
                "--as-of", "2026-12-01T00:00:00Z", "--out", str(out),
            ])

        self.assertEqual(code, 0)
        runs = self.store.list_runs("u1")
        self.assertEqual(runs["total"], 1)
        self.assertEqual(runs["items"][0]["mode"], "plan")

    def test_cli_baseline_ab_records_not_run(self):
        spec = importlib.util.spec_from_file_location(
            "memory_sleep_cli",
            Path(__file__).resolve().parents[2] / "scripts" / "memory_sleep.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        db = Path(self._temporary.name) / "memory.db"
        memory = MemoryStore(db)
        memory.add_item(
            "u1", kind="project_fact", layer="project",
            trust="user_confirmed", key="k", text="alpha project fact",
        )
        queries = Path(self._temporary.name) / "queries.json"
        queries.write_text(json.dumps({"queries": [{"query": "alpha", "expected_keys": ["k"]}]}), encoding="utf-8")

        with patch.object(module, "SleepRunStore", return_value=self.store):
            code = module.main([
                "ab", "--replica-db", str(db), "--owner", "u1",
                "--queries", str(queries), "--as-of", "2026-12-01T00:00:00Z",
            ])

        self.assertEqual(code, 0)
        run = self.store.list_runs("u1")["items"][0]
        self.assertEqual(run["ab_variant"], "baseline_only")
        self.assertEqual(run["gate_status"], "not_run")


if __name__ == "__main__":
    unittest.main()
