"""Orchestrator tests：状态机、fail-closed、返工上限。"""

import json
import tempfile
import unittest

from pathlib import Path

from src.collab.dsh_runner import RunResult
from src.collab.orchestrator import (
    Orchestrator,
    QwenResult,
    read_verdict,
)


PLAN = "计划正文。" * 40


class FakeQwen:
    def __init__(self, plans=None, verdicts=None):
        self.plans = list(plans or [PLAN])
        self.verdicts = list(verdicts or ["VERDICT: PASS"])
        self.calls = []
        self.timeouts = []

    def __call__(self, prompt, workspace, *, timeout=600):
        self.calls.append(prompt)
        self.timeouts.append(timeout)

        if "验收者" in prompt:
            return QwenResult(True, "验收理由\n" + self.verdicts.pop(0), 0, 0.1)

        text = self.plans.pop(0) if self.plans else PLAN

        return QwenResult(True, text, 0, 0.1)


class FakeDsh:
    def __init__(self, verdicts=None, writes_code=True, ok=True):
        self.verdicts = list(verdicts or ["PASS"])
        self.writes_code = writes_code
        self.ok = ok
        self.calls = []
        self.timeouts = []

    def __call__(self, prompt, workspace, *, timeout=600, extra_env=None):
        self.calls.append(prompt)
        self.timeouts.append(timeout)
        workspace = Path(workspace)

        if "评审" in prompt:
            (workspace / "review.md").write_text(
                "评审意见\nVERDICT: " + self.verdicts.pop(0) + "\n", encoding="utf-8"
            )
        elif "src/" in prompt:
            if self.writes_code:
                (workspace / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")

        return RunResult(ok=self.ok, text="done", reason="completed", exit_code=0, elapsed_s=0.1)


class OrchestratorCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name) / "runs"
        self.notes = []

    def build(self, qwen=None, dsh=None):
        return Orchestrator(
            root=self.root,
            qwen=qwen or FakeQwen(),
            dsh=dsh or FakeDsh(),
            notifier=self.notes.append,
        )

    def test_happy_path_writes_all_artifacts(self):
        orchestrator = self.build()
        state = orchestrator.create("做一个扫雷")

        final = orchestrator.run(state["run_id"])
        directory = orchestrator.run_dir(state["run_id"])

        self.assertEqual(final["state"], "done")
        self.assertTrue((directory / "plan.md").is_file())
        self.assertTrue((directory / "review.md").is_file())
        self.assertTrue((directory / "verdict.md").is_file())
        self.assertTrue((directory / "src" / "main.py").is_file())
        self.assertEqual(len(final["artifacts"]), 1)
        self.assertTrue(any("完成" in note for note in self.notes))

    def test_failed_review_triggers_revision(self):
        qwen = FakeQwen(plans=[PLAN, PLAN + "修订"])
        dsh = FakeDsh(verdicts=["FAIL", "PASS"])
        orchestrator = self.build(qwen=qwen, dsh=dsh)
        state = orchestrator.create("做一个扫雷")

        final = orchestrator.run(state["run_id"])

        self.assertEqual(final["state"], "done")
        self.assertEqual(final["revisions"], 1)
        self.assertEqual(len(dsh.calls), 3)  # review(FAIL) + review(PASS) + coding

    def test_max_revisions_stops(self):
        dsh = FakeDsh(verdicts=["FAIL", "FAIL", "FAIL"])
        orchestrator = self.build(dsh=dsh)
        state = orchestrator.create("做一个扫雷")

        final = orchestrator.run(state["run_id"])

        self.assertEqual(final["state"], "stopped")
        self.assertEqual(final["stop_reason"], "max_revisions")
        self.assertEqual(final["revisions"], 2)

    def test_missing_verdict_is_fail_closed(self):
        class SilentDsh(FakeDsh):
            def __call__(self, prompt, workspace, *, timeout=600, extra_env=None):
                workspace = Path(workspace)
                (workspace / "review.md").write_text("只有意见，没有结论\n", encoding="utf-8")

                return RunResult(ok=True, text="done", reason="completed", exit_code=0)

        orchestrator = self.build(dsh=SilentDsh())
        state = orchestrator.create("做一个扫雷")

        final = orchestrator.run(state["run_id"])

        self.assertEqual(final["state"], "stopped")
        self.assertEqual(final["stop_reason"], "review_no_verdict")

    def test_acceptance_fail_stops_without_recoding(self):
        qwen = FakeQwen(verdicts=["VERDICT: FAIL"])
        dsh = FakeDsh()
        orchestrator = self.build(qwen=qwen, dsh=dsh)
        state = orchestrator.create("做一个扫雷")

        final = orchestrator.run(state["run_id"])

        self.assertEqual(final["state"], "stopped")
        self.assertEqual(final["stop_reason"], "acceptance_failed")
        self.assertEqual(len(dsh.calls), 2)  # 只有 review + coding，没有第二次编码

    def test_coding_without_artifacts_stops(self):
        dsh = FakeDsh(writes_code=False)
        orchestrator = self.build(dsh=dsh)
        state = orchestrator.create("做一个扫雷")

        final = orchestrator.run(state["run_id"])

        self.assertEqual(final["state"], "stopped")
        self.assertEqual(final["stop_reason"], "code_failed")

    def test_failed_dsh_step_stops(self):
        dsh = FakeDsh(ok=False)
        dsh.ok = False
        orchestrator = self.build(dsh=dsh)
        state = orchestrator.create("做一个扫雷")

        final = orchestrator.run(state["run_id"])

        self.assertEqual(final["state"], "stopped")
        self.assertEqual(final["stop_reason"], "review_no_verdict")

    def test_status_returns_latest(self):
        orchestrator = self.build()
        state = orchestrator.create("做一个扫雷")

        info = orchestrator.status()

        self.assertEqual(info["run_id"], state["run_id"])
        self.assertEqual(info["state"], "created")

    def test_per_step_timeouts(self):
        qwen = FakeQwen()
        dsh = FakeDsh()
        orchestrator = self.build(qwen=qwen, dsh=dsh)
        state = orchestrator.create("做一个扫雷")

        orchestrator.run(state["run_id"])

        self.assertIn(600.0, qwen.timeouts)   # planning
        self.assertIn(900.0, qwen.timeouts)   # verifying
        self.assertIn(1800.0, dsh.timeouts)   # coding

    def test_status_reports_stop_reason(self):
        orchestrator = self.build(dsh=FakeDsh(verdicts=["FAIL", "FAIL", "FAIL"]))
        state = orchestrator.create("做一个扫雷")

        orchestrator.run(state["run_id"])
        info = orchestrator.status()

        self.assertEqual(info["state"], "stopped")
        self.assertEqual(info["stop_reason"], "max_revisions")
        self.assertIn("max_revisions", info["message"])

    def test_stop_notification_mentions_artifacts(self):
        orchestrator = self.build(dsh=FakeDsh(writes_code=False))
        state = orchestrator.create("做一个扫雷")

        orchestrator.run(state["run_id"])

        self.assertTrue(
            any("停止" in note and "产物" in note for note in self.notes)
        )


class VerdictCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.path = Path(self._temporary.name) / "review.md"

    def test_missing_file_is_none(self):
        self.assertIsNone(read_verdict(self.path))

    def test_no_match_is_none(self):
        self.path.write_text("没有结论\n", encoding="utf-8")
        self.assertIsNone(read_verdict(self.path))

    def test_last_match_wins_and_lowercase_ok(self):
        self.path.write_text("VERDICT: FAIL\n再想想\nverdict: pass\n", encoding="utf-8")
        self.assertEqual(read_verdict(self.path), "PASS")

    def test_trailing_text_after_verdict_does_not_match(self):
        self.path.write_text("VERDICT: PASS（但有保留）\n", encoding="utf-8")
        self.assertIsNone(read_verdict(self.path))


class FakePlatformRun:
    """platform_runner.run_coding 的替身：记录调用、写产物、回填平台字段。"""

    def __init__(self, ok=True, error="", tampered=False):
        self.ok = ok
        self.error = error
        self.tampered = tampered
        self.calls = []

    def __call__(self, prompt, run_dir, *, run_id, timeout=1800.0,
                 extra_env=None, attempt=1, **_ignored):
        self.calls.append({
            "prompt": prompt, "run_id": run_id,
            "timeout": timeout, "extra_env": extra_env, "attempt": attempt,
        })

        if self.ok:
            target = Path(run_dir) / "src"
            target.mkdir(parents=True, exist_ok=True)
            (target / "main.py").write_text("print('hi')\n", encoding="utf-8")

        result = RunResult(
            ok=self.ok, text="done" if self.ok else "",
            reason="completed" if self.ok else "",
            exit_code=0 if self.ok else None,
            elapsed_s=0.1, error=self.error,
        )
        result.sandbox_id = "sbx_" + "1" * 32
        result.exec_id = "exec_fake"
        result.platform_status = "succeeded" if self.ok else "timed_out"
        result.plan_tampered = self.tampered

        return result


class PlatformPathCase(OrchestratorCase):
    """PERSONAL_AGENT_ORCH_PLATFORM=1：coding 走平台通道；默认走旧路径。"""

    def build(self, qwen=None, dsh=None, platform_run=None):
        return Orchestrator(
            root=self.root,
            qwen=qwen or FakeQwen(),
            dsh=dsh or FakeDsh(),
            platform_run=platform_run or FakePlatformRun(),
            notifier=self.notes.append,
        )

    def test_flag_off_uses_host_dsh(self):
        import os
        from unittest.mock import patch

        dsh = FakeDsh()
        platform = FakePlatformRun()
        orchestrator = self.build(dsh=dsh, platform_run=platform)

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PERSONAL_AGENT_ORCH_PLATFORM", None)
            state = orchestrator.create("做一个扫雷")
            final = orchestrator.run(state["run_id"])

        self.assertEqual(final["state"], "done")
        self.assertEqual(platform.calls, [])
        # review + coding 都走宿主 dsh
        self.assertEqual(len(dsh.calls), 2)
        self.assertNotIn("platform", final["steps"]["coding"])

    def test_flag_on_uses_platform_runner(self):
        import os
        from unittest.mock import patch

        dsh = FakeDsh()
        platform = FakePlatformRun()
        orchestrator = self.build(dsh=dsh, platform_run=platform)

        with patch.dict(
            os.environ, {"PERSONAL_AGENT_ORCH_PLATFORM": "1"}, clear=False
        ):
            state = orchestrator.create("做一个扫雷")
            final = orchestrator.run(state["run_id"])

        self.assertEqual(final["state"], "done")
        self.assertEqual(len(platform.calls), 1)
        # review 仍走宿主 dsh，coding 走平台
        self.assertEqual(len(dsh.calls), 1)

        coding = final["steps"]["coding"]
        self.assertEqual(coding["platform"]["exec_id"], "exec_fake")
        self.assertEqual(coding["platform"]["status"], "succeeded")
        self.assertEqual(platform.calls[0]["attempt"], 1)

    def test_platform_failure_maps_to_step_error(self):
        import os
        from unittest.mock import patch

        platform = FakePlatformRun(ok=False, error="code_timeout")
        orchestrator = self.build(platform_run=platform)

        with patch.dict(
            os.environ, {"PERSONAL_AGENT_ORCH_PLATFORM": "1"}, clear=False
        ):
            state = orchestrator.create("做一个扫雷")
            final = orchestrator.run(state["run_id"])

        self.assertEqual(final["state"], "stopped")
        self.assertEqual(final["stop_reason"], "code_timeout")
        self.assertEqual(final["steps"]["coding"]["error"], "code_timeout")
        self.assertEqual(
            final["steps"]["coding"]["platform"]["status"], "timed_out"
        )

    def test_plan_tampered_warns_but_continues(self):
        import os
        from unittest.mock import patch

        platform = FakePlatformRun(tampered=True)
        orchestrator = self.build(platform_run=platform)

        with patch.dict(
            os.environ, {"PERSONAL_AGENT_ORCH_PLATFORM": "1"}, clear=False
        ):
            state = orchestrator.create("做一个扫雷")
            final = orchestrator.run(state["run_id"])

        self.assertEqual(final["state"], "done")
        self.assertTrue(
            final["steps"]["coding"]["plan_tampered_warning"]
        )


if __name__ == "__main__":
    unittest.main()
