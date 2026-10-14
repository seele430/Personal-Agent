import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from src.config import paths

BASE_DIR = Path(__file__).resolve().parents[2]


# 连接写法说明：
#   with closing(sqlite3.connect(...)) as conn, conn:
# 内层 conn 负责事务，外层 closing 在退出时关闭连接。


def init_db():
    with closing(sqlite3.connect(paths.reminders_db)) as conn, conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message TEXT NOT NULL,
                remind_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
            )
        """)


def create_reminder(message: str, minutes: int):
    if minutes <= 0:
        raise ValueError("提醒时间必须大于 0 分钟")

    remind_at = datetime.now().astimezone() + timedelta(
        minutes=minutes
    )

    with closing(sqlite3.connect(paths.reminders_db)) as conn, conn:
        cursor = conn.execute(
            """
            INSERT INTO reminders
            (message, remind_at, status)
            VALUES (?, ?, 'pending')
            """,
            (message, remind_at.isoformat())
        )
        reminder_id = cursor.lastrowid

    return reminder_id, remind_at