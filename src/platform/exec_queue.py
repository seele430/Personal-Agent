"""D30：有界执行队列。

契约 §5.2：D30 之前不隐式无限排队；D30 之后队列满返回 429 ``QUEUE_FULL``，
queued 的请求仍然必须可取消。

设计取舍：

- 并发上限与队列容量都是**进程内**的（和 D26 的限流一样），多 worker 各算各的；
  这不是全局调度，文档里写死。
- 队列里只放「还没起进程」的 exec；一旦出队就交给 ``_run_exec`` 线程，
  真正的进程树清理仍由 tier1 执行器负责。
- 取消一个 queued exec 不需要杀进程：它从来没启动过，直接落 cancelled。
"""

from __future__ import annotations

import os
import threading
import time

from collections import deque
from dataclasses import dataclass, field
from typing import Callable

ACTIVE_ENV = "PERSONAL_AGENT_PLATFORM_MAX_ACTIVE_EXECS"
QUEUE_ENV = "PERSONAL_AGENT_PLATFORM_QUEUE_SIZE"
WAIT_ENV = "PERSONAL_AGENT_PLATFORM_QUEUE_MAX_WAIT"
SWEEP_ENV = "PERSONAL_AGENT_PLATFORM_QUEUE_SWEEP"

DEFAULT_MAX_ACTIVE = 4
DEFAULT_QUEUE_SIZE = 16
DEFAULT_MAX_WAIT_SECONDS = 60.0
DEFAULT_SWEEP_SECONDS = 5.0
MAX_ACTIVE_LIMIT = 64
MAX_QUEUE_LIMIT = 1024


def _int_env(name: str, default: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()

    if not raw:
        return default

    try:
        value = int(raw)
    except ValueError:
        return default

    if value < 1:
        return default

    return min(value, maximum)


def queue_limits() -> tuple[int, int]:
    return (
        _int_env(ACTIVE_ENV, DEFAULT_MAX_ACTIVE, MAX_ACTIVE_LIMIT),
        _int_env(QUEUE_ENV, DEFAULT_QUEUE_SIZE, MAX_QUEUE_LIMIT),
    )


def _float_env(name: str, default: float, *, allow_zero: bool = False) -> float:
    raw = os.getenv(name, "").strip()

    if not raw:
        return default

    try:
        value = float(raw)
    except ValueError:
        return default

    if value < 0:
        return default

    if value == 0 and not allow_zero:
        return default

    return value


def queue_max_wait() -> float:
    """排队最长等待秒数；0 表示不限。"""

    return _float_env(WAIT_ENV, DEFAULT_MAX_WAIT_SECONDS, allow_zero=True)


def queue_sweep_interval() -> float:
    return max(0.1, _float_env(SWEEP_ENV, DEFAULT_SWEEP_SECONDS))


@dataclass
class QueuedExec:
    exec_id: str
    sandbox_id: str
    argv: list[str]
    timeout_ms: int
    max_output_bytes: int
    traceparent: str | None = None
    payload: dict = field(default_factory=dict)
    enqueued_at: float = field(default_factory=time.monotonic)


class ExecQueue:
    """并发闸门 + 有界等待队列（数量有界、时间也有界）。"""

    def __init__(
        self,
        *,
        max_active: int,
        max_queued: int,
        max_wait_seconds: float = 0.0,
    ):
        self.max_active = max_active
        self.max_queued = max_queued
        # 0 表示不限等待时间（默认 60 秒由环境变量给）。
        self.max_wait_seconds = max_wait_seconds
        self._active = 0
        self._queue: deque[QueuedExec] = deque()
        self._lock = threading.Lock()
        self._start: Callable[[QueuedExec], None] | None = None
        self._expire: Callable[[QueuedExec], None] | None = None

    def start_with(self, starter: Callable[[QueuedExec], None]) -> None:
        self._start = starter

    def expire_with(self, handler: Callable[[QueuedExec], None]) -> None:
        self._expire = handler

    # ------------------------------------------------------------------

    def accepting(self) -> bool:
        """还有没有位置（立刻能跑或能排队）。"""

        with self._lock:
            return (
                self._active < self.max_active
                or len(self._queue) < self.max_queued
            )

    def submit(self, item: QueuedExec) -> str:
        """返回 ``started`` / ``queued`` / ``full``。"""

        with self._lock:
            if self._active < self.max_active and not self._queue:
                self._active += 1
                starter = self._start
            elif len(self._queue) < self.max_queued:
                self._queue.append(item)
                return "queued"
            else:
                return "full"

        if starter is not None:
            starter(item)

        return "started"

    def cancel(self, exec_id: str) -> bool:
        """把还没起进程的 exec 从队列里摘掉；返回是否摘到。"""

        with self._lock:
            for index, item in enumerate(self._queue):
                if item.exec_id == exec_id:
                    del self._queue[index]
                    return True

        return False

    def finish(self) -> None:
        """一个活跃 exec 结束；有空位就把队首放出去（跳过已超等待期的）。"""

        expired: list[QueuedExec] = []
        next_item: QueuedExec | None = None
        starter = None

        with self._lock:
            self._active = max(0, self._active - 1)
            now = time.monotonic()

            while self._queue and self._active < self.max_active:
                candidate = self._queue.popleft()

                if self._expired(candidate, now):
                    expired.append(candidate)
                    continue

                next_item = candidate
                self._active += 1
                starter = self._start
                break

        for item in expired:
            self._notify_expired(item)

        if next_item is not None and starter is not None:
            starter(next_item)

    def _expired(self, item: QueuedExec, now: float) -> bool:
        return (
            self.max_wait_seconds > 0
            and now - item.enqueued_at >= self.max_wait_seconds
        )

    def _notify_expired(self, item: QueuedExec) -> None:
        if self._expire is not None:
            self._expire(item)

    def sweep(self, now: float | None = None) -> list[QueuedExec]:
        """把等太久的排队项摘出来交给 expire 回调（看门狗调这个）。"""

        if self.max_wait_seconds <= 0:
            return []

        moment = time.monotonic() if now is None else now
        expired: list[QueuedExec] = []

        with self._lock:
            kept: deque[QueuedExec] = deque()

            for item in self._queue:
                if self._expired(item, moment):
                    expired.append(item)
                else:
                    kept.append(item)

            self._queue = kept

        for item in expired:
            self._notify_expired(item)

        return expired

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "active": self._active,
                "queued": len(self._queue),
                "max_active": self.max_active,
                "max_queued": self.max_queued,
                "max_wait_seconds": self.max_wait_seconds,
            }

    def reset(self) -> None:
        with self._lock:
            self._active = 0
            self._queue.clear()


_queue: ExecQueue | None = None
_queue_lock = threading.Lock()


def get_queue() -> ExecQueue:
    global _queue

    with _queue_lock:
        if _queue is None:
            active, queued = queue_limits()
            _queue = ExecQueue(
                max_active=active,
                max_queued=queued,
                max_wait_seconds=queue_max_wait(),
            )

        return _queue


def reset_queue() -> None:
    """测试用：丢弃单例与其中排队的项。"""

    global _queue

    with _queue_lock:
        if _queue is not None:
            _queue.reset()
        _queue = None


_watchdog: threading.Thread | None = None
_watchdog_stop = threading.Event()


def start_watchdog() -> threading.Thread | None:
    """周期扫描排队项，把等太久的交给 expire 回调。幂等。"""

    global _watchdog

    if _watchdog is not None and _watchdog.is_alive():
        return _watchdog

    _watchdog_stop.clear()
    interval = queue_sweep_interval()

    def loop() -> None:
        while not _watchdog_stop.wait(interval):
            try:
                get_queue().sweep()
            except Exception:  # noqa: BLE001 - 看门狗不能崩
                pass

    _watchdog = threading.Thread(
        target=loop, name="exec-queue-watchdog", daemon=True
    )
    _watchdog.start()
    return _watchdog


def stop_watchdog() -> None:
    global _watchdog

    _watchdog_stop.set()

    if _watchdog is not None:
        _watchdog.join(timeout=5)
        _watchdog = None
