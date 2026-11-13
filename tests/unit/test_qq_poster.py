"""海报管线测试：路由、契约、出图、发送（Provider/Edge/网络全 mock）。"""

import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from src.qq import router
from src.qq.inbound import QQInbound
from src.qq.intents import check_contract, get_spec
from src.qq.onebot import OneBotClient, OneBotError
from src.qq.outbox import QQOutbox
from src.qq.poster import (
    PosterError,
    build_poster_prompt,
    make_poster,
    render_png,
)
from src.qq.worker import TaskWorker


class RouterPosterCase(unittest.TestCase):
    def test_poster_keyword(self):
        for text in ("做个北京工资海报", "来一张宣传图", "汇总成长图", "一图读懂医保"):
            decision = router.classify(text)

            self.assertEqual(decision["intent"], "poster", text)
            self.assertEqual(decision["action"], "execute", text)
            self.assertEqual(decision["params"]["topic"], text, text)

    def test_draw_still_unavailable(self):
        for text in ("画一只猫", "生成一张图给我"):
            decision = router.classify(text)

            self.assertEqual(decision["action"], "unavailable", text)


class PosterContractCase(unittest.TestCase):
    def test_registered(self):
        spec = get_spec("poster")

        self.assertIsNotNone(spec)
        self.assertTrue(spec.enabled)
        self.assertEqual(spec.output_kind, "image")

    def test_valid_params(self):
        _, validated, error = check_contract("poster", {"topic": "北京工资海报"})

        self.assertEqual(error, "")
        self.assertEqual(validated["topic"], "北京工资海报")

    def test_reject_bad_params(self):
        _, _, error = check_contract("poster", {"topic": "x"})
        self.assertEqual(error, "invalid_params")

        _, _, error = check_contract("poster", {"topic": "ok 海报", "hack": 1})
        self.assertEqual(error, "invalid_params")


class OneBotImageCase(unittest.TestCase):
    def test_missing_file(self):
        client = OneBotClient(base_url="http://127.0.0.1:3001", token="t")

        with self.assertRaises(OneBotError):
            client.send_private_image(7, "no-such-file.png")

    def test_payload(self):
        client = OneBotClient(base_url="http://127.0.0.1:3001", token="t")
        calls = []
        client._call = lambda action, payload=None: calls.append((action, payload)) or {}

        with tempfile.TemporaryDirectory() as temporary:
            png = Path(temporary) / "p.png"
            png.write_bytes(b"\x89PNG")
            client.send_private_image(7, str(png))

        action, payload = calls[0]
        self.assertEqual(action, "send_msg")
        self.assertEqual(payload["message_type"], "private")
        self.assertEqual(payload["user_id"], 7)
        segment = payload["message"][0]
        self.assertEqual(segment["type"], "image")
        self.assertTrue(segment["data"]["file"].startswith("file:///"))


class FakeProvider:
    configured = True

    def __init__(self, html="<html><body>海报</body></html>", error=None):
        self.html = html
        self.error = error

    def submit(self, prompt, *, artifact_name, out_dir):
        if self.error:
            raise RuntimeError(self.error)

        target = Path(out_dir) / artifact_name
        target.write_text(self.html, encoding="utf-8")

        return {"artifact": str(target)}


class MakePosterCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def test_prompt_contains_topic_and_rules(self):
        prompt = build_poster_prompt("北京工资海报")

        self.assertIn("北京工资海报", prompt)
        self.assertIn("禁止引用任何外部资源", prompt)
        self.assertIn("未联网核实", prompt)

    def test_happy_path(self):
        def fake_renderer(html_path, png_path):
            Path(png_path).write_bytes(b"\x89PNG")

            return Path(png_path)

        png = make_poster(
            "北京工资海报", "m1",
            provider=FakeProvider(), root=self.root, renderer=fake_renderer,
        )

        self.assertTrue(png.is_file())
        self.assertTrue((self.root / "posters" / "m1" / "poster.html").is_file())

    def test_provider_not_configured(self):
        provider = FakeProvider()
        provider.configured = False

        with self.assertRaises(PosterError):
            make_poster("海报", "m2", provider=provider, root=self.root)

    def test_provider_failure(self):
        with self.assertRaises(PosterError) as ctx:
            make_poster("海报", "m3", provider=FakeProvider(error="HTTP 500"), root=self.root)

        self.assertIn("模型生成失败", str(ctx.exception))

    def test_invalid_html(self):
        with self.assertRaises(PosterError):
            make_poster("海报", "m4", provider=FakeProvider(html="不是HTML"), root=self.root)

    def test_strips_preamble_and_fence(self):
        """模型带开场白 + ```html 围栏时，落盘的必须是抠干净的 HTML。"""

        messy = (
            "这是可直接运行的HTML代码，生成一张海报。\n"
            "```html\n<html><body>海报</body></html>\n```"
        )

        def fake_renderer(html_path, png_path):
            text = Path(html_path).read_text(encoding="utf-8")
            assert text == "<html><body>海报</body></html>", text
            Path(png_path).write_bytes(b"\x89PNG")

            return Path(png_path)

        png = make_poster(
            "海报", "m5",
            provider=FakeProvider(html=messy), root=self.root, renderer=fake_renderer,
        )

        self.assertTrue(png.is_file())

    def test_extract_without_doctype(self):
        from src.qq.poster import extract_html

        self.assertEqual(
            extract_html("前言<html><body>x</body></html>后记"),
            "<html><body>x</body></html>",
        )
        self.assertEqual(extract_html("完全没有"), "")

    def test_render_without_edge(self):
        html = self.root / "a.html"
        html.write_text("<html></html>", encoding="utf-8")

        with patch("src.qq.poster.find_edge", return_value=""):
            with self.assertRaises(PosterError):
                render_png(html, self.root / "a.png")

    def test_render_success(self):
        html = self.root / "a.html"
        html.write_text("<html></html>", encoding="utf-8")
        png = self.root / "a.png"

        def fake_run(command, **kwargs):
            target = next(
                item.split("=", 1)[1] for item in command if item.startswith("--screenshot=")
            )
            Path(target).write_bytes(b"\x89PNG")

        with patch("src.qq.poster.find_edge", return_value="edge"), patch(
            "src.qq.poster.subprocess.run", side_effect=fake_run
        ):
            result = render_png(html, png)

        self.assertEqual(result, png)


class FakeClient:
    def __init__(self):
        self.sent = []
        self.images = []

    def send_private_msg(self, user_id, text):
        self.sent.append((user_id, text))

        return {"message_id": 1}

    def send_private_image(self, user_id, file_path):
        self.images.append((user_id, file_path))

        return {"message_id": 2}


class WorkerPosterCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.outbox = QQOutbox(self.root / "outbox.db")
        self.client = FakeClient()
        self.handler = QQInbound(owner_id=7, secret="s", client=self.client, enabled=True)
        self.outbox.enqueue(
            message_id="1", sender=7, raw_text="北京工资海报", queue="task"
        )

    def worker(self):
        return TaskWorker(outbox=self.outbox, client=self.client, handler=self.handler)

    def test_poster_sends_image(self):
        png = self.root / "p.png"
        png.write_bytes(b"\x89PNG")

        with patch(
            "src.collab.providers.provider_name", return_value="ark"
        ), patch(
            "src.qq.poster.make_poster", return_value=png
        ):
            self.worker().run_once()

        self.assertEqual(len(self.client.images), 1)
        self.assertEqual(self.client.images[0], (7, str(png)))
        self.assertTrue(any("口径" in text for _, text in self.client.sent))
        self.assertEqual(self.outbox.get("1")["state"], "sent")
        self.assertEqual(self.outbox.get("1")["intent"], "poster")

    def test_poster_failure_replies_text(self):
        with patch(
            "src.collab.providers.provider_name", return_value="ark"
        ), patch(
            "src.qq.poster.make_poster",
            side_effect=PosterError("Edge 渲染超时"),
        ):
            self.worker().run_once()

        self.assertEqual(len(self.client.images), 0)
        self.assertTrue(any("海报生成失败" in text for _, text in self.client.sent))
        self.assertEqual(self.outbox.get("1")["state"], "sent")

    def test_poster_rejects_agent_mode(self):
        with patch(
            "src.collab.providers.provider_name", return_value="qwen-agent"
        ):
            self.worker().run_once()

        self.assertEqual(len(self.client.images), 0)
        self.assertTrue(any("agent" in text for _, text in self.client.sent))


if __name__ == "__main__":
    unittest.main()
