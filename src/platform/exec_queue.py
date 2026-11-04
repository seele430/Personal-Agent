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

from collections import deque
from dataclasses import dataclass, field
from typing import Callable

ACTIVE_ENV = "PERSONAL_AGENT_PLATFORM_MAX_ACTIVE_EXECS"
QUEUE_ENV = "PERSONAL_AGENT_PLATFORM_QUEUE_SIZE"

DEFAULT_MAX_ACTIVE = 4
DEFAULT_QUEUE_SIZE = 16
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


@dataclass
class QueuedExec:
    exec_id: str
    sandbox_id: str
    argv: list[str]
    timeout_ms: int
    max_output_bytes: int
    traceparent: str | None = None
    payload: dict = field(default_factory=dict)


class ExecQueue:
    """并发闸门 + 有界等待队列。"""

    def __init__(self, *, max_active: int, max_queued: int):
        self.max_active = max_active
        self.max_queued = max_queued
        self._active = 0
        self._queue: deque[QueuedExec] = deque()
        self._lock = threading.Lock()
        self._start: Callable[[QueuedExec], None] | None = None

    def start_with(self, starter: Callable[[QueuedExec], None]) -> None:
        self._start = starter

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
        """一个活跃 exec 结束；有空位就把队首放出去。"""

        next_item: QueuedExec | None = None

        with self._lock:
            self._active = max(0, self._active - 1)

            if self._queue and self._active < self.max_active:
                next_item = self._queue.popleft()
                self._active += 1
                starter = self._start
            else:
                starter = None

        if next_item is not None and starter is not None:
            starter(next_item)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "active": self._active,
                "queued": len(self._queue),
                "max_active": self.max_active,
                "max_queued": self.max_queued,
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
            _queue = ExecQueue(max_active=active, max_queued=queued)

        return _queue


def reset_queue() -> None:
    """测试用：丢弃单例与其中排队的项。"""

    global _queue

    with _queue_lock:
        if _queue is not None:
            _queue.reset()
        _queue = None
