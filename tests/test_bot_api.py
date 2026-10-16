"""Bot 管理接口（S2）的 HTTP 测试。

只使用临时数据库和模拟身份，不调用真实模型。
"""

import unittest

from tempfile import TemporaryDirectory

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from tests.helper import isolate_paths
from src.auth_api import get_current_user
from src.bot_api import router
from src.core.bot_store import BotStore


class TestBotAPI(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        temporary = isolate_paths(self, self.temp)
        self.db_path = temporary.bots_db

        self.store = BotStore(self.db_path)

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

    def create(self, token="alice-token", **kw):
        return self.client.post(
            "/bots",
            headers=self.headers(token),
            json={
                "name": kw.get("name", "助手"),
                "persona": kw.get("persona", "谨慎的风险厌恶者"),
                "system_prompt": kw.get(
                    "system_prompt", "在 persona 立场下发言"
                ),
                "api_source": kw.get("api_source", "platform"),
                "avatar": kw.get("avatar", "default"),
            },
        )

    def seed_platform(self, name="站长助手"):
        return self.store.seed_platform_bot(
            name=name,
            persona="中立综合者",
            system_prompt="汇总观点引导决策",
        )

    def test_create_bot(self):
        result = self.create()

        self.assertEqual(result.status_code, 200)

        body = result.json()
        self.assertEqual(body["name"], "助手")
        self.assertEqual(body["owner_id"], "alice")
        self.assertTrue(body["enabled"])

    def test_list_requires_authentication(self):
        result = self.client.get("/bots")

        self.assertEqual(result.status_code, 401)

    def test_list_shows_platform_and_own(self):
        self.seed_platform()
        self.create()

        result = self.client.get("/bots", headers=self.headers())

        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(result.json()["bots"]), 2)

    def test_create_capped_at_three(self):
        for index in range(3):
            self.create(name=f"bot-{index}")

        result = self.create(name="bot-4")

        self.assertEqual(result.status_code, 422)

    def test_invalid_api_source_rejected(self):
        result = self.create(api_source="ghost")

        self.assertEqual(result.status_code, 422)

    def test_custom_avatar(self):
        result = self.create(avatar="qwen")

        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["avatar"], "qwen")

    def test_update_own_bot(self):
        created = self.create().json()

        result = self.client.patch(
            f"/bots/{created['id']}",
            headers=self.headers(),
            json={"persona": "激进的乐观主义者"},
        )

        self.assertEqual(result.status_code, 200)

    def test_cannot_update_others_bot(self):
        created = self.create().json()  # alice 的 bot

        result = self.client.patch(
            f"/bots/{created['id']}",
            headers=self.headers("bob-token"),
            json={"name": "篡改"},
        )

        self.assertEqual(result.status_code, 404)

    def test_patch_requires_a_field(self):
        created = self.create().json()

        result = self.client.patch(
            f"/bots/{created['id']}",
            headers=self.headers(),
            json={},
        )

        self.assertEqual(result.status_code, 422)

    def test_disable_and_enable(self):
        created = self.create().json()

        disabled = self.client.post(
            f"/bots/{created['id']}/disable",
            headers=self.headers(),
        )
        self.assertEqual(disabled.status_code, 200)

        enabled = self.client.post(
            f"/bots/{created['id']}/enable",
            headers=self.headers(),
        )
        self.assertEqual(enabled.status_code, 200)


if __name__ == "__main__":
    unittest.main()
