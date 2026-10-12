
"""DeepSeek Flash 保守费用估算测试。

全部为本地计算和临时数据库操作。
不连接 DeepSeek，不消耗真实 Token。
"""

import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from src.core.ai_pricing import (
    PricingUnavailable,
    estimate_peak_cost_usd,
    user_observed_cost_report,
)
from src.core.ai_usage import AIUsageStore


HOST = "api.deepseek.com"
MODEL = "deepseek-flash"


class TestAIPricing(unittest.TestCase):

    def setUp(self):
        self.temp = TemporaryDirectory()

        self.db_path = (
            Path(self.temp.name) / "usage.db"
        )

        self.store = AIUsageStore(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def estimate(
        self,
        prompt=1000000,
        completion=1000000,
        cached=0,
        model=MODEL,
        host=HOST,
    ):
        return estimate_peak_cost_usd(
            provider_host=host,
            model=model,
            prompt_tokens=prompt,
            completion_tokens=completion,
            cache_hit_tokens=cached,
        )

    def record(
        self,
        user_id,
        request_id,
        usage,
        model=MODEL,
    ):
        self.store.record_completion(
            user_id=user_id,
            request_id=request_id,
            call_number=1,
            model=model,
            response=SimpleNamespace(
                usage=usage
            ),
        )

    # 1. 全部缓存未命中的高峰价格

    def test_peak_cache_miss_price(self):
        self.assertEqual(
            self.estimate(),
            Decimal("1.50"),
        )

    # 2. 输入全部命中缓存

    def test_peak_cache_hit_price(self):
        self.assertEqual(
            self.estimate(cached=1000000),
            Decimal("1.206"),
        )

    # 3. 缓存字段缺失，按照较贵价格计算

    def test_missing_cache_uses_miss_rate(self):
        self.assertEqual(
            self.estimate(cached=None),
            self.estimate(cached=0),
        )

    # 4. 关键用量缺失，不能假设免费

    def test_missing_tokens_are_unknown(self):
        self.assertIsNone(
            self.estimate(prompt=None)
        )

        self.assertIsNone(
            self.estimate(completion=None)
        )

    # 5. 拒绝负数、布尔值及错误缓存数

    def test_invalid_token_values(self):
        for value in (-1, True, 1.5):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.estimate(prompt=value)

        with self.assertRaises(ValueError):
            self.estimate(
                prompt=100,
                completion=20,
                cached=101,
            )

    # 6. 其他服务商或模型不能套用价格

    def test_unknown_model_or_provider(self):
        with self.assertRaises(
            PricingUnavailable
        ):
            self.estimate(
                model="another-model"
            )

        with self.assertRaises(
            PricingUnavailable
        ):
            self.estimate(
                host="another-provider.example"
            )

    # 7. 缺失 usage 必须单独报告

    def test_report_tracks_unpriced_calls(self):
        self.record(
            "alice",
            "request_1",
            {
                "prompt_tokens": 1000,
                "completion_tokens": 500,
                "total_tokens": 1500,
            },
        )

        self.record(
            "alice",
            "request_2",
            None,
        )

        report = user_observed_cost_report(
            db_path=self.db_path,
            user_id="alice",
            provider_host=HOST,
        )

        self.assertEqual(
            report["total_recorded_calls"],
            2,
        )

        self.assertEqual(
            report["priced_calls"],
            1,
        )

        self.assertEqual(
            report["unpriced_calls"],
            1,
        )

        self.assertGreater(
            Decimal(
                report["known_calls_peak_usd"]
            ),
            0,
        )

    # 8. 不同用户的统计不能混合

    def test_user_reports_are_independent(self):
        usage = {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        }

        self.record(
            "alice",
            "alice_request",
            usage,
        )

        self.record(
            "bob",
            "bob_request",
            usage,
        )

        alice = user_observed_cost_report(
            db_path=self.db_path,
            user_id="alice",
            provider_host=HOST,
        )

        bob = user_observed_cost_report(
            db_path=self.db_path,
            user_id="bob",
            provider_host=HOST,
        )

        self.assertEqual(
            alice["total_recorded_calls"],
            1,
        )

        self.assertEqual(
            bob["total_recorded_calls"],
            1,
        )


if __name__ == "__main__":
    unittest.main()