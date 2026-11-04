"""进程内活动 exec 注册表：取消与 TTL 回收都靠它找到正在跑的东西。

只记录「本进程正在执行」的 exec；状态真相仍然在 ``platform_execs`` 表里。
reaper 到期回收沙箱时用它先把活动 exec 取消掉，再改沙箱状态。
"""

from __future__ import annotations

import threading

from src.core.run_control import CancellationToken

_lock = threading.Lock()
_registry: dict[str, tuple[str, CancellationToken]] = {}


def register(exec_id: str, sandbox_id: str, token: CancellationToken) -> None:
    with _lock:
        _registry[exec_id] = (sandbox_id, token)


def unregister(exec_id: str) -> None:
    with _lock:
        _registry.pop(exec_id, None)


def cancel(exec_id: str) -> bool:
    with _lock:
        entry = _registry.get(exec_id)

    if entry is None:
        return False

    entry[1].cancel()
    return True


def cancel_for_sandbox(sandbox_id: str) -> int:
    """取消某沙箱下所有活动 exec；返回触发数量。"""

    with _lock:
        tokens = [
            token
            for registered_sandbox, token in _registry.values()
            if registered_sandbox == sandbox_id
        ]

    for token in tokens:
        token.cancel()

    return len(tokens)


def snapshot() -> dict[str, str]:
    with _lock:
        return {
            exec_id: sandbox_id
            for exec_id, (sandbox_id, _token) in _registry.items()
        }
