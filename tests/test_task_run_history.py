
import tempfile
import unittest

from tests.helper import isolated_paths
from src.core import task_run_history


class TestTaskRunHistory(unittest.TestCase):

    def test_successful_execution(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with isolated_paths(temp_dir):
                run_id = task_run_history.start_task_run(
                    "rss_test",
                    "lease_123"
                )

                result = task_run_history.finish_task_run(
                    run_id,
                    status="success",
                    new_items=3
                )

                self.assertTrue(result)

                records = task_run_history.list_task_runs(
                    "rss_test"
                )

                self.assertEqual(len(records), 1)
                self.assertEqual(
                    records[0]["status"], "success"
                )
                self.assertEqual(
                    records[0]["new_items"], 3
                )
                self.assertIsNotNone(
                    records[0]["finished_at"]
                )
                self.assertGreaterEqual(
                    records[0]["duration_ms"], 0
                )

                # 不允许重复结束同一次执行
                self.assertFalse(
                    task_run_history.finish_task_run(
                        run_id,
                        status="failed"
                    )
                )

    def test_failed_execution(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with isolated_paths(temp_dir):
                run_id = task_run_history.start_task_run(
                    "rss_failure"
                )

                task_run_history.finish_task_run(
                    run_id,
                    status="failed",
                    error_message="Simulated RSS failure"
                )

                records = task_run_history.list_task_runs(
                    "rss_failure"
                )

                self.assertEqual(
                    records[0]["status"], "failed"
                )
                self.assertEqual(
                    records[0]["error_message"],
                    "Simulated RSS failure"
                )


if __name__ == "__main__":
    unittest.main()