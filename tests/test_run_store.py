"""D6 run / turn / step / exec 数据模型的临时 SQLite 集成测试。"""

import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from src.core.run_store import RunStore, init_run_schema


class RunStoreTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "chat-requests-test.db"
        self.store = RunStore(self.db_path)

    def test_tool_exec_is_linked_to_real_step(self):
        run = self.store.create_run(owner_id="alice", request_id="request-1")
        turn = self.store.create_turn(run["id"], user_message="提醒我喝水")
        step = self.store.add_step(
            turn["id"], kind="tool", actor="assistant", content="创建提醒"
        )
        self.assertIsNone(step["exec_id"])

        execution = self.store.record_exec(
            step["id"],
            tool_name="create_reminder",
            request={"message": "喝水"},
            status="running",
        )
        self.store.update_exec(
            execution["id"], status="succeeded", result={"reminder_id": 7}
        )
        self.store.finish(run["id"], status="succeeded")

        actual = self.store.get_run(run["id"], owner_id="alice")
        actual_step = actual["turns"][0]["steps"][0]
        self.assertEqual(actual_step["exec_id"], execution["id"])
        self.assertEqual(actual_step["exec"]["tool_name"], "create_reminder")
        self.assertEqual(actual_step["exec"]["request"], {"message": "喝水"})
        self.assertEqual(actual_step["exec"]["result"], {"reminder_id": 7})
        self.assertEqual(actual_step["exec"]["status"], "succeeded")
        self.assertEqual(actual["status"], "succeeded")

    def test_owner_isolation_does_not_reveal_run(self):
        run = self.store.create_run(owner_id="alice")
        with self.assertRaises(KeyError):
            self.store.get_run(run["id"], owner_id="bob")
        self.assertEqual(
            self.store.get_run(run["id"], owner_id="alice")["owner_id"],
            "alice",
        )

    def test_only_one_exec_per_step(self):
        run = self.store.create_run(owner_id="alice")
        turn = self.store.create_turn(run["id"], user_message="测试")
        step = self.store.add_step(
            turn["id"], kind="tool", actor="assistant", content="执行"
        )
        self.store.record_exec(step["id"], tool_name="one", request={})
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.record_exec(step["id"], tool_name="two", request={})

    def test_new_model_has_no_legacy_stage(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            for table in (
                "agent_runs", "agent_turns", "agent_steps", "agent_execs"
            ):
                columns = {
                    row[1] for row in conn.execute(f"PRAGMA table_info({table})")
                }
                self.assertNotIn("stage", columns)

    def test_schema_and_data_share_one_transaction(self):
        db_path = Path(self.temp.name) / "atomic.db"
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            init_run_schema(conn)
            self.assertTrue(conn.in_transaction)
            conn.execute(
                """
                INSERT INTO agent_runs(
                    id, owner_id, status, created_at, updated_at
                ) VALUES ('run-rollback', 'alice', 'running', 'now', 'now')
                """
            )
            conn.rollback()
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE name = 'agent_runs'"
                )
            }
            self.assertEqual(tables, set())

    def test_invalid_exec_and_run_states_are_rejected(self):
        run = self.store.create_run(owner_id="alice")
        turn = self.store.create_turn(run["id"], user_message="测试")
        step = self.store.add_step(
            turn["id"], kind="tool", actor="assistant", content="执行"
        )
        with self.assertRaises(ValueError):
            self.store.record_exec(step["id"], tool_name="x", request={}, status="done")
        with self.assertRaises(ValueError):
            self.store.finish(run["id"], status="done")


if __name__ == "__main__":
    unittest.main()
