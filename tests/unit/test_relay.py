"""接力测试：错误分类、接力顺序、resume/continue、needs_review。"""

import tempfile
import unittest

from pathlib import Path

from src.collab.providers.openai_compat import ProviderError
from src.collab.relay import RelayRunner, classify_error
from src.collab.run_store import RunStore


class FakeProvider:
    def __init__(self, name="fake", text="result", error=None):
        self.name = name
        self.text = text
        self.error = error
        self.calls = 0

    def submit(self, prompt, *, artifact_name, out_dir):
        self.calls += 1

        if self.error is not None:
            raise self.error

        path = Path(out_dir) / artifact_name
        path.write_text(self.text, encoding="utf-8")

        return {"artifact": str(path), "provider": self.name}


class RelayCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.store = RunStore(self.root / "runs.db")

    def runner(self, providers):
        return RelayRunner(
            store=self.store,
            providers=providers,
            artifact_root=self.root / "artifacts",
        )

    def make_run(self, chain=("a", "b")):
        return self.store.create_run("写一篇文章", list(chain))

    def test_success_first_provider(self):
        provider = FakeProvider("a", "hello")
        result = self.runner({"a": provider, "b": FakeProvider("b")}).start(self.make_run())

        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(provider.calls, 1)

    def test_retryable_falls_to_second(self):
        first = FakeProvider("a", error=ProviderError("timeout"))
        second = FakeProvider("b", "second")
        result = self.runner({"a": first, "b": second}).start(self.make_run())

        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["provider"], "b")
        attempts = self.store.attempts(result["run_id"])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["error_class"], "retryable")

    def test_fatal_does_not_relay(self):
        error = ProviderError("401 unauthorized")
        first = FakeProvider("a", error=error)
        second = FakeProvider("b")
        result = self.runner({"a": first, "b": second}).start(self.make_run())

        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error_class"], "fatal")
        self.assertEqual(second.calls, 0)
        self.assertEqual(len(self.store.attempts(result["run_id"])), 1)

    def test_unknown_does_not_relay(self):
        provider = FakeProvider("a", error=ValueError("奇怪错误"))
        result = self.runner({"a": provider, "b": FakeProvider("b")}).start(self.make_run())

        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error_class"], "unknown")

    def test_chain_exhausted(self):
        result = self.runner(
            {
                "a": FakeProvider("a", error=ProviderError("timeout")),
                "b": FakeProvider("b", error=ProviderError("429")),
            }
        ).start(self.make_run())

        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error_class"], "retryable")

    def test_resume_after_failure(self):
        result = self.runner(
            {"a": FakeProvider("a", error=ProviderError("timeout")), "b": FakeProvider("b", error=ProviderError("timeout"))}
        ).start(self.make_run())

        retry = self.runner({"a": FakeProvider("a", "ok"), "b": FakeProvider("b")}).resume(result["run_id"])

        self.assertEqual(retry["state"], "succeeded")
        self.assertEqual(retry["run_id"], result["run_id"])

    def test_continue_after_success(self):
        result = self.runner({"a": FakeProvider("a", "v1")}).start(self.make_run(("a",)))

        second = self.runner({"a": FakeProvider("a", "v2")}).continue_run(result["run_id"], "再补一章")

        self.assertEqual(second["state"], "succeeded")
        self.assertEqual(second["revision"], 2)

    def test_needs_review_does_not_call_provider(self):
        run_id = self.make_run(("a",))
        claimed = self.store.claim(run_id, "w1")
        self.store.step_begin(run_id, 1, "compose", "写一篇文章", claimed["fencing_token"])

        provider = FakeProvider("a")
        result = self.runner({"a": provider}).start(run_id)

        self.assertEqual(provider.calls, 0)
        self.assertIn(result["state"], ("not_claimed", "needs_review"))

    def test_classify_status_codes(self):
        error = ProviderError("x")
        error.status = 401
        self.assertEqual(classify_error(error), "fatal")

        error.status = 429
        self.assertEqual(classify_error(error), "retryable")


if __name__ == "__main__":
    unittest.main()
