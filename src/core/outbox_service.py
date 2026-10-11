import logging
import time

from src.core.outbox_worker import process_one_notification


logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | %(levelname)s | "
        "%(name)s | %(message)s"
    ),
)

logger = logging.getLogger("personal_agent.outbox")


def main():
    interval = 15

    logger.info("Outbox Service 已启动")

    try:
        while True:
            delay = interval

            try:
                process_one_notification()

            except Exception:
                logger.exception(
                    "Outbox Worker 出现未预料的异常"
                )
                delay = 60

            time.sleep(delay)

    except KeyboardInterrupt:
        logger.info("Outbox Service 已停止")


if __name__ == "__main__":
    main()