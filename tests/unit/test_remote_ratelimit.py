"""失败限速测试：阈值、冷却、清零、隔离、时间推进。"""

import unittest

from src.remote.ratelimit import FailureLimiter, LimitPolicy, RateLimitError


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def build(**kwargs):
    clock = Clock()
    policy = LimitPolicy(**kwargs)

    return FailureLimiter(policy, clock=clock), clock


class TestPolicy(unittest.TestCase):
    def test_defaults(self):
        policy = LimitPolicy()

        self.assertEqual(policy.max_failures, 10)
        self.assertEqual(policy.window_seconds, 60)
        self.assertEqual(policy.block_seconds, 300)

    def test_invalid_values(self):
        for kwargs in (
            {"max_failures": 0},
            {"window_seconds": -1},
            {"block_seconds": True},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(RateLimitError):
                    LimitPolicy(**kwargs)


class TestLimiter(unittest.TestCase):
    def test_allows_before_any_failure(self):
        limiter, _ = build()

        self.assertEqual(limiter.allow("a"), (True, 0))

    def test_below_threshold_is_allowed(self):
        limiter, _ = build(max_failures=3)

        limiter.record_failure("a")
        limiter.record_failure("a")

        self.assertTrue(limiter.allow("a")[0])

    def test_threshold_blocks(self):
        limiter, _ = build(max_failures=3, block_seconds=60)

        for _ in range(3):
            limiter.record_failure("a")

        allowed, retry_after = limiter.allow("a")

        self.assertFalse(allowed)
        self.assertGreaterEqual(retry_after, 1)

    def test_block_expires(self):
        limiter, clock = build(max_failures=2, block_seconds=60)

        limiter.record_failure("a")
        limiter.record_failure("a")
        self.assertFalse(limiter.allow("a")[0])

        clock.advance(61)

        self.assertTrue(limiter.allow("a")[0])

    def test_success_resets(self):
        limiter, _ = build(max_failures=3)

        limiter.record_failure("a")
        limiter.record_failure("a")
        limiter.record_success("a")

        self.assertTrue(limiter.allow("a")[0])

        for _ in range(2):
            limiter.record_failure("a")

        self.assertTrue(limiter.allow("a")[0])

    def test_clients_isolated(self):
        limiter, _ = build(max_failures=2)

        limiter.record_failure("a")
        limiter.record_failure("a")

        self.assertFalse(limiter.allow("a")[0])
        self.assertTrue(limiter.allow("b")[0])

    def test_window_expiry_clears_failures(self):
        limiter, clock = build(max_failures=3, window_seconds=10)

        limiter.record_failure("a")
        limiter.record_failure("a")
        clock.advance(11)
        limiter.record_failure("a")

        self.assertTrue(limiter.allow("a")[0])

    def test_blocked_helper(self):
        limiter, _ = build(max_failures=1)

        limiter.record_failure("a")

        self.assertTrue(limiter.blocked("a"))
        self.assertFalse(limiter.blocked("b"))

    def test_snapshot(self):
        limiter, _ = build(max_failures=2)

        limiter.record_failure("a")
        payload = limiter.snapshot()

        self.assertEqual(payload["clients"], 1)
        self.assertEqual(payload["policy"]["max_failures"], 2)

    def test_retry_after_rounds_up(self):
        limiter, clock = build(max_failures=1, block_seconds=60)

        limiter.record_failure("a")
        clock.advance(0.5)

        _, retry_after = limiter.allow("a")

        self.assertEqual(retry_after, 60)


if __name__ == "__main__":
    unittest.main()
