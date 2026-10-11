

import logging
import sqlite3
import time

from contextlib import closing
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

from src.notification import send_notification
from src.tools.reminder import DB_PATH, init_db

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
    """检查并发送到期提醒。"""

    now = datetime.now().astimezone()

    try:
        with closing(
            sqlite3.connect(DB_PATH, timeout=10)
        ) as conn:
            reminders = conn.execute(
                """
                SELECT id, message, remind_at
                FROM reminders
                WHERE status = 'pending'
                """
            ).fetchall()

    except Exception:
        logger.exception("读取提醒任务失败")
        return

    for reminder_id, message, remind_at in reminders:
        try:
            due = datetime.fromisoformat(remind_at)

            if due > now:
                continue

            send_notification(f"⏰ 提醒：{message}")

            with closing(
                sqlite3.connect(DB_PATH, timeout=10)
            ) as conn:
                with conn:
                    conn.execute(
                        """
                        UPDATE reminders
                        SET status = 'sent'
                        WHERE id = ?
                          AND status = 'pending'
                        """,
                        (reminder_id,)
                    )

            logger.info(
                "提醒 %s 已提交到 ntfy",
                reminder_id
            )

        except Exception:
            logger.exception(
                "提醒 %s 处理失败",
                reminder_id
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