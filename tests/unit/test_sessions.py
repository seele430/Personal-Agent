"""会话存储与 API 测试。"""

import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import sessions_api
from src.sessions import SessionError, SessionStore


class StoreCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.now = [1000.0]
        self.store = SessionStore(
            Path(self._temporary.name) / "sessions.db",
            clock=lambda: self.now[0],
        )

    def tick(self, step=10.0):
        self.now[0] += step

    def test_create_and_get(self):
        session = self.store.create("web", "测试会话")

        self.assertEqual(session["source"], "web")
        self.assertEqual(session["title"], "测试会话")
        self.assertIsNotNone(self.store.get(session["id"]))

    def test_list_orders_pinned_then_recent(self):
        first = self.store.create("web", "A")
        self.tick()
        second = self.store.create("web", "B")
        self.tick()
        self.store.set_pinned(first["id"], True)

        rows = self.store.list()

        self.assertEqual(rows[0]["id"], first["id"])
        self.assertEqual(rows[1]["id"], second["id"])

    def test_message_idempotent(self):
        session = self.store.create("web")
        self.assertTrue(self.store.add_message(session["id"], "user", "hi", "m1"))
        self.assertFalse(self.store.add_message(session["id"], "user", "hi", "m1"))

        self.assertEqual(self.store.get(session["id"])["message_count"], 1)

    def test_messages_after_cursor(self):
        session = self.store.create("web")
        self.store.add_message(session["id"], "user", "one", "m1")
        self.store.add_message(session["id"], "assistant", "two", "m1")

        rows = self.store.messages(session["id"], after=1)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content"], "two")

    def test_context_trims_to_budget(self):
        session = self.store.create("web")

        for index in range(20):
            self.store.add_message(session["id"], "user", f"消息{index}", f"m{index}")

        context = self.store.context(session["id"], max_messages=5, max_chars=100)

        self.assertIn("消息19", context)
        self.assertLessEqual(len(context), 120)

    def test_rename_pin_soft_delete(self):
        session = self.store.create("web")
        self.assertTrue(self.store.rename(session["id"], "新名字"))
        self.assertEqual(self.store.get(session["id"])["title_source"], "user")
        self.assertTrue(self.store.set_pinned(session["id"], True))
        self.assertTrue(self.store.soft_delete(session["id"]))
        self.assertFalse(self.store.soft_delete("no-such"))
        self.assertEqual(self.store.list(), [])
        self.assertEqual(len(self.store.list(include_deleted=True)), 1)

    def test_find_by_prefix_rules(self):
        session = self.store.create("web", "A")
        prefix = session["id"][:8]

        self.assertEqual(self.store.find_by_prefix(prefix)[0]["id"], session["id"])

        with self.assertRaises(SessionError):
            self.store.find_by_prefix(session["id"][:3])

    def test_ensure_qq_launch_idempotent(self):
        first = self.store.ensure_qq_launch("launch-1", owner_id=7)
        second = self.store.ensure_qq_launch("launch-1", owner_id=7)

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.store.active_session(7)["id"], first["id"])
        self.assertEqual(len(self.store.list()), 1)

    def test_ensure_active_creates_when_missing(self):
        session = self.store.ensure_active(7)

        self.assertEqual(session["source"], "qq")
        self.assertEqual(self.store.active_session(7)["id"], session["id"])


class ApiCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.store = SessionStore(Path(self._temporary.name) / "sessions.db")
        app = FastAPI()
        app.include_router(sessions_api.router)
        self.client = TestClient(app)
        patcher = patch.object(sessions_api, "_store", lambda: self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_create_list_and_read(self):
        created = self.client.post("/sessions", json={"title": "网页会话"}).json()
        listed = self.client.get("/sessions").json()["sessions"]

        self.assertEqual(listed[0]["id"], created["id"])

        self.store.add_message(created["id"], "user", "你好", "m1")
        payload = self.client.get(f"/sessions/{created['id']}").json()

        self.assertEqual(payload["messages"][0]["content"], "你好")

    def test_patch_pin_and_delete(self):
        created = self.client.post("/sessions", json={}).json()

        patched = self.client.patch(
            f"/sessions/{created['id']}", json={"pinned": True, "title": "改名"}
        ).json()

        self.assertTrue(patched["pinned"])
        self.assertEqual(patched["title"], "改名")

        self.assertEqual(self.client.delete(f"/sessions/{created['id']}").status_code, 200)
        self.assertEqual(self.client.get("/sessions").json()["sessions"], [])

    def test_missing_session_404(self):
        self.assertEqual(self.client.get("/sessions/nope").status_code, 404)


if __name__ == "__main__":
    unittest.main()
