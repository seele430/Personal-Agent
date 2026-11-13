"""小号上线检测测试。"""

import unittest

from unittest.mock import patch

from src.qq import launch_watcher


class FakeClient:
    def __init__(self, online):
        self.online = online
        self.calls = 0

    def _call(self, action, payload=None):
        self.calls += 1

        # OneBotClient._call 返回的已经是 body["data"]
        return {"online": self.online}


class FakeStore:
    def __init__(self, created):
        self.created = created

    def ensure_qq_launch(self, launch_id, *, owner_id, title=""):
        self.created.append({"launch_id": launch_id, "owner_id": owner_id, "title": title})

        return {"id": "fake"}


class PollCase(unittest.TestCase):
    def poll(self, state, online, owner=7, created=None):
        created = created if created is not None else []

        return launch_watcher.poll_once(
            state,
            client=FakeClient(online),
            owner=owner,
            store_factory=lambda: FakeStore(created),
            clock=lambda: 1000.0,
        ), created

    def test_first_poll_records_baseline_without_session(self):
        state, created = self.poll({}, online=True)

        self.assertEqual(created, [])
        self.assertTrue(state["last"])

    def test_offline_to_online_creates_session(self):
        state, created = self.poll({"last": False}, online=True)

        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["owner_id"], 7)
        self.assertIn("QQ ·", created[0]["title"])

    def test_staying_online_does_not_create(self):
        state, created = self.poll({"last": True}, online=True)

        self.assertEqual(created, [])

    def test_going_offline_marks_state(self):
        state, created = self.poll({"last": True}, online=False)

        self.assertFalse(state["last"])
        self.assertEqual(created, [])

    def test_owner_zero_skips_creation(self):
        state, created = self.poll({"last": False}, online=True, owner=0)

        self.assertEqual(created, [])

    def test_get_status_failure_is_offline(self):
        class Broken:
            def _call(self, action, payload=None):
                raise RuntimeError("down")

        self.assertFalse(launch_watcher.is_online(Broken()))

    def test_start_respects_env_switch(self):
        with patch.dict("os.environ", {"PA_QQ_LAUNCH_WATCH": "0"}, clear=False):
            self.assertFalse(launch_watcher.start())


if __name__ == "__main__":
    unittest.main()
