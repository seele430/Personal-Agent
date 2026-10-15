from src.monitors.rss import fetch_rss
from src.core.task_engine import detect_new_items
from src.core.access_policy import decide_access


def run_rss_monitor(
    monitor_id: str,
    feed_url: str
) -> int:
    """
    执行一次 RSS 监控检查。

    返回:
        新增文章数量

    异常:
        网络或 HTTP 错误直接抛出，
        交由 Dispatcher 记录失败。
    """

    print(f"开始检查 RSS：{monitor_id}")

    try:
        articles = fetch_rss(feed_url)

    except Exception as exc:
        """
        不吞异常。

        Dispatcher 需要知道任务失败，
        否则会把失败误认为 0 条更新。
        """

        status_code = None

        response = getattr(
            exc,
            "response",
            None
        )

        if response is not None:
            status_code = response.status_code

            decision = decide_access(
                status_code
            )

            print(
                f"RSS HTTP失败: {decision.reason}"
            )

            print(
                f"处理策略: {decision.action.value}"
            )

        else:
            print(
                f"RSS 网络失败: {exc}"
            )

        raise


    new_articles = detect_new_items(
        monitor_id,
        articles,
        label="RSS 更新",
    )


    print(
        f"读取文章：{len(articles)} 篇"
    )

    print(
        f"发现新增：{len(new_articles)} 篇"
    )


    print(
        f"新增文章已写入 Outbox：{len(new_articles)} 条"
    )


    return len(new_articles)