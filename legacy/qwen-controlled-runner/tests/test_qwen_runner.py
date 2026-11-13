"""Runner 状态机测试（mock 进程，不调用真实模型）。"""

import json
import subprocess
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from src.collab.qwen_runner import QwenRunner, RunSpec, status_of


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "qwen"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeTracking:
    instances = []

    def __init__(self, pid, interval=0.3):
        self.pid = pid
        self.tracked = dict(getattr(FakeTracking, "seed", {}))
        self.ok = True
        self.started = False
        FakeTracking.instances.append(self)

    def start(self):
        self.started = True

        return self

    def stop(self):
        return dict(self.tracked)


class FakeProcess:
    def __init__(self, stdout: str, code: int = 0, timeout: bool = False, reaped: bool = True, interrupt: bool = False):
        self.pid = 4242
        self._stdout = stdout
        self._code = code
        self._timeout = timeout
        self._reaped = reaped
        self._interrupt = interrupt
        self._raised = False
        self.returncode = None

    def communicate(self, input=None, timeout=None):
        if self._interrupt and not self._raised:
            self._raised = True
            raise KeyboardInterrupt()

        if self._timeout and not self._raised:
            self._raised = True
            raise subprocess.TimeoutExpired("qwen", timeout or 1)

        self.returncode = self._code if self._reaped else None

        return self._stdout, ""

    def poll(self):
        return self.returncode


def spec(**overrides):
    base = dict(
        run_id="33333333-3333-3333-3333-333333333333",
        provider_session_id="11111111-1111-1111-1111-111111111111",
        prompt="say ok",
        model="qwen3.8-flash",
        approval_mode="plan",
        expected_artifacts={},
        max_wall_time="30s",
        timeout_seconds=5,
    )
    base.update(overrides)

    return RunSpec(**base)


class RunnerCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.runner = QwenRunner(root=self.root)

    def execute(self, process, run_spec=None, tree=(True, 0, {}), seed=None):
        FakeTracking.seed = seed or {}
        FakeTracking.instances = []

        with patch("src.collab.qwen_runner.subprocess.Popen", return_value=process), patch(
            "src.collab.qwen_runner.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")
        ), patch("src.collab.qwen_runner._wait_tree_gone", return_value=tree), patch(
            "src.collab.qwen_runner._Tracking", FakeTracking
        ), patch(
            "src.collab.qwen_runner._creation_iso", return_value=getattr(self, "creation", "2026-01-01T00:00:00+00:00")
        ):
            return self.runner.execute(run_spec or spec())

    def success_fixture(self):
        data = json.loads(fixture("success.json"))
        data[0]["session_id"] = "11111111-1111-1111-1111-111111111111"
        data[-1]["session_id"] = "11111111-1111-1111-1111-111111111111"

        return json.dumps(data)

    def test_success_becomes_completed_unverified(self):
        state = self.execute(FakeProcess(self.success_fixture()))

        self.assertEqual(state["state"], "completed_unverified")
        self.assertEqual(state["observed_model"], "qwen3.8-flash")
        self.assertTrue((Path(self.root) / "33333333-3333-3333-3333-333333333333" / "state.json").exists())

    def test_nonzero_exit_fails(self):
        state = self.execute(FakeProcess(self.success_fixture(), code=1))

        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["reason"]["code"], "provider_error")

    def test_model_fallback_fails(self):
        data = json.loads(fixture("model_fallback.json"))
        data[0]["session_id"] = "11111111-1111-1111-1111-111111111111"
        data[-1]["session_id"] = "11111111-1111-1111-1111-111111111111"
        state = self.execute(FakeProcess(json.dumps(data)))

        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["reason"]["code"], "model_mismatch")

    def test_session_mismatch_fails(self):
        data = json.loads(self.success_fixture())
        data[-1]["session_id"] = "22222222-2222-2222-2222-222222222222"

        state = self.execute(FakeProcess(json.dumps(data)))

        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["reason"]["code"], "session_mismatch")

    def test_missing_result_fails(self):
        data = [event for event in json.loads(self.success_fixture()) if event.get("type") != "result"]

        state = self.execute(FakeProcess(json.dumps(data)))

        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["reason"]["code"], "missing_result")

    def test_timeout_reaped_is_timeout(self):
        state = self.execute(FakeProcess("", timeout=True, reaped=True))

        self.assertEqual(state["state"], "timeout")
        self.assertTrue(state["cleanup"]["parent_reaped"])
        self.assertTrue(state["cleanup"]["tree_cleanup_confirmed"])

    def test_timeout_not_reaped_is_unknown(self):
        state = self.execute(FakeProcess("", timeout=True, reaped=False))

        self.assertEqual(state["state"], "unknown")
        self.assertEqual(state["reason"]["code"], "cleanup_failed")

    def test_missing_artifact_fails(self):
        state = self.execute(
            FakeProcess(self.success_fixture()),
            run_spec=spec(expected_artifacts={"out.txt": False}),
        )

        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["reason"]["code"], "artifact_missing")

    def test_command_and_env(self):
        command = self.runner.build_command(spec())
        env = self.runner.build_env()

        self.assertIn("--session-id", command)
        self.assertIn("--output-format", command)
        self.assertIn("--max-wall-time", command)
        self.assertNotIn("--no-browser", command)
        self.assertEqual(env["NO_BROWSER"], "1")
        self.assertNotIn("QWEN_API_KEY", env)

    def test_stale_running_reconciled(self):
        workspace = Path(self.root) / "44444444-4444-4444-4444-444444444444"
        workspace.mkdir()
        (workspace / "state.json").write_text(
            json.dumps({"state": "running", "pid": 999999, "run_id": "44444444-4444-4444-4444-444444444444"}),
            encoding="utf-8",
        )

        with patch("src.collab.qwen_runner._pid_alive", return_value=False):
            state = status_of(self.root, "44444444-4444-4444-4444-444444444444")

        self.assertEqual(state["state"], "unknown")
        self.assertEqual(state["reason"]["code"], "stale_running")



    def test_yolo_requires_expected_artifacts(self):
        from src.collab.qwen_runner import RunSpecError, validate_spec

        with self.assertRaises(RunSpecError) as caught:
            validate_spec(spec(approval_mode="yolo"))

        self.assertEqual(caught.exception.code, "expected_artifacts_required")

    def test_non_uuid_run_id_rejected(self):
        from src.collab.qwen_runner import RunSpecError, validate_spec

        with self.assertRaises(RunSpecError):
            validate_spec(spec(run_id="not-a-uuid"))

    def test_existing_run_directory_rejected(self):
        from src.collab.qwen_runner import RunSpecError

        (Path(self.root) / "77777777-7777-7777-7777-777777777777").mkdir(parents=True)

        with self.assertRaises(RunSpecError):
            self.runner.execute(spec(run_id="77777777-7777-7777-7777-777777777777"))

    def test_native_exit_55_is_not_timeout(self):
        state = self.execute(FakeProcess("", code=55))

        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["reason"]["code"], "native_limit")

    def test_kill_failure_is_unknown(self):
        def failing_run(*args, **kwargs):
            return subprocess.CompletedProcess(args, 1, "", "")

        with patch("src.collab.qwen_runner.subprocess.Popen", return_value=FakeProcess("", timeout=True, reaped=True)), patch(
            "src.collab.qwen_runner.subprocess.run", side_effect=failing_run
        ):
            state = self.runner.execute(spec())

        self.assertEqual(state["state"], "unknown")

    def test_popen_failure_is_failed(self):
        with patch("src.collab.qwen_runner.subprocess.Popen", side_effect=OSError("no exe")):
            state = self.runner.execute(spec())

        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["reason"]["code"], "provider_error")

    def test_stale_when_liveness_unknown(self):
        workspace = Path(self.root) / "88888888-8888-8888-8888-888888888888"
        workspace.mkdir()
        (workspace / "state.json").write_text(
            json.dumps({"state": "running", "pid": 1234, "run_id": workspace.name}),
            encoding="utf-8",
        )

        with patch("src.collab.qwen_runner._pid_alive", return_value=None):
            state = status_of(self.root, workspace.name)

        self.assertEqual(state["state"], "unknown")

    def test_missing_model_field_fails(self):
        data = json.loads(self.success_fixture())
        data[0].pop("model", None)
        state = self.execute(FakeProcess(json.dumps(data)))

        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["reason"]["code"], "model_mismatch")



    def test_csv_real_format_pid_in_second_column(self):
        from src.collab.qwen_runner import _pid_exists

        output = '"System","4","Services","0","2,112 K"\n"python.exe","16980","Console","1","20,784 K"\n'

        with patch(
            "src.collab.qwen_runner.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, output, ""),
        ):
            self.assertTrue(_pid_exists(16980))

    def test_pid_123_does_not_match_1234(self):
        from src.collab.qwen_runner import _pid_exists

        output = '"python.exe","1234","Console","1","20,784 K"\n'

        with patch(
            "src.collab.qwen_runner.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, output, ""),
        ):
            self.assertFalse(_pid_exists(123))

    def test_empty_tasklist_means_absent(self):
        from src.collab.qwen_runner import _pid_exists

        with patch(
            "src.collab.qwen_runner.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ):
            self.assertFalse(_pid_exists(123))

    def test_tasklist_failure_is_none(self):
        from src.collab.qwen_runner import _pid_exists

        with patch(
            "src.collab.qwen_runner.subprocess.run",
            return_value=subprocess.CompletedProcess([], 1, "", "error"),
        ):
            self.assertIsNone(_pid_exists(123))

    def test_descendants_tree(self):
        from src.collab.qwen_runner import _descendants

        snapshot = [
            {"pid": 10, "parent": 1, "created": "a"},
            {"pid": 11, "parent": 10, "created": "b"},
            {"pid": 12, "parent": 11, "created": "c"},
            {"pid": 99, "parent": 1, "created": "d"},
        ]

        found = _descendants(10, snapshot)

        self.assertEqual(set(found), {11, 12})

    def test_normal_exit_with_live_descendants_is_unknown(self):
        state = self.execute(FakeProcess(self.success_fixture()), tree=(False, 1, {}))

        self.assertEqual(state["state"], "unknown")
        self.assertEqual(state["reason"]["code"], "cleanup_failed")

    def test_kill_success_but_descendants_remain_unknown(self):
        state = self.execute(FakeProcess("", timeout=True, reaped=True), tree=(False, 2, {}))

        self.assertEqual(state["state"], "unknown")

    def test_ctrl_c_cleanup_success_cancelled(self):
        state = self.execute(FakeProcess("", interrupt=True))

        self.assertEqual(state["state"], "cancelled")

    def test_ctrl_c_cleanup_failure_unknown(self):
        state = self.execute(FakeProcess("", interrupt=True), tree=(False, 1, {}))

        self.assertEqual(state["state"], "unknown")

    def test_creation_mismatch_means_pid_reuse(self):
        from src.collab.qwen_runner import _pid_alive
        from datetime import datetime, timezone

        with patch("src.collab.qwen_runner._pid_exists", return_value=True), patch(
            "src.collab.qwen_runner._process_creation_time",
            return_value=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ):
            self.assertFalse(_pid_alive(123, "2026-01-01T00:10:00+00:00"))



    def test_trailing_garbage_rejected(self):
        state = self.execute(FakeProcess(self.success_fixture() + "garbage"))

        self.assertEqual(state["state"], "failed")

    def test_env_whitelist_drops_secrets(self):
        runner = QwenRunner(root=self.root, env={"PATH": "x", "SECRET_TOKEN": "leak"})

        self.assertNotIn("SECRET_TOKEN", runner.build_env())

    def test_fsync_failure_raises(self):
        from src.collab.qwen_workspace import RunWorkspace

        workspace = RunWorkspace(self.root, "99999999-9999-9999-9999-999999999999").create()

        with patch("src.collab.qwen_workspace.os.fsync", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                workspace.write_state({"state": "running"})

    def test_illegal_transition_rejected(self):
        from src.collab.qwen_runner import transition

        state = {"state": "completed_unverified"}

        with self.assertRaises(RuntimeError):
            transition(state, "timeout")

    def test_model_alias_accepted(self):
        data = json.loads(self.success_fixture())
        data[0]["model"] = "qwen3.8-max"
        state = self.execute(
            FakeProcess(json.dumps(data)),
            run_spec=spec(model="flash-alias", model_aliases={"flash-alias": "qwen3.8-max"}),
        )

        self.assertEqual(state["state"], "completed_unverified")

    def test_junction_rejected(self):
        import subprocess as sp
        from src.collab.qwen_workspace import ArtifactError, RunWorkspace

        run_id = "55555555-5555-5555-5555-555555555555"
        workspace = RunWorkspace(self.root, run_id).create()
        target = Path(self.root) / "outside"
        target.mkdir()
        (target / "x.txt").write_text("x", encoding="utf-8")
        link = workspace.workspace / "link"

        result = sp.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)

        if result.returncode != 0:
            self.fail(f"junction 创建失败：{result.stderr.decode('utf-8', 'replace')[:200]}")

        with self.assertRaises(ArtifactError):
            workspace.scan()

    def test_junction_in_intermediate_directory(self):
        import subprocess as sp
        from src.collab.qwen_workspace import ArtifactError, RunWorkspace

        run_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        workspace = RunWorkspace(self.root, run_id).create()
        target = Path(self.root) / "outside2"
        target.mkdir()
        (target / "y.txt").write_text("y", encoding="utf-8")
        (workspace.workspace / "sub").mkdir()
        link = workspace.workspace / "sub" / "link"

        result = sp.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)

        if result.returncode != 0:
            self.fail(f"junction 创建失败：{result.stderr.decode('utf-8', 'replace')[:200]}")

        (link / "inside.txt").write_text("z", encoding="utf-8")

        with self.assertRaises(ArtifactError):
            workspace.scan()

    def test_missing_creation_time_is_unknown(self):
        self.creation = None
        self.addCleanup(lambda: setattr(self, "creation", "2026-01-01T00:00:00+00:00"))

        state = self.execute(FakeProcess(self.success_fixture()))

        self.assertEqual(state["state"], "unknown")

    def test_status_uses_process_created_at(self):
        run_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        workspace = Path(self.root) / run_id
        workspace.mkdir()
        (workspace / "state.json").write_text(
            json.dumps({
                "run_id": run_id,
                "state": "running",
                "pid": 1234,
                "process_started_at": "2026-01-01T00:00:00+00:00",
                "process_created_at": "2026-01-02T00:00:00+00:00",
            }),
            encoding="utf-8",
        )

        with patch("src.collab.qwen_runner._pid_alive", return_value=False) as mock_alive:
            status_of(self.root, run_id)

        passed = mock_alive.call_args.args[1]
        self.assertEqual(passed, "2026-01-02T00:00:00+00:00")

    def test_empty_snapshot_is_failure(self):
        from src.collab.qwen_runner import _wait_tree_gone

        with patch("src.collab.qwen_runner._snapshot_processes", return_value=None):
            gone, count, tracked = _wait_tree_gone(123)

        self.assertFalse(gone)

    def test_header_only_snapshot_is_none(self):
        from src.collab.qwen_runner import _snapshot_processes

        header = '"ProcessId","ParentProcessId","CreationDate"\n'

        with patch(
            "src.collab.qwen_runner.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, header, ""),
        ):
            self.assertIsNone(_snapshot_processes())

    def test_malformed_snapshot_is_none(self):
        from src.collab.qwen_runner import _snapshot_processes

        data = '"ProcessId","ParentProcessId","CreationDate"\n"x","y","z"\n'

        with patch(
            "src.collab.qwen_runner.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, data, ""),
        ):
            self.assertIsNone(_snapshot_processes())

    def test_tracked_descendant_reuse_detected(self):
        from src.collab.qwen_runner import _wait_tree_gone

        snapshot = [
            {"pid": 10, "parent": 1, "created": "old"},
            {"pid": 11, "parent": 10, "created": "new"},
        ]

        with patch("src.collab.qwen_runner._snapshot_processes", return_value=snapshot), patch(
            "src.collab.qwen_runner.time.sleep"
        ):
            gone, count, tracked = _wait_tree_gone(10, {11: "old"}, timeout=1)

        self.assertTrue(gone)

    def test_fsync_initial_failure_raises(self):
        from src.collab.qwen_runner import RunSpecError

        with patch("src.collab.qwen_workspace.os.fsync", side_effect=OSError("disk")):
            with self.assertRaises(RunSpecError):
                self.runner.execute(spec())

    def test_kill_path_keeps_pre_kill_tracked(self):
        captured = {}

        def fake_wait(pid, tracked=None, timeout=3.0):
            captured["tracked"] = dict(tracked or {})

            return False, len(tracked or {}), dict(tracked or {})

        FakeTracking.seed = {5000: "child-before-kill"}
        FakeTracking.instances = []

        with patch("src.collab.qwen_runner.subprocess.Popen", return_value=FakeProcess("", timeout=True, reaped=True)), patch(
            "src.collab.qwen_runner.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")
        ), patch("src.collab.qwen_runner._wait_tree_gone", side_effect=fake_wait), patch(
            "src.collab.qwen_runner._Tracking", FakeTracking
        ), patch("src.collab.qwen_runner._creation_iso", return_value="2026-01-01T00:00:00+00:00"):
            self.runner.execute(spec())

        self.assertIn(5000, captured["tracked"])

    def test_normal_path_tracking_starts_before_communicate(self):
        order = []

        class OrderTracking(FakeTracking):
            def start(self):
                order.append("start")

                return super().start()

        class OrderProcess(FakeProcess):
            def communicate(self, input=None, timeout=None):
                order.append("communicate")

                return super().communicate(input=input, timeout=timeout)

        with patch("src.collab.qwen_runner.subprocess.Popen", return_value=OrderProcess(self.success_fixture())), patch(
            "src.collab.qwen_runner.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")
        ), patch("src.collab.qwen_runner._wait_tree_gone", return_value=(True, 0, {})), patch(
            "src.collab.qwen_runner._Tracking", OrderTracking
        ), patch("src.collab.qwen_runner._creation_iso", return_value="2026-01-01T00:00:00+00:00"):
            self.runner.execute(spec())

        self.assertEqual(order[:2], ["start", "communicate"])

    def test_creation_offset_means_different_process(self):
        from datetime import datetime, timedelta, timezone
        from src.collab.qwen_runner import _pid_alive

        created = datetime(2026, 1, 1, tzinfo=timezone.utc)

        with patch("src.collab.qwen_runner._pid_exists", return_value=True), patch(
            "src.collab.qwen_runner._process_creation_time", return_value=created
        ):
            self.assertFalse(_pid_alive(123, (created + timedelta(seconds=0.5)).isoformat()))

    def test_state_write_failure_after_start_raises(self):
        from src.collab.qwen_runner import RunSpecError

        calls = {"n": 0}
        original = __import__("src.collab.qwen_workspace", fromlist=["RunWorkspace"]).RunWorkspace.write_state

        def flaky(self_ws, state):
            calls["n"] += 1

            if calls["n"] >= 2:
                raise OSError("disk")

            return original(self_ws, state)

        with patch("src.collab.qwen_workspace.RunWorkspace.write_state", flaky):
            with self.assertRaises(RunSpecError):
                self.execute(FakeProcess(self.success_fixture()))

    def test_prompt_not_in_state(self):
        state = self.execute(FakeProcess(self.success_fixture()), run_spec=spec(prompt="SECRET-PROMPT"))

        self.assertNotIn("SECRET-PROMPT", json.dumps(state, ensure_ascii=False))



    def test_slow_snapshot_result_is_merged(self):
        import time as _time

        from src.collab.qwen_runner import _Tracking

        snapshot = [{"pid": 5000, "parent": 4242, "created": "child"}]

        def slow_snapshot():
            _time.sleep(2.5)

            return snapshot

        with patch("src.collab.qwen_runner._snapshot_processes", slow_snapshot):
            tracking = _Tracking(4242, interval=0.05).start()
            _time.sleep(0.2)
            tracked = tracking.stop()

        self.assertIn(5000, tracked)
        self.assertFalse(tracking._thread.is_alive())

    def test_tracking_incomplete_is_unknown(self):
        class BrokenTracking(FakeTracking):
            def stop(self):
                from src.collab.qwen_runner import TrackingError

                raise TrackingError("incomplete")

        with patch("src.collab.qwen_runner.subprocess.Popen", return_value=FakeProcess(self.success_fixture())), patch(
            "src.collab.qwen_runner.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")
        ), patch("src.collab.qwen_runner._Tracking", BrokenTracking), patch(
            "src.collab.qwen_runner._creation_iso", return_value="2026-01-01T00:00:00+00:00"
        ):
            state = self.runner.execute(spec())

        self.assertEqual(state["state"], "unknown")

    def test_provider_error_does_not_leak_prompt(self):
        error_event = [{
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "session_id": "11111111-1111-1111-1111-111111111111",
            "error": {"message": "provider rejected prompt: SECRET-PROMPT-7f3e"},
        }]
        state = self.execute(FakeProcess(json.dumps(error_event)))

        blob = json.dumps(state, ensure_ascii=False)
        self.assertNotIn("SECRET-PROMPT-7f3e", blob)
        self.assertEqual(state["reason"]["message"], "Provider 返回错误")


if __name__ == "__main__":
    unittest.main()
