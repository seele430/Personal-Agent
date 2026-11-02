"""助手 run 的协作式取消与活动请求索引。

取消会先进入当前模型流或工具上下文。关闭 HTTP 连接本身不被视为完成取消；
runner 进程树取消由后续平台执行链路复用同一令牌语义。
"""

from __future__ import annotations

import threading
from collections.abc import Callable


class ChatCancelled(RuntimeError):
    """当前 run 已被用户或断流显式取消。"""


class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._cancellers: dict[int, Callable[[], None]] = {}
        self._next_id = 0

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self.cancelled:
            raise ChatCancelled("当前回复已取消")

    def register_canceller(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register an active operation; an already-cancelled token fires now."""

        with self._lock:
            if self.cancelled:
                callback()
                return lambda: None
            handle = self._next_id
            self._next_id += 1
            self._cancellers[handle] = callback

        def unregister() -> None:
            with self._lock:
                self._cancellers.pop(handle, None)

        return unregister

    def cancel(self) -> None:
        with self._lock:
            if self.cancelled:
                return
            self._event.set()
            callbacks = list(self._cancellers.values())
        for callback in callbacks:
            callback()


class ActiveRunRegistry:
    """Process-local index; ownership is part of every key."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[tuple[str, str], CancellationToken] = {}

    def start(self, user_id: str, request_id: str) -> CancellationToken:
        key = (user_id, request_id)
        token = CancellationToken()
        with self._lock:
            if key in self._runs:
                raise RuntimeError("相同请求已经在执行")
            self._runs[key] = token
        return token

    def cancel(self, user_id: str, request_id: str) -> bool:
        with self._lock:
            token = self._runs.get((user_id, request_id))
        if token is None:
            return False
        token.cancel()
        return True

    def finish(self, user_id: str, request_id: str) -> None:
        with self._lock:
            self._runs.pop((user_id, request_id), None)


active_runs = ActiveRunRegistry()
