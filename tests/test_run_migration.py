"""D6 旧版数据库副本迁移、幂等与备份恢复测试。"""

import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from src.core.bot_store import BotStore
from src.core.run_migration import (
    MigrationError,
    migrate_legacy_database,
    restore_backup,
)
from src.core.run_store import RunStore


class RunMigrationTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_path = self.root / "legacy-copy.db"
        self.backup_dir = self.root / "backups"
        bots = BotStore(self.db_path)
        self.alice_bot = bots.create_bot(
            owner_id="alice", name="分析助手", persona="谨慎",
            system_prompt="分析", api_source="platform",
        )
        self.bob_bot = bots.create_bot(
            owner_id="bob", name="另一位助手", persona="直接",
            system_prompt="回答", api_source="platform",
        )
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.executescript(
                """
                CREATE TABLE rooms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    stage TEXT NOT NULL DEFAULT 'opening',
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id INTEGER NOT NULL,
                    turn_id TEXT NOT NULL,
                    speaker TEXT NOT NULL,
                    bot_id INTEGER,
                    role TEXT NOT NULL,
                    stage TEXT,
                    content TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                """
            )
            conn.executemany(
                "INSERT INTO rooms(id, owner_id, name, stage, created_at) VALUES (?, ?, ?, ?, ?)",
                [
                    (1, "alice", "旧讨论", "closing", 1700000000),
                    (2, "bob", "归属隔离", "opening", 1700000100),
                ],
            )
            conn.executemany(
                """
                INSERT INTO messages(
                    id, room_id, turn_id, speaker, bot_id, role,
                    stage, content, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (1, 1, "turn-original", "user", None, "core", "opening", "原问题", 1700000001),
                    (2, 1, "turn-original", "bot", self.alice_bot["id"], "core", "statement", "原回答", 1700000002),
                    (3, 1, "turn-original", "bot", self.alice_bot["id"], "summary", "closing", "后写入但时间更早", 1700000001),
                    (4, 1, "turn-next", "user", None, "core", "opening", "下一轮", 1700000003),
                    (5, 2, "turn-bob", "user", None, "core", "opening", "Bob 的问题", 1700000101),
                    (6, 2, "turn-bob", "bot", self.bob_bot["id"], "core", "statement", "Bob 的回答", 1700000102),
                ],
            )
        self.before = self.legacy_rows()

    def legacy_rows(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return {
                table: conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
                for table in ("bots", "rooms", "messages")
            }

    def test_migration_preserves_turn_order_and_legacy_rows(self):
        result = migrate_legacy_database(self.db_path, self.backup_dir)
        self.assertTrue(Path(result["backup"]).is_file())
        self.assertEqual(result["runs_created"], 2)
        self.assertEqual(result["turns_created"], 3)
        self.assertEqual(result["steps_created"], 6)
        self.assertEqual(self.legacy_rows(), self.before)

        store = RunStore(self.db_path)
        run = store.get_run("legacy-room-1", owner_id="alice")
        self.assertEqual([turn["id"] for turn in run["turns"]], ["turn-original", "turn-next"])
        first = run["turns"][0]
        self.assertEqual(first["user_message"], "原问题")
        self.assertEqual(
            [step["source_message_id"] for step in first["steps"]],
            [1, 3, 2],
        )
        self.assertEqual(first["steps"][0]["created_at"], "2023-11-14T22:13:21+00:00")
        self.assertTrue(all(step["exec_id"] is None for turn in run["turns"] for step in turn["steps"]))
        with self.assertRaises(KeyError):
            store.get_run("legacy-room-1", owner_id="bob")

    def test_repeated_migration_does_not_duplicate_or_fabricate_exec(self):
        first = migrate_legacy_database(self.db_path, self.backup_dir)
        second = migrate_legacy_database(self.db_path, self.backup_dir)
        self.assertNotEqual(first["backup"], second["backup"])
        self.assertEqual(second["runs_created"], 0)
        self.assertEqual(second["turns_created"], 0)
        self.assertEqual(second["steps_created"], 0)
        self.assertEqual(self.legacy_rows(), self.before)
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_turns").fetchone()[0], 3)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_steps").fetchone()[0], 6)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_execs").fetchone()[0], 0)

    def test_restore_removes_new_tables_and_keeps_legacy_rows(self):
        result = migrate_legacy_database(self.db_path, self.backup_dir)
        restored = restore_backup(self.db_path, result["backup"], expected_snapshot=self.before)
        self.assertEqual(restored["bots"], 2)
        self.assertEqual(restored["messages"], 6)
        self.assertEqual(self.legacy_rows(), self.before)
        with closing(sqlite3.connect(self.db_path)) as conn:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE name LIKE 'agent_%'"
                )
            }
        self.assertEqual(tables, set())

    def test_failure_restores_backup_atomically(self):
        # Inject the fault only after the pristine online backup is complete.
        from unittest.mock import patch
        from src.core.run_store import init_run_schema

        def fail_after_schema(conn):
            init_run_schema(conn)
            raise MigrationError("注入迁移失败")

        with patch("src.core.run_migration.init_run_schema", side_effect=fail_after_schema):
            with self.assertRaisesRegex(MigrationError, "注入迁移失败"):
                migrate_legacy_database(self.db_path, self.backup_dir)
        self.assertEqual(self.legacy_rows(), self.before)
        with closing(sqlite3.connect(self.db_path)) as conn:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE name LIKE 'agent_%'"
                )
            }
        self.assertEqual(tables, set())


if __name__ == "__main__":
    unittest.main()
