"""数据源注册表 —— Source → Condition → Action 里的 Source 一层。

调度器不再硬编码 RSS：它按任务的 `task_type`（= source_type）查表拿
handler。**新增一种数据源 = 写一个 handler + 在这里注册一行**，
引擎其余部分（租约、退避、频率、历史、通知队列）完全不动。

handler 约定：

    handler(monitor_id: str, source_url: str) -> int   # 返回新增条目数

handler 内部负责"取数据 → 判断有没有新条目 → 写通知队列"。
判断与动作**共用同一个事务**（`detect_new_items` 里的 BEGIN IMMEDIATE），
这是"新增条目恰好通知一次"的前提，详见 docs/TASK_ENGINE.md。
"""

from src.monitors.http_text import run_http_text_monitor
from src.monitors.rss_monitor import run_rss_monitor


DEFAULT_SOURCE_TYPE = "rss"


class UnknownSourceType(ValueError):
    """任务声明了引擎不认识的数据源类型。"""


def _run_rss(monitor_id: str, source_url: str) -> int:
    return run_rss_monitor(
        monitor_id=monitor_id,
        feed_url=source_url,
    )


SOURCE_HANDLERS = {
    "rss": _run_rss,
    "http_text": run_http_text_monitor,
}


def known_source_types() -> list[str]:
    return sorted(SOURCE_HANDLERS)


def require_source_type(source_type: str) -> str:
    """校验数据源类型，返回它本身（便于链式使用）。"""

    if source_type not in SOURCE_HANDLERS:
        raise UnknownSourceType(
            f"不支持的数据源类型：{source_type!r}，"
            f"可用：{known_source_types()}"
        )

    return source_type


def run_source(
    *,
    source_type: str,
    monitor_id: str,
    source_url: str,
) -> int:
    """执行一次数据源检查，返回新增条目数。"""

    require_source_type(source_type)

    handler = SOURCE_HANDLERS[source_type]

    return handler(monitor_id, source_url)
