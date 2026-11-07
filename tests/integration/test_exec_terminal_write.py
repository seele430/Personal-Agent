"""终态唯一写：同一个 exec 只允许被写入一次终态。

背景（W1-b 查到的缺陷）：`ExecStore.finish()` 曾是无条件 UPDATE，
取消与超时同时到达时后到的会覆盖先到的终态，连 error_code 一起覆盖。
`exec_service.py` 的注释写着「第一个写下的原因决定终态」，但存储层没有兜底。

这组用例就是那条不变量的机器断言。
"""

import tempfile
import threading
import unittest

from pathlib import Path

from src.platform.exec_store import (
    EXEC_CANCELLED,
    EXEC_FAILED,
    EXEC_QUEUED,
    EXEC_SUCCEEDED,
    EXEC_TIMED_OUT,
    ExecStore,
)


SANDBOX = "sandbox-test"


class TerminalWriteTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ExecStore(Path(self.temp.name) / "platform.db")
        self.exec_id = self.create_exec()

    def create_exec(self) -> str:
        row = self.store.create(
            SANDBOX,
            argv=["cmd", "/c", "echo", "hi"],
            run_id=None,
            step_id=None,
            timeout_ms=1000,
            max_output_bytes=1024,
        )
        return row["id"]

    def finish(self, state, *, error_code="X", result_code="R"):
        return self.store.finish(
            self.exec_id,
            state=state,
            exit_code=0,
            stdout="out",
            stderr="",
            truncated=False,
            error_code=error_code,
            result_code=result_code,
        )

    # ---- 单写者 -----------------------------------------------------

    def test_finish_reports_true_on_first_write(self):
        wrote = self.finish(EXEC_SUCCEEDED, error_code=None, result_code="OK")

        self.assertIs(wrote, True)

        row = self.store.get(self.exec_id)

        self.assertEqual(row["state"], EXEC_SUCCEEDED)
        self.assertEqual(row["result_code"], "OK")
        self.assertIsNotNone(row["finished_at"])

    def test_second_finish_never_overwrites(self):
        self.assertTrue(self.finish(EXEC_CANCELLED, error_code="EXEC_CANCELLED"))
        wrote = self.finish(EXEC_TIMED_OUT, error_code="TIMEOUT")

        self.assertIs(wrote, False)

        row = self.store.get(self.exec_id)

        self.assertEqual(row["state"], EXEC_CANCELLED, "终态被后到的写覆盖了")
        self.assertEqual(row["error_code"], "EXEC_CANCELLED")
        self.assertEqual(row["stdout"], "out")

    def test_second_writer_cannot_change_payload(self):
        self.finish(EXEC_SUCCEEDED, error_code=None, result_code="OK")

        self.store.finish(
            self.exec_id,
            state=EXEC_FAILED,
            exit_code=1,
            stdout="late",
            stderr="late",
            truncated=True,
            error_code="LATE",
            result_code="EXEC_FAILED",
        )

        row = self.store.get(self.exec_id)

        self.assertEqual(row["state"], EXEC_SUCCEEDED)
        self.assertEqual(row["stdout"], "out")
        self.assertEqual(row["exit_code"], 0)
        self.assertEqual(row["truncated"], 0)

    def test_finished_at_is_not_rewritten(self):
        self.finish(EXEC_SUCCEEDED, error_code=None, result_code="OK")
        first = self.store.get(self.exec_id)["finished_at"]

        self.finish(EXEC_FAILED, error_code="LATE")

        self.assertEqual(self.store.get(self.exec_id)["finished_at"], first)

    def test_all_terminal_states_are_accepted(self):
        for state in (
            EXEC_SUCCEEDED,
            EXEC_FAILED,
            EXEC_CANCELLED,
            EXEC_TIMED_OUT,
        ):
            with self.subTest(state=state):
                exec_id = self.create_exec()

                self.assertTrue(
                    self.store.finish(
                        exec_id,
                        state=state,
                        exit_code=None,
                        stdout="",
                        stderr="",
                        truncated=False,
                        error_code="X",
                        result_code="R",
                    )
                )
                self.assertEqual(self.store.get(exec_id)["state"], state)

    def test_running_exec_can_be_finished_once(self):
        self.store.start(self.exec_id)

        self.assertTrue(self.finish(EXEC_SUCCEEDED, error_code=None, result_code="OK"))
        self.assertFalse(self.finish(EXEC_FAILED, error_code="LATE"))

    def test_non_terminal_state_is_rejected(self):
        for state in (EXEC_QUEUED, "running", "unknown"):
            with self.subTest(state=state):
                with self.assertRaises(ValueError):
                    self.finish(state)

    def test_unknown_exec_reports_false(self):
        wrote = self.store.finish(
            "exec_does_not_exist",
            state=EXEC_FAILED,
            exit_code=None,
            stdout="",
            stderr="",
            truncated=False,
            error_code="X",
            result_code="R",
        )

        self.assertIs(wrote, False)

    # ---- 并发 -------------------------------------------------------

    def test_concurrent_finish_writes_exactly_once(self):
        states = [
            (EXEC_SUCCEEDED, "OK"),
            (EXEC_FAILED, "BOOM"),
            (EXEC_CANCELLED, "CANCELLED"),
            (EXEC_TIMED_OUT, "TIMEOUT"),
            (EXEC_SUCCEEDED, "OK2"),
            (EXEC_FAILED, "BOOM2"),
            (EXEC_CANCELLED, "CANCELLED2"),
            (EXEC_TIMED_OUT, "TIMEOUT2"),
        ]

        barrier = threading.Barrier(len(states))
        results = []
        lock = threading.Lock()

        def worker(state, code):
            barrier.wait()
            wrote = self.finish(state, error_code=code, result_code=code)

            with lock:
                results.append((wrote, state, code))

        threads = [
            threading.Thread(target=worker, args=pair) for pair in states
        ]

        for thread in threads:
            thread.start()

        for thread in threads:
            thread.join()

        winners = [item for item in results if item[0]]

        self.assertEqual(len(winners), 1, f"应当恰好一次写入，实际 {len(winners)} 次")

        _, state, code = winners[0]
        row = self.store.get(self.exec_id)

        self.assertEqual(row["state"], state)
        self.assertEqual(row["error_code"], code)
        self.assertEqual(row["result_code"], code)


if __name__ == "__main__":
    unittest.main()
