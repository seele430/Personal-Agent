"""真实 Provider（OpenAI 兼容端点）单测：全 mock，不联网。"""

import io
import json
import tempfile
import unittest
import urllib.error

from pathlib import Path
from unittest.mock import patch

from src.collab.providers import openai_compat as mod
from src.collab.providers.openai_compat import OpenAICompatProvider, ProviderError


def response(payload):
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


class FakeOpener:
    def __init__(self, payload=None, *, error=None):
        self.payload = payload
        self.error = error
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)

        if self.error:
            raise self.error

        class Ctx:
            def __enter__(self_inner):
                return response(self.payload)

            def __exit__(self_inner, *args):
                return False

        return Ctx()


class ProviderCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.out = Path(self._temporary.name)

    def provider(self, opener, key="k" * 20):
        return OpenAICompatProvider(
            api_key=key,
            base_url="https://example.invalid",
            model="deepseek-flash",
            opener=opener,
        )

    def payload(self, content="正文内容", total=120):
        return {
            "id": "chatcmpl-1",
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 80, "completion_tokens": 40, "total_tokens": total},
        }


class TestSuccess(ProviderCase):
    def test_writes_artifact_and_reports_usage(self):
        opener = FakeOpener(self.payload())
        result = self.provider(opener).submit(
            "写一段说明", artifact_name="report.md", out_dir=self.out
        )

        self.assertEqual(result["task_id"], "chatcmpl-1")
        self.assertEqual(result["usage"]["total"], 120)
        self.assertEqual(
            (self.out / "report.md").read_text(encoding="utf-8"), "正文内容"
        )

    def test_request_caps_tokens_and_streams_false(self):
        opener = FakeOpener(self.payload())
        self.provider(opener).submit("x", artifact_name="a.md", out_dir=self.out)

        body = json.loads(opener.requests[0].data.decode("utf-8"))

        self.assertEqual(body["max_tokens"], mod.MAX_TOKENS)
        self.assertFalse(body["stream"])
        self.assertEqual(body["model"], "deepseek-flash")

    def test_authorization_header_present(self):
        opener = FakeOpener(self.payload())
        self.provider(opener).submit("x", artifact_name="a.md", out_dir=self.out)

        headers = {k.lower(): v for k, v in opener.requests[0].headers.items()}

        self.assertIn("authorization", headers)

    def test_length_finish_marks_truncation(self):
        payload = self.payload(content="半句话")
        payload["choices"][0]["finish_reason"] = "length"
        result = self.provider(FakeOpener(payload)).submit(
            "x", artifact_name="a.md", out_dir=self.out
        )

        self.assertTrue(result["truncated"])
        text = (self.out / "a.md").read_text(encoding="utf-8")
        self.assertIn(mod.TRUNCATION_MARKER, text)

    def test_stop_finish_is_complete(self):
        payload = self.payload()
        payload["choices"][0]["finish_reason"] = "stop"
        result = self.provider(FakeOpener(payload)).submit(
            "x", artifact_name="a.md", out_dir=self.out
        )

        self.assertFalse(result["truncated"])

    def test_env_overrides_max_tokens(self):
        opener = FakeOpener(self.payload())

        with patch.dict("os.environ", {mod.MAX_TOKENS_ENV: "999"}, clear=False):
            self.provider(opener).submit("x", artifact_name="a.md", out_dir=self.out)

        body = json.loads(opener.requests[0].data.decode("utf-8"))

        self.assertEqual(body["max_tokens"], 999)

    def test_reasoning_exhausted_is_explained(self):
        payload = self.payload(content="")
        payload["choices"][0]["finish_reason"] = "length"
        payload["usage"]["completion_tokens_details"] = {"reasoning_tokens": 400}

        with self.assertRaises(ProviderError) as caught:
            self.provider(FakeOpener(payload)).submit(
                "x", artifact_name="a.md", out_dir=self.out
            )

        self.assertIn("思考", str(caught.exception))


class TestErrors(ProviderCase):
    def test_missing_key_refuses_without_request(self):
        opener = FakeOpener(self.payload())
        provider = self.provider(opener, key="")

        with self.assertRaises(ProviderError) as caught:
            provider.submit("x", artifact_name="a.md", out_dir=self.out)

        self.assertIn("OPENAI_API_KEY", str(caught.exception))
        self.assertEqual(opener.requests, [])

    def test_http_error_is_chinese_and_has_no_key(self):
        opener = FakeOpener(error=urllib.error.HTTPError(
            "https://example.invalid", 401, "unauth", {}, io.BytesIO(b'{"error":"bad key"}')
        ))

        with self.assertRaises(ProviderError) as caught:
            self.provider(opener, key="secret-key-value").submit(
                "x", artifact_name="a.md", out_dir=self.out
            )

        message = str(caught.exception)

        self.assertIn("HTTP 401", message)
        self.assertNotIn("secret-key-value", message)

    def test_empty_content_rejected(self):
        opener = FakeOpener(self.payload(content="   "))

        with self.assertRaises(ProviderError):
            self.provider(opener).submit("x", artifact_name="a.md", out_dir=self.out)

    def test_missing_choices_rejected(self):
        opener = FakeOpener({"id": "x", "choices": []})

        with self.assertRaises(ProviderError) as caught:
            self.provider(opener).submit("x", artifact_name="a.md", out_dir=self.out)

        self.assertIn("choices", str(caught.exception))

    def test_not_json_rejected(self):
        class BadOpener:
            def __call__(self, request, timeout=None):
                class Ctx:
                    def __enter__(self_inner):
                        return io.BytesIO(b"<html>")

                    def __exit__(self_inner, *args):
                        return False

                return Ctx()

        with self.assertRaises(ProviderError):
            self.provider(BadOpener()).submit("x", artifact_name="a.md", out_dir=self.out)

    def test_empty_prompt_rejected(self):
        opener = FakeOpener(self.payload())

        with self.assertRaises(ProviderError):
            self.provider(opener).submit("  ", artifact_name="a.md", out_dir=self.out)

        self.assertEqual(opener.requests, [])


if __name__ == "__main__":
    unittest.main()
