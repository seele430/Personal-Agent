"""D26：最简单的进程内令牌桶限流。

为什么不是分布式：当前平台是单进程，多进程共享需要 Redis 之类的外部状态，
这里不引入。**限制是进程内的**，多 worker 部署时每个进程各有一份额度 ——
这一点写在文档里，不假装是全局限流。
"""

from __future__ import annotations

import os
import threading
import time

from dataclasses import dataclass

RATE_ENV = "PERSONAL_AGENT_PLATFORM_RATE_PER_MIN"
BURST_ENV = "PERSONAL_AGENT_PLATFORM_RATE_BURST"

DEFAULT_RATE_PER_MIN = 600
DEFAULT_BURST = 60


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()

    if not raw:
        return default

    try:
        value = int(raw)
    except ValueError:
        return default

    return value if value > 0 else default


def platform_rate() -> tuple[int, int] | None:
    """返回 (每分钟补充数, 桶容量)；返回 None 表示显式关闭限流。"""

    raw = os.getenv(RATE_ENV, "").strip()

    if raw == "0":
        # 0 是显式关闭（测试与受控环境用），不是「回退默认值」。
        return None

    return (
        _positive_int(RATE_ENV, DEFAULT_RATE_PER_MIN),
        _positive_int(BURST_ENV, DEFAULT_BURST),
    )


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class RateLimiter:
    def __init__(
        self,
        *,
        rate_per_minute: int,
        burst: int,
        clock=time.monotonic,
        max_buckets: int = 4096,
        idle_ttl_seconds: float = 300.0,
    ):
        self.rate_per_second = rate_per_minute / 60.0
        self.capacity = float(burst)
        self.clock = clock
        self.max_buckets = max_buckets
        self.idle_ttl_seconds = idle_ttl_seconds
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def _sweep(self, now: float) -> None:
        """回收「令牌已满」的空闲桶；调用方必须已持锁。

        只回收满桶：满桶与新建的桶完全等价，丢掉不改变任何调用方的额度。
        被消耗过的桶一律保留 —— 否则攻击者只要触发回收就能重获突发额度，
        修复等于没做。
        """

        if self.max_buckets <= 0 or len(self._buckets) <= self.max_buckets:
            return

        def effective_tokens(bucket: _Bucket) -> float:
            # 桶只在被访问时才补令牌；回收判断必须自己把这段时间算上，
            # 否则「很久没碰过的满桶」会被误判成空桶，永远收不掉。
            return min(
                self.capacity,
                bucket.tokens
                + max(0.0, now - bucket.updated_at) * self.rate_per_second,
            )

        expired = [
            key
            for key, bucket in self._buckets.items()
            if now - bucket.updated_at >= self.idle_ttl_seconds
            and effective_tokens(bucket) >= self.capacity
        ]

        for key in expired:
            self._buckets.pop(key, None)

        if len(self._buckets) <= self.max_buckets:
            return

        # 仍超限：从最老的满桶开始收，绝不碰欠额度的桶。
        full = sorted(
            (
                key
                for key, bucket in self._buckets.items()
                if effective_tokens(bucket) >= self.capacity
            ),
            key=lambda key: self._buckets[key].updated_at,
        )

        for key in full:
            if len(self._buckets) <= self.max_buckets:
                break

            self._buckets.pop(key, None)

    def allow(self, key: str) -> bool:
        """取一个令牌；成功返回 True。"""

        now = self.clock()

        with self._lock:
            bucket = self._buckets.get(key)

            if bucket is None:
                bucket = _Bucket(tokens=self.capacity, updated_at=now)
                self._buckets[key] = bucket
            else:
                elapsed = max(0.0, now - bucket.updated_at)
                bucket.tokens = min(
                    self.capacity,
                    bucket.tokens + elapsed * self.rate_per_second,
                )
                bucket.updated_at = now

            if bucket.tokens < 1.0:
                return False

            bucket.tokens -= 1.0
            self._sweep(now)
            return True

    def retry_after_seconds(self, key: str) -> int:
        """满桶恢复一个令牌需要多久（向上取整，至少 1 秒）。"""

        with self._lock:
            bucket = self._buckets.get(key)

            if bucket is None or self.rate_per_second <= 0:
                return 1

            missing = max(0.0, 1.0 - bucket.tokens)
            return max(1, int(missing / self.rate_per_second) + 1)

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


_limiter: RateLimiter | None = None
_limiter_lock = threading.Lock()


def get_limiter() -> RateLimiter | None:
    """进程级单例；未配置或显式关闭时返回 None。"""

    global _limiter

    with _limiter_lock:
        if _limiter is None:
            configured = platform_rate()

            if configured is None:
                return None

            rate, burst = configured
            _limiter = RateLimiter(rate_per_minute=rate, burst=burst)

        return _limiter


def reset_limiter() -> None:
    """测试用：丢掉当前实例，下次按最新环境变量重建。"""

    global _limiter

    with _limiter_lock:
        _limiter = None
