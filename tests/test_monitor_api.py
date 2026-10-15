"""监控任务接口的归属隔离测试。

全部使用临时数据库和模拟登录身份。
不联网、不调用模型、不发送通知。
"""

import unittest

from tempfile import TemporaryDirectory

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from tests.helper import isolate_paths
from src.auth_api import get_current_user
from src.core import monitor_registry
from src.monitors_api import router


RSS_URL = "https://blog.python.org/rss.xml"


class TestMonitorAPI(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        isolate_paths(self, self.temp)

        self.app = FastAPI()
        self.app.include_router(router)

        def fake_user(request: Request):
            token = request.headers.get(
                "authorization", ""
            )

            users = {
                "Bearer owner-token": {
                    "id": "owner_001",
                    "role": "owner",
                },
                "Bearer admin-token": {
                    "id": "admin_001",
                    "role": "admin",
                },
                "Bearer member-token": {
                    "id": "member_001",
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
            lambda: self.client.__exit__(
                None, None, None
            )
        )

    def headers(self, token="owner-token"):
        return {
            "Authorization": f"Bearer {token}"
        }

    def seed(self, owner_id, url=RSS_URL, interval=30):
        return monitor_registry.create_monitor_task(
            owner_id, url, interval
        )

    def list_ids(self, token="owner-token"):
        body = self.client.get(
            "/monitors",
            headers=self.headers(token),
        ).json()

        return [item["id"] for item in body["monitors"]]

    # 1. 未登录一律拒绝

    def test_requires_login(self):
        self.assertEqual(
            self.client.get("/monitors").status_code,
            401,
        )

        self.assertEqual(
            self.client.post(
                "/monitors",
                json={"source_url": RSS_URL},
            ).status_code,
            401,
        )

    # 2. 普通用户没有监控权限

    def test_member_has_no_access(self):
        self.assertEqual(
            self.client.get(
                "/monitors",
                headers=self.headers("member-token"),
            ).status_code,
            403,
        )

        self.assertEqual(
            self.client.post(
                "/monitors",
                headers=self.headers("member-token"),
                json={"source_url": RSS_URL},
            ).status_code,
            403,
        )

    # 3. 列表只返回自己的任务

    def test_list_returns_only_own(self):
        owner_task = self.seed("owner_001")
        admin_task = self.seed("admin_001")

        self.assertEqual(
            self.list_ids("owner-token"),
            [owner_task],
        )

        self.assertEqual(
            self.list_ids("admin-token"),
            [admin_task],
        )

    # 4. 创建：合法公网地址 + 间隔校验

    def test_owner_can_create(self):
        response = self.client.post(
            "/monitors",
            headers=self.headers(),
            json={
                "source_url": RSS_URL,
                "interval_minutes": 60,
            },
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["status"], "active")

        self.assertEqual(
            self.list_ids(),
            [response.json()["id"]],
        )

        self.assertEqual(
            response.headers["cache-control"],
            "no-store",
        )

    def test_interval_bounds(self):
        for interval in (5, 0, 1441, 24 * 60 + 1):
            with self.subTest(interval=interval):
                response = self.client.post(
                    "/monitors",
                    headers=self.headers(),
                    json={
                        "source_url": RSS_URL,
                        "interval_minutes": interval,
                    },
                )

                self.assertEqual(
                    response.status_code, 422
                )

        self.assertEqual(self.list_ids(), [])

    # 5. 创建：SSRF 策略拦住内网地址

    def test_create_rejects_unsafe_urls(self):
        for url in (
            "http://blog.python.org/rss.xml",
            "https://localhost/rss.xml",
            "https://127.0.0.1/rss.xml",
            "https://169.254.169.254/latest/meta-data",
            "https://user:pass@example.com/rss",
            "https://[::1]/rss.xml",
        ):
            with self.subTest(url=url):
                response = self.client.post(
                    "/monitors",
                    headers=self.headers(),
                    json={"source_url": url},
                )

                self.assertEqual(
                    response.status_code, 422
                )

        # 一个都不许落地
        self.assertEqual(self.list_ids(), [])

    # 6. 管理员能看不能建

    def test_admin_cannot_create(self):
        response = self.client.post(
            "/monitors",
            headers=self.headers("admin-token"),
            json={"source_url": RSS_URL},
        )

        self.assertEqual(response.status_code, 403)

        self.assertEqual(
            self.list_ids("admin-token"), []
        )

    # 7. 暂停与恢复

    def test_pause_and_resume_own_task(self):
        task_id = self.seed("owner_001")

        paused = self.client.post(
            f"/monitors/{task_id}/pause",
            headers=self.headers(),
        )

        self.assertEqual(paused.status_code, 200)
        self.assertEqual(
            paused.json()["status"], "paused"
        )

        resumed = self.client.post(
            f"/monitors/{task_id}/resume",
            headers=self.headers(),
        )

        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(
            resumed.json()["status"], "active"
        )

    # 8. 别人的任务：看不到也动不了

    def test_cannot_touch_other_users_task(self):
        other_id = self.seed("owner_001")

        for path, method in (
            (f"/monitors/{other_id}/pause", "post"),
            (f"/monitors/{other_id}/resume", "post"),
            (f"/monitors/{other_id}", "delete"),
        ):
            with self.subTest(path=path):
                response = getattr(
                    self.client, method
                )(
                    path,
                    headers=self.headers("admin-token"),
                )

                self.assertEqual(
                    response.status_code, 404
                )

        # 原任务没有被改动
        monitors = monitor_registry.list_monitor_tasks(
            "owner_001"
        )

        self.assertEqual(len(monitors), 1)
        self.assertEqual(monitors[0]["status"], "active")

    # 9. 归档后从列表消失

    def test_archive_own_task(self):
        task_id = self.seed("owner_001")

        archived = self.client.delete(
            f"/monitors/{task_id}",
            headers=self.headers(),
        )

        self.assertEqual(archived.status_code, 200)
        self.assertEqual(
            archived.json()["status"], "archived"
        )

        self.assertEqual(self.list_ids(), [])

    # 10. 不存在的任务返回 404

    def test_unknown_task_returns_404(self):
        for path, method in (
            ("/monitors/rss_missing/pause", "post"),
            ("/monitors/rss_missing/resume", "post"),
            ("/monitors/rss_missing", "delete"),
        ):
            with self.subTest(path=path):
                response = getattr(
                    self.client, method
                )(
                    path,
                    headers=self.headers(),
                )

                self.assertEqual(
                    response.status_code, 404
                )


if __name__ == "__main__":
    unittest.main()
