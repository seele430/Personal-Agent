"""文档一致性检查的测试：当前应 PASS；注入漂移必须 FAIL。"""

import io
import unittest

from contextlib import redirect_stdout
from unittest.mock import patch


def run():
    import scripts.check_docs_consistency as checker

    buffer = io.StringIO()

    with redirect_stdout(buffer):
        code = checker.main()

    return code, buffer.getvalue()


class DocsCase(unittest.TestCase):
    def test_current_docs_are_consistent(self):
        code, output = run()

        self.assertEqual(code, 0, output)
        self.assertIn("PASS", output)

    def test_spec_count_drift_detected(self):
        import scripts.check_docs_consistency as checker

        with patch.object(checker, "spec_tests", return_value=1):
            code, output = run()

        self.assertEqual(code, 1)
        self.assertIn("Playwright", output)

    def test_gpu_constant_drift_detected(self):
        import scripts.check_docs_consistency as checker
        from src.tools import comfy_tools

        with patch.object(comfy_tools, "MAX_STEPS", 99):
            code, output = run()

        self.assertEqual(code, 1)
        self.assertIn("30", output)


if __name__ == "__main__":
    unittest.main()
