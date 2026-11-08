"""模拟 Provider 测试：真实 HTTP（本地回环），dely 极短。"""

import http.client
import json
import socket
import threading
import time
import unittest

import scripts.mock_provider as provider


class ProviderCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 受保护运行器会阻断真实网络（含 127.0.0.1），此时跳过而不是失败：
        # 这组用例的价值在独立运行时验证 mock provider 的协议实现。
        try:
            cls.server = provider.serve(0, delay=0.05)

        except Exception as error:  # noqa: BLE001 - 守卫抛的是 TestSafetyViolation
            raise unittest.SkipTest(f"当前环境禁止监听端口：{error}") from None

        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

        # 受保护运行器允许 bind 但阻断 connect：试连一次，连不上就跳过整组
        try:
            probe = socket.create_connection(("127.0.0.1", cls.port), timeout=2)
            probe.close()

        except Exception as error:  # noqa: BLE001 - 守卫抛的是 TestSafetyViolation
            cls.server.shutdown()
            cls.server.server_close()
            cls.server = None
            raise unittest.SkipTest(f"当前环境禁止回环连接：{error}") from None

    @classmethod
    def tearDownClass(cls):
        server = getattr(cls, "server", None)

        if server is None:
            return

        server.shutdown()
        server.server_close()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def _conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)

    def post(self, path, payload):
        connection = self._conn()

        try:
            connection.request(
                "POST",
                path,
                body=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()

            return response.status, json.loads(response.read().decode("utf-8"))

        finally:
            connection.close()

    def get(self, path):
        connection = self._conn()

        try:
            connection.request("GET", path)
            response = connection.getresponse()

            return response.status, response.read().decode("utf-8")

        finally:
            connection.close()

    def submit(self):
        status, payload = self.post("/submit", {"prompt": "hello"})

        self.assertEqual(status, 200)

        return payload["task_id"]

    def test_submit_returns_task_id(self):
        self.assertTrue(self.submit().startswith(""))


class TestFlow(ProviderCase):
    def test_status_transitions_to_succeeded(self):
        task_id = self.submit()
        deadline = time.time() + 5

        while time.time() < deadline:
            _, body = self.get(f"/status/{task_id}")
            payload = json.loads(body)

            if payload["state"] == "succeeded":
                self.assertEqual(len(payload["artifacts"]), 1)

                return

            time.sleep(0.02)

        self.fail("任务没有变成 succeeded")

    def test_unknown_task_404(self):
        status, _ = self.get("/status/nope")

        self.assertEqual(status, 404)

    def test_artifact_content(self):
        task_id = self.submit()
        deadline = time.time() + 5
        name = None

        while time.time() < deadline:
            _, body = self.get(f"/status/{task_id}")
            payload = json.loads(body)

            if payload["state"] == "succeeded":
                name = payload["artifacts"][0]
                break

            time.sleep(0.02)

        self.assertIsNotNone(name)

        status, text = self.get(f"/artifact/{name}")

        self.assertEqual(status, 200)
        self.assertIn("模拟产物", text)
        self.assertIn("hello", text)

    def test_bad_json_rejected(self):
        connection = self._conn()

        try:
            connection.request(
                "POST",
                "/submit",
                body=b"{not json",
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()

            self.assertEqual(response.status, 400)

        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
