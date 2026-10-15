

import logging
import time

from apscheduler.schedulers.background import BackgroundScheduler

from src.notification import send_notification
from src.config import paths
from src.core.reminder_store import ReminderStore
from src.tools.reminder import init_db

from src.core.monitor_registry import init_monitor_registry
from src.core.monitor_dispatcher import run_due_monitor_tasks


# =====================================
# Logging
# =====================================

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | %(levelname)s | "
        "%(name)s | %(message)s"
    ),
)

logger = logging.getLogger("personal_agent.scheduler")

# 避免每 5 秒输出一次 APScheduler 成功日志
logging.getLogger(
    "apscheduler.executors.default"
).setLevel(logging.WARNING)


# =====================================
# Reminder Scheduler
# =====================================

def check_reminders():
    """投递到期的 ntfy 提醒。

    取件口是**新表** `reminder_requests`（按人投递的权威记录），
    不再直接扫旧表 —— 旧表只是站长提醒的历史镜像，
    由 `ReminderStore.mark_sent()` 与新表同事务更新。

    另外保留一条**互斥**的兼容路径：处理没有任何新表记录指向的旧表
    待发行。它们只可能来自按人投递上线之前（旧的 create_reminder 已
    废弃），但直接不管等于把用户当时设好的提醒吞掉。
    两条路径不会重复推送同一条提醒。
    """

    store = ReminderStore(paths.reminders_db)

    try:
        due = store.due_ntfy()

    except Exception:
        logger.exception("读取到期提醒失败")
        return

    for row in due:
        try:
            send_notification(f"⏰ 提醒：{row['message']}")

            store.mark_sent(reminder_id=row["id"])

            logger.info(
                "提醒 %s 已提交到 ntfy（用户 %s）",
                row["id"],
                row["user_id"],
            )

        except Exception:
            # 保持 pending：下一个扫描周期重试。
            logger.exception(
                "提醒 %s 投递失败", row["id"]
            )

    try:
        legacy = store.legacy_only_due()

    except Exception:
        logger.exception("读取历史提醒失败")
        return

    for row in legacy:
        try:
            send_notification(f"⏰ 提醒：{row['message']}")

            store.mark_legacy_sent(
                legacy_reminder_id=row["id"]
            )

            logger.info(
                "历史提醒 %s 已提交到 ntfy", row["id"]
            )

        except Exception:
            logger.exception(
                "历史提醒 %s 投递失败", row["id"]
            )


# =====================================
# Database Monitor Dispatcher
# =====================================

def check_monitor_tasks():
    """检查数据库中的到期监控任务。"""

    try:
        count = run_due_monitor_tasks()

        logger.info(
            "数据库监控扫描完成，本轮领取 %s 个任务",
            count
        )

    except Exception:
        logger.exception(
            "数据库监控任务调度异常"
        )


# =====================================
# Main Scheduler
# =====================================

def main():

    # 初始化两个独立的任务数据库
    init_db()
    init_monitor_registry()

    scheduler = BackgroundScheduler()

    # 自然语言闹钟：每 5 秒检查
    scheduler.add_job(
        check_reminders,
        trigger="interval",
        seconds=5,
        id="check_reminders",
        max_instances=1,
        coalesce=True,
    )

    # 数据库监控任务：每 60 秒扫描
    scheduler.add_job(
        check_monitor_tasks,
        trigger="interval",
        seconds=60,
        id="check_monitor_tasks",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )

    scheduler.start()

    logger.info(
        "Personal Agent Scheduler 已启动"
    )

    logger.info(
        "已注册任务：%s",
        [job.id for job in scheduler.get_jobs()]
    )

    logger.info("按 Ctrl+C 停止")

    try:
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        logger.info("正在停止 Scheduler")

    finally:
        scheduler.shutdown(wait=True)
        logger.info("Scheduler 已停止")


if __name__ == "__main__":
    main()