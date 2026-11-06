"""进程内趋势采样（ring buffer）。

口径（V2 §4.3）：

- **单进程采样，重启即丢**，页面必须标注，不伪装成完整历史监控；
- 每个采样点记录：运行中 / 排队 / 实际容量 / 期望容量 / 最近决策动作；
- 读取时按时间窗过滤，样本量随数据一起返回（V2 §5：p50/p99 不得脱离窗口和样本量）。

采样线程默认开启（30 秒一次），成本是一次队列快照 + 一次节点表查询。
"""

from __future__ import annotations

import logging
import os
import threading
import time

from collections import deque
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("personal_agent.platform.trends")

WINDOW_SECONDS = 900
DEFAULT_INTERVAL_SECONDS = 30.0
MAX_SAMPLES = 480

INTERVAL_ENV = "PERSONAL_AGENT_TRENDS_INTERVAL"
ENABLED_ENV = "PERSONAL_AGENT_TRENDS"


def enabled() -> bool:
    return os.getenv(ENABLED_ENV, "1").strip().lower() not in (
        "",
        "0",
        "off",
        "false",
        "no",
    )


def interval_seconds() -> float:
    raw = os.getenv(INTERVAL_ENV, "").strip()

    if not raw:
        return DEFAULT_INTERVAL_SECONDS

    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_INTERVAL_SECONDS

    return value if value > 0 else DEFAULT_INTERVAL_SECONDS


class TrendBuffer:
    """有界环形缓冲；窗口过滤在读的时候做。"""

    def __init__(
        self,
        *,
        window_seconds: int = WINDOW_SECONDS,
        max_samples: int = MAX_SAMPLES,
    ):
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._samples: deque[dict[str, Any]] = deque(maxlen=max(2, max_samples))

    def record(
        self,
        *,
        running: int,
        queued: int,
        capacity: int,
        desired: int,
        action: str = "none",
        at: float | None = None,
    ) -> dict[str, Any]:
        moment = time.time() if at is None else float(at)

        sample = {
            "at": datetime.fromtimestamp(moment, timezone.utc).isoformat(),
            "_epoch": moment,
            "running": int(running),
            "queued": int(queued),
            "capacity": int(capacity),
            "desired": int(desired),
            "action": action,
        }

        with self._lock:
            self._samples.append(sample)

        public = dict(sample)
        public.pop("_epoch", None)

        return public

    def series(
        self,
        *,
        window_seconds: int | None = None,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        window = self.window_seconds if window_seconds is None else window_seconds
        moment = time.time() if now is None else float(now)
        floor = moment - window

        with self._lock:
            rows = [dict(sample) for sample in self._samples]

        result = []

        for sample in rows:
            if sample["_epoch"] < floor:
                continue

            sample.pop("_epoch", None)
            result.append(sample)

        return result

    def count(self, *, window_seconds: int | None = None, now: float | None = None) -> int:
        return len(self.series(window_seconds=window_seconds, now=now))

    def clear(self) -> None:
        with self._lock:
            self._samples.clear()


_buffer = TrendBuffer()


def get_buffer() -> TrendBuffer:
    return _buffer


def reset() -> None:
    _buffer.clear()


def record_sample(**kwargs) -> dict[str, Any]:
    return _buffer.record(**kwargs)


def series(*, window_seconds: int | None = None, now: float | None = None):
    return _buffer.series(window_seconds=window_seconds, now=now)


def sample_count(*, window_seconds: int | None = None, now: float | None = None) -> int:
    return _buffer.count(window_seconds=window_seconds, now=now)


def sample_once(*, now: float | None = None) -> dict[str, Any]:
    """采一次：队列 + 容量 + 期望容量 + 最近决策动作。"""

    from src.platform.autoscale import AutoscalePolicy, desired_capacity
    from src.platform.elastic_controller import capacity_snapshot, get_controller
    from src.platform.exec_queue import get_queue

    queue = get_queue().snapshot()
    running = int(queue.get("active", 0))
    queued = int(queue.get("queued", 0))

    controller = get_controller()
    policy = controller.policy if controller is not None else AutoscalePolicy()
    desired = desired_capacity(running, queued, policy)

    capacity = capacity_snapshot()["nodes_accepting"]

    action = "none"

    if controller is not None and controller.last_decision is not None:
        action = str(controller.last_decision.get("label") or "none")

    return record_sample(
        running=running,
        queued=queued,
        capacity=capacity,
        desired=desired,
        action=action,
        at=now,
    )


class TrendSampler(threading.Thread):
    """定时采样；daemon，随应用关闭一起停。"""

    def __init__(self, interval: float | None = None):
        super().__init__(name="platform-trends", daemon=True)
        # 名字不能叫 _stop：会覆盖 Thread._stop，is_alive() 会抛 TypeError。
        self._stopped = threading.Event()
        self.interval_seconds = interval if interval is not None else interval_seconds()

    def run(self) -> None:  # noqa: D102
        while not self._stopped.wait(self.interval_seconds):
            try:
                sample_once()
            except Exception:  # noqa: BLE001 - 一次失败不能让线程死掉
                logger.exception("趋势采样失败")

    def stop(self) -> None:
        self._stopped.set()


_instance: TrendSampler | None = None


def start_sampler() -> TrendSampler | None:
    global _instance

    if not enabled():
        return None

    if _instance is not None and _instance.is_alive():
        return _instance

    # 启动即采一次，页面打开时不会只有一条未来数据。
    try:
        sample_once()
    except Exception:  # noqa: BLE001
        logger.exception("首次趋势采样失败")

    _instance = TrendSampler()
    _instance.start()

    return _instance


def stop_sampler() -> None:
    global _instance

    if _instance is not None:
        _instance.stop()
        _instance = None
