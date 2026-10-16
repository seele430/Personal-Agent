"""聊天室接口（S4c）的 HTTP 测试。发言时模型被 patch。"""

import unittest

from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from tests.helper import isolate_paths
from src import agent
from src.auth_api import get_current_user
from src.core.bot_store import BotStore
from src.room_api import router


class TestRoomAPI(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        temporary = isolate_paths(self, self.temp)
        self.bots = BotStore(temporary.bots_db)

        self.app = FastAPI()
        self.app.include_router(router)

        def fake_user(request: Request):
            token = request.headers.get("authorization", "")

            users = {
                "Bearer alice-token": {
                    "id": "alice",
                    "role": "member",
                },
                "Bearer bob-token": {
                    "id": "bob",
                    "role": "member",
                },
            }

            if token not in users:
                raise HTTPException(
                    status_code=401,
                    detail="请先登录",
                )

            return users[token]

        self.app.dependency_overrides[
            get_current_user
        ] = fake_user

        self.client = TestClient(self.app)
        self.client.__enter__()

        self.addCleanup(
            lambda: self.client.__exit__(None, None, None)
        )

    def headers(self, token="alice-token"):
        return {"Authorization": f"Bearer {token}"}

    def make_room(self, name="讨论房", token="alice-token"):
        return self.client.post(
            "/rooms",
            headers=self.headers(token),
            json={"name": name},
        ).json()

    def make_bot(self, owner="alice", name="助手"):
        return self.bots.create_bot(
            owner_id=owner,
            name=name,
            persona="中立",
            system_prompt="就事论事",
            api_source="platform",
        )

    def test_create_and_list_rooms(self):
        created = self.client.post(
            "/rooms",
            headers=self.headers(),
            json={"name": "讨论房"},
        )
        self.assertEqual(created.status_code, 200)

        listed = self.client.get(
            "/rooms", headers=self.headers()
        )
        self.assertEqual(len(listed.json()["rooms"]), 1)

    def test_add_bot_and_list(self):
        room = self.make_room()
        bot = self.make_bot()

        added = self.client.post(
            f"/rooms/{room['id']}/bots",
            headers=self.headers(),
            json={"bot_id": bot["id"]},
        )
        self.assertEqual(added.status_code, 200)

        listed = self.client.get(
            f"/rooms/{room['id']}/bots",
            headers=self.headers(),
        )
        self.assertEqual(len(listed.json()["bots"]), 1)

    def test_send_message_triggers_speech(self):
        room = self.make_room()
        bot = self.make_bot()

        self.client.post(
            f"/rooms/{room['id']}/bots",
            headers=self.headers(),
            json={"bot_id": bot["id"]},
        )

        with patch.object(
            agent, "utter", return_value="一句话观点"
        ):
            sent = self.client.post(
                f"/rooms/{room['id']}/messages",
                headers=self.headers(),
                json={"message": "怎么看？"},
            )

        self.assertEqual(sent.status_code, 200)
        self.assertEqual(len(sent.json()["replies"]), 1)
        self.assertEqual(
            sent.json()["replies"][0]["content"], "一句话观点"
        )

    def test_cannot_access_others_room(self):
        room = self.make_room(name="alice 的房", token="alice-token")

        result = self.client.get(
            f"/rooms/{room['id']}/bots",
            headers=self.headers("bob-token"),
        )

        self.assertEqual(result.status_code, 404)

    def test_decision_requires_summary(self):
        room = self.make_room()
        bot = self.make_bot()
        self.client.post(
            f"/rooms/{room['id']}/bots",
            headers=self.headers(),
            json={"bot_id": bot["id"]},
        )

        with patch.object(agent, "utter", return_value="观点"):
            sent = self.client.post(
                f"/rooms/{room['id']}/messages",
                headers=self.headers(),
                json={"message": "怎么看？"},
            )

        turn_id = sent.json()["turn_id"]

        result = self.client.post(
            f"/rooms/{room['id']}/turns/{turn_id}/decision",
            headers=self.headers(),
            json={"option": "做"},
        )

        self.assertEqual(result.status_code, 409)

    def test_decision_success(self):
        room = self.make_room()
        bot = self.make_bot()
        self.client.post(
            f"/rooms/{room['id']}/bots",
            headers=self.headers(),
            json={"bot_id": bot["id"]},
        )
        self.bots.set_default_bot(user_id="alice", bot_id=bot["id"])

        with patch.object(agent, "utter", return_value="观点"):
            sent = self.client.post(
                f"/rooms/{room['id']}/messages",
                headers=self.headers(),
                json={"message": "怎么看？"},
            )

        turn_id = sent.json()["turn_id"]

        with patch.object(
            agent,
            "host_summary",
            return_value={
                "consensus": "一致",
                "disagreement": "分歧",
                "options": [{"label": "做", "risk": "贵"}],
            },
        ):
            self.client.post(
                f"/rooms/{room['id']}/turns/{turn_id}/summary",
                headers=self.headers(),
            )

        with patch.object(agent, "chat", return_value="已执行"):
            result = self.client.post(
                f"/rooms/{room['id']}/turns/{turn_id}/decision",
                headers=self.headers(),
                json={"option": "做"},
            )

        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["reply"], "已执行")


if __name__ == "__main__":
    unittest.main()
