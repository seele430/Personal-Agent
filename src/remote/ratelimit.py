"""令牌失败限速：按客户端计数，超阈值进入冷却期。

设计取舍（写在代码里，避免以后有人"顺手改松"）：

1. **只对失败计数**——成功一次就把该客户端清零，正常用户不会被自己误伤；
2. **两段式**——窗口内失败达阈值 → 冷却期内**不再校验令牌**，直接 429，
   否则爆破者仍能用 401/429 的差异做探测；
3. **单进程内存**——多进程/多实例部署时各算各的；要真正分布式得换 Redis，
   这里不假装支持。
"""

from __future__ import annotations

import math
import threading
import time

from dataclasses import dataclass


MAX_CLIENTS = 10_000


class RateLimitError(ValueError):
    """限速参数不合法。"""


@dataclass(frozen=True)
class LimitPolicy:
    max_failures: int = 10
    window_seconds: int = 60
    block_seconds: int = 300

    def __post_init__(self):
        for name, value in (
            ("max_failures", self.max_failures),
            ("window_seconds", self.window_seconds),
            ("block_seconds", self.block_seconds),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise RateLimitError(f"{name} 必须是 ≥1 的整数")


@dataclass
class _Entry:
    failures: list[float]
    blocked_until: float = 0.0


class FailureLimiter:
    """线程安全的失败计数 + 冷却。"""

    def __init__(self, policy: LimitPolicy | None = None, *, clock=None):
        self.policy = policy or LimitPolicy()
        self._clock = clock or time.monotonic
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def allow(self, client: str) -> tuple[bool, int]:
        """返回 (是否放行, Retry-After 秒数)。"""

        now = self._clock()

        with self._lock:
            entry = self._entries.get(client)

            if entry is None:
                return True, 0

            if entry.blocked_until > now:
                return False, max(1, math.ceil(entry.blocked_until - now))

            self._prune(entry, now)

            return True, 0

    def record_failure(self, client: str) -> None:
        now = self._clock()

        with self._lock:
            if client not in self._entries and len(self._entries) >= MAX_CLIENTS:
                self._evict_oldest()

            entry = self._entries.setdefault(client, _Entry(failures=[]))
            self._prune(entry, now)
            entry.failures.append(now)

            if len(entry.failures) >= self.policy.max_failures:
                entry.blocked_until = now + self.policy.block_seconds
                entry.failures.clear()

    def record_success(self, client: str) -> None:
        with self._lock:
            self._entries.pop(client, None)

    def blocked(self, client: str) -> bool:
        allowed, _ = self.allow(client)

        return not allowed

    def _prune(self, entry: _Entry, now: float) -> None:
        cutoff = now - self.policy.window_seconds
        entry.failures = [stamp for stamp in entry.failures if stamp > cutoff]

        if entry.blocked_until and entry.blocked_until <= now:
            entry.blocked_until = 0.0

    def _evict_oldest(self) -> None:
        oldest = min(
            self._entries,
            key=lambda key: max(self._entries[key].failures, default=0.0),
        )
        self._entries.pop(oldest, None)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "clients": len(self._entries),
                "policy": {
                    "max_failures": self.policy.max_failures,
                    "window_seconds": self.policy.window_seconds,
                    "block_seconds": self.policy.block_seconds,
                },
            }
