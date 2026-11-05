"""W1.4 垂直切片：一句话 → 路由 → 沙箱 → chart.png → trace。"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest

from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]


class RoutePipelineTest(unittest.TestCase):
    def test_pipeline_produces_chart_and_trace(self):
        with TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "demo_route_pipeline.py"),
                    "--data-dir",
                    temp,
                ],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=180,
            )

            self.assertEqual(result.returncode, 0, result.stderr[-2000:])

            chart = Path(temp) / "chart.png"
            self.assertTrue(chart.is_file(), result.stdout[-1000:])
            self.assertGreater(chart.stat().st_size, 1000)

            summary = json.loads(result.stdout[result.stdout.index("{") :])

            self.assertEqual(summary["intent"], "data.analysis")
            self.assertEqual(summary["exec_state"], "succeeded")
            self.assertEqual(summary["node_id"], "node_local")

            events = summary["events"]

            for required in (
                "run_started",
                "route_decided",
                "approval_checked",
                "node_selected",
                "exec_queued",
                "exec_started",
                "exec_succeeded",
                "artifact",
                "run_finished",
            ):
                self.assertIn(required, events)


if __name__ == "__main__":
    unittest.main()
