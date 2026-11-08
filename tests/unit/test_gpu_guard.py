"""GPU 保护自检的测试：现在应全过；改坏常量应报 FAIL。"""

import importlib
import io
import sys
import unittest

from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def run_main():
    import scripts.verify_gpu_guard as guard

    importlib.reload(guard)
    buffer = io.StringIO()

    with redirect_stdout(buffer):
        code = guard.main()

    return code, buffer.getvalue()


class GuardCase(unittest.TestCase):
    def test_current_state_passes(self):
        code, output = run_main()

        self.assertEqual(code, 0, output)
        self.assertIn("PASS", output)
        self.assertNotIn("FAIL  ", output)

    def test_steps_limit_drift_detected(self):
        from scripts import verify_gpu_guard as guard
        import src.tools.comfy_tools as tools

        with patch.object(tools, "MAX_STEPS", 60):
            buffer = io.StringIO()

            with redirect_stdout(buffer):
                code = guard.main()

        self.assertEqual(code, 1)
        self.assertIn("steps 上限", buffer.getvalue())

    def test_vram_threshold_drift_detected(self):
        from scripts import verify_gpu_guard as guard
        import src.tools.comfy_tools as tools

        with patch.object(tools, "MIN_FREE_VRAM_MB", 500):
            buffer = io.StringIO()

            with redirect_stdout(buffer):
                code = guard.main()

        self.assertEqual(code, 1)
        self.assertIn("显存门槛", buffer.getvalue())

    def test_timeout_drift_detected(self):
        from scripts import verify_gpu_guard as guard
        import src.tools.comfy_tools as tools

        with patch.object(tools, "DEFAULT_TIMEOUT", 600.0):
            buffer = io.StringIO()

            with redirect_stdout(buffer):
                code = guard.main()

        self.assertEqual(code, 1)
        self.assertIn("默认超时", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
