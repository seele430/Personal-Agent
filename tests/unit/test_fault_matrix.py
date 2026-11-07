"""六类故障注入测试：每类都要有终态、可恢复，且不能靠改产品逻辑配合。"""

import unittest

from src.faults.matrix import (
    FAULTS,
    fault_db_write_interrupt,
    fault_disk_full,
    fault_exec_crash,
    fault_model_rate_limit,
    fault_notification_fail,
    fault_search_timeout,
    run_matrix,
)


class TestMatrix(unittest.TestCase):
    def test_six_faults_are_registered(self):
        names = [name for name, _, _ in FAULTS]

        self.assertEqual(
            names,
            [
                "disk_full",
                "search_timeout",
                "model_rate_limit",
                "exec_crash",
                "notification_fail",
                "db_write_interrupt",
            ],
        )


class TestIndividualFaults(unittest.TestCase):
    def test_disk_full(self):
        outcome = fault_disk_full()

        self.assertEqual(outcome["terminal"], "artifact_rejected")
        self.assertTrue(outcome["recovered"])
        self.assertEqual(outcome["evidence"]["rows_after_reject"], 0)

    def test_search_timeout(self):
        outcome = fault_search_timeout()

        self.assertEqual(outcome["terminal"], "search_unavailable")
        self.assertEqual(outcome["evidence"]["recovered_hits"], 1)

    def test_model_rate_limit(self):
        outcome = fault_model_rate_limit()

        self.assertEqual(outcome["terminal"], "llm_rate_limited")
        # 只重试 1 次：初次 + 1 次重试 = 2 次调用
        self.assertEqual(outcome["evidence"]["attempts"], 2)

    def test_exec_crash_is_recorded_not_raised(self):
        outcome = fault_exec_crash()

        self.assertEqual(outcome["terminal"], "run_failed")
        self.assertTrue(outcome["evidence"]["recovered"])

    def test_notification_fail_retries_once(self):
        outcome = fault_notification_fail()

        self.assertEqual(outcome["terminal"], "outbox_retry")
        self.assertEqual(outcome["evidence"]["final_status"], "sent")
        self.assertEqual(outcome["evidence"]["delivered_times"], 1)

    def test_db_write_interrupt_rolls_back(self):
        outcome = fault_db_write_interrupt()

        self.assertEqual(outcome["terminal"], "transaction_rolled_back")
        self.assertEqual(outcome["evidence"]["count_after_failure"], 0)
        self.assertEqual(outcome["evidence"]["recovered_rows"], 1)


class TestRunMatrix(unittest.TestCase):
    def test_all_six_pass(self):
        results = run_matrix()

        self.assertEqual(len(results), 6)
        self.assertTrue(all(item["recovered"] for item in results), results)
        self.assertTrue(
            all(item["terminal"] != "injection_failed" for item in results)
        )

    def test_each_result_has_label_and_evidence(self):
        for item in run_matrix():
            self.assertTrue(item["label"])
            self.assertIsInstance(item["evidence"], dict)
            self.assertTrue(item["detail"])


class TestMatrixStability(unittest.TestCase):
    """矩阵本身的可重复性与可序列化（证据要能落盘复核）。"""

    def test_all_labels_present(self):
        labels = [item["label"] for item in run_matrix()]

        self.assertEqual(len(labels), 6)
        self.assertTrue(all(labels))

    def test_matrix_is_repeatable(self):
        first = run_matrix()
        second = run_matrix()

        self.assertEqual(
            [item["name"] for item in first], [item["name"] for item in second]
        )
        self.assertTrue(all(item["recovered"] for item in second))

    def test_notification_fault_twice_delivers_once_each(self):
        first = fault_notification_fail()
        second = fault_notification_fail()

        self.assertEqual(first["evidence"]["delivered_times"], 1)
        self.assertEqual(second["evidence"]["delivered_times"], 1)

    def test_results_serialize_to_json(self):
        import json

        payload = json.dumps(run_matrix(), ensure_ascii=False)

        self.assertIn("disk_full", payload)
        self.assertIn("notification_fail", payload)
if __name__ == "__main__":
    unittest.main()
