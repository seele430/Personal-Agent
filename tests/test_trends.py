"""趋势采样：ring buffer 窗口、样本量与采样线程（V2 §4.3 / §5）。"""

from __future__ import annotations

import time
import unittest

from tempfile import TemporaryDirectory

from src.platform import trends
from tests.helper import isolate_paths


class TrendBufferTest(unittest.TestCase):
    def setUp(self):
        self.buffer = trends.TrendBuffer(window_seconds=900, max_samples=5)

    def test_records_and_returns_series(self):
        sample = self.buffer.record(
            running=3,
            queued=2,
            capacity=1,
            desired=2,
            action="hold",
        )

        self.assertEqual(sample["running"], 3)
        self.assertNotIn("_epoch", sample)

        series = self.buffer.series()

        self.assertEqual(len(series), 1)
        self.assertEqual(series[0]["action"], "hold")
        self.assertIn("at", series[0])

    def test_window_filters_old_samples(self):
        now = time.time()

        self.buffer.record(running=1, queued=0, capacity=0, desired=1, at=now - 1000)
        self.buffer.record(running=2, queued=0, capacity=0, desired=1, at=now - 10)

        self.assertEqual(len(self.buffer.series(window_seconds=900, now=now)), 1)
        self.assertEqual(len(self.buffer.series(window_seconds=2000, now=now)), 2)

    def test_ring_buffer_drops_oldest(self):
        for index in range(7):
            self.buffer.record(
                running=index,
                queued=0,
                capacity=0,
                desired=1,
                at=time.time(),
            )

        series = self.buffer.series()

        self.assertEqual(len(series), 5)
        self.assertEqual(series[0]["running"], 2)

    def test_clear(self):
        self.buffer.record(running=1, queued=0, capacity=0, desired=1)
        self.buffer.clear()

        self.assertEqual(self.buffer.count(), 0)


class SampleOnceTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        isolate_paths(self, self.temp)

        trends.reset()
        self.addCleanup(trends.reset)

    def test_sample_once_reads_queue_capacity_and_policy(self):
        sample = trends.sample_once(now=time.time())

        self.assertEqual(sample["running"], 0)
        self.assertEqual(sample["queued"], 0)
        self.assertEqual(sample["capacity"], 0)
        # 空负载下期望容量是 min_nodes=1
        self.assertEqual(sample["desired"], 1)
        self.assertEqual(sample["action"], "none")
        self.assertEqual(trends.sample_count(), 1)


class SamplerThreadTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        isolate_paths(self, self.temp)

        trends.reset()
        self.addCleanup(trends.reset)

    def test_sampler_runs_and_stops_cleanly(self):
        """回归：`_stop` 不能覆盖 Thread._stop，否则 is_alive() 抛 TypeError。"""

        sampler = trends.TrendSampler(interval=0.05)
        sampler.start()

        try:
            deadline = time.monotonic() + 2.0

            while trends.sample_count() == 0 and time.monotonic() < deadline:
                time.sleep(0.01)

            self.assertGreaterEqual(trends.sample_count(), 1)
        finally:
            sampler.stop()
            sampler.join(timeout=2.0)

        self.assertFalse(sampler.is_alive())


if __name__ == "__main__":
    unittest.main()
