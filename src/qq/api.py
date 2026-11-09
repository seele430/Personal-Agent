"""QQ 入站 HTTP 端点：NapCat（OneBot 11）→ Personal Agent。"""

from __future__ import annotations

import json
import os

from fastapi import APIRouter, Request
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

    status, payload = handler.handle(event)

    if status == 204:
        return JSONResponse(status_code=204, content=None)

    return JSONResponse(status_code=status, content=payload)
