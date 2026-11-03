"""D16：TTL 回收（reaper）与 tombstone 清理。

契约（``docs/platform-contract.md`` 4.2 / 4.4）：

- ``ttl_seconds`` 从创建接受时间起算；到期后**先取消活动 exec、清理进程树**，
  再把沙箱置为 reclaimed；
- 已回收沙箱上的操作返回 410 ``SANDBOX_GONE``；
- tombstone 至少保留 24 小时；幂等记录至少与 tombstone 同寿；
- 两者都到期并清除后，旧 key 才可以按新请求处理。

``sweep_once()`` 是一次可注入 ``now`` 的纯函数式扫描，FLASK 线程只是定时调用它，
所以测试不需要等真实时间。
"""

from __future__ import annotations

import logging
import os
import threading

from datetime import datetime, timedelta, timezone
from typing import Any

from src.platform import active_execs
from src.platform.sandbox_service import SandboxService

logger = logging.getLogger("personal_agent.platform.reaper")

TOMBSTONE_HOURS_ENV = "PERSONAL_AGENT_TOMBSTONE_HOURS"
REAPER_ENV = "PERSONAL_AGENT_PLATFORM_REAPER"
REAPER_INTERVAL_ENV = "PERSONAL_AGENT_PLATFORM_REAPER_INTERVAL"

DEFAULT_TOMBSTONE_HOURS = 24.0
DEFAULT_INTERVAL_SECONDS = 60.0


def tombstone_hours() -> float:
    raw = os.getenv(TOMBSTONE_HOURS_ENV, "").strip()

    if not raw:
        return DEFAULT_TOMBSTONE_HOURS

    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TOMBSTONE_HOURS

    return value if value > 0 else DEFAULT_TOMBSTONE_HOURS


def reaper_interval_seconds() -> float:
    raw = os.getenv(REAPER_INTERVAL_ENV, "").strip()

    if not raw:
        return DEFAULT_INTERVAL_SECONDS

    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_INTERVAL_SECONDS

    return value if value > 0 else DEFAULT_INTERVAL_SECONDS


def sweep_once(now: datetime | None = None) -> dict[str, int]:
    """回收过期沙箱 + 清理过期 tombstone；返回本次处理数量。"""

    moment = now or datetime.now(timezone.utc)
    service = SandboxService()

    expired = service.list_expired(moment)
    cancelled = 0

    for sandbox in expired:
        # 先取消活动 exec：进程树由执行器负责清，沙箱状态随后才改。
        cancelled += active_execs.cancel_for_sandbox(sandbox["id"])
        service.reclaim(sandbox["id"], reason="ttl_expired")

    cleared = service.purge_tombstones(
        moment - timedelta(hours=tombstone_hours())
    )

    if expired or cleared:
        logger.info(
            "reaper: reclaimed=%s cancelled_execs=%s tombstones_cleared=%s",
            len(expired),
            cancelled,
            cleared,
        )

    return {
        "reclaimed": len(expired),
        "cancelled_execs": cancelled,
        "tombstones_cleared": cleared,
    }


class Reaper(threading.Thread):
    """定时调用 ``sweep_once``；daemon，随应用关闭一起停。"""

    def __init__(self, interval_seconds: float | None = None):
        super().__init__(name="platform-reaper", daemon=True)
        self.interval_seconds = (
            interval_seconds
            if interval_seconds is not None
            else reaper_interval_seconds()
        )
        self._stop = threading.Event()

    def run(self) -> None:  # noqa: D102
        while not self._stop.wait(self.interval_seconds):
            try:
                sweep_once()
            except Exception:  # noqa: BLE001 - 一次失败不能让线程死掉
                logger.exception("reaper sweep 失败")

    def stop(self) -> None:
        self._stop.set()


_instance: Reaper | None = None


def start_reaper() -> Reaper | None:
    """按环境开关启动后台回收线程；测试里默认关闭。"""

    global _instance

    if os.getenv(REAPER_ENV, "1").strip() in ("0", "off", "false"):
        return None

    if _instance is not None and _instance.is_alive():
        return _instance

    _instance = Reaper()
    _instance.start()
    return _instance


def stop_reaper() -> None:
    global _instance

    if _instance is not None:
        _instance.stop()
        _instance = None
