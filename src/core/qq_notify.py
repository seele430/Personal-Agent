"""QQ 通知发送：复用既有 OneBotClient，只允许本机 loopback。

边界：
- 只允许站长私聊，不支持群/多收件人；
- 正文截断 1000 个 Unicode 字符；
- 配置错误抛 ValueError（永久失败），网络/OneBot 错误抛 OneBotError（可重试）；
- token 不写日志或异常。
"""

from __future__ import annotations

import os

from urllib.parse import urlsplit

from src.qq.onebot import OneBotClient, OneBotError


MAX_CHARS = 1000
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _qq_owner() -> int:
    raw = (os.getenv("PA_QQ_OWNER") or "").strip()

    if not raw.isdigit() or int(raw) <= 0:
        raise ValueError("PA_QQ_OWNER 必须是正整数")

    return int(raw)


def _base_url() -> str:
    raw = (os.getenv("PA_ONEBOT_URL") or "").strip()
    parts = urlsplit(raw)

    if (
        parts.scheme not in ("http", "https")
        or parts.hostname not in LOOPBACK_HOSTS
        or parts.username is not None
        or parts.password is not None
    ):
        raise ValueError("PA_ONEBOT_URL 必须是本机 loopback 地址")

    return raw.rstrip("/")


def send_qq_notification(message, *, client=None) -> None:
    text = str(message or "").strip()

    if not text:
        raise ValueError("通知内容不能为空")

    text = text[:MAX_CHARS]
    owner = _qq_owner()

    if client is None:
        token = (os.getenv("PA_ONEBOT_TOKEN") or "").strip()

        if not token:
            raise ValueError("PA_ONEBOT_TOKEN 未配置")

        client = OneBotClient(base_url=_base_url(), token=token)

    client.send_private_msg(owner, text)
