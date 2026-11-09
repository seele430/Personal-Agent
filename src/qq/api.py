"""QQ 入站 HTTP 端点：NapCat（OneBot 11）→ Personal Agent。"""

from __future__ import annotations

import asyncio
import json
import os

from fastapi import APIRouter, Request
from fastapi import Response
from fastapi.responses import JSONResponse

from src.qq.inbound import QQInbound
from src.qq.onebot import OneBotClient


router = APIRouter(tags=["qq"])

_inbound: QQInbound | None = None


def configured_inbound() -> QQInbound:
    global _inbound

    if _inbound is None:
        _inbound = QQInbound(
            owner_id=int(os.getenv("PA_QQ_OWNER") or 0),
            secret=os.getenv("PA_QQ_SECRET") or "",
            client=OneBotClient(
                base_url=os.getenv("PA_ONEBOT_URL") or "",
                token=os.getenv("PA_ONEBOT_TOKEN") or "",
            ),
            enabled=(os.getenv("PA_QQ_ENABLED") or "").strip() == "1",
        )

    return _inbound


def _audit_inbound(event: dict) -> None:
    from pathlib import Path as _Path

    sender = (event.get("sender") or {}).get("user_id") or event.get("user_id")

    try:
        root = _Path(os.getenv("PERSONAL_AGENT_DATA_DIR") or _Path(__file__).resolve().parents[2] / "data")
        root.mkdir(parents=True, exist_ok=True)

        with (root / "qq-audit.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "action": "inbound",
                        "message_id": str(event.get("message_id") or ""),
                        "sender": sender,
                        "message_type": event.get("message_type"),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except OSError:
        pass


def reset_inbound() -> None:
    """测试用：丢掉缓存的实例。"""

    global _inbound

    _inbound = None


@router.post("/api/qq/onebot")
async def onebot_event(request: Request):
    body = await request.body()
    handler = configured_inbound()

    if not handler.enabled:
        return JSONResponse(status_code=204, content=None)

    if not handler.verify(body, request.headers.get("X-Signature", "")):
        return JSONResponse(status_code=401, content={"error": "bad signature"})

    try:
        event = json.loads(body.decode("utf-8"))

    except (UnicodeDecodeError, json.JSONDecodeError):
        return JSONResponse(status_code=400, content={"error": "bad json"})

    if (os.getenv("PA_QQ_AUDIT") or "").strip() == "1" and event.get("post_type") == "message":
        _audit_inbound(event)

    await asyncio.to_thread(handler.handle, event)

    # OneBot 11：HTTP 上报响应里的 reply 会触发“快速回复”。
    # 我们固定走显式 send_msg 单通道，所以这里必须返回 204 空正文，
    # 否则同一条回复会被发两次（一次显式、一次快速回复）。
    return Response(status_code=204)
