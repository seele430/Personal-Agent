import os
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def send_notification(message: str) -> bool:
    server = os.getenv(
        "NTFY_SERVER",
        "https://ntfy.sh"
    ).rstrip("/")

    topic = os.getenv("NTFY_TOPIC")

    if not topic:
        raise ValueError("未配置 NTFY_TOPIC")

    if not message.strip():
        raise ValueError("通知内容不能为空")

    response = requests.post(
        f"{server}/{topic}",
        data=message.encode("utf-8"),
        headers={
            "Content-Type": "text/plain; charset=utf-8"
        },
        timeout=15
    )

    response.raise_for_status()
    return True