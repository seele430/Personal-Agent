import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
DB_PATH = BASE_DIR / "reminders.db"


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
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

    with sqlite3.connect(DB_PATH) as conn:
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