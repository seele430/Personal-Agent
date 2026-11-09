"""QQ 入站 HTTP 端点：NapCat（OneBot 11）→ 入队 → 立即 204。

双通道教训：OneBot 11 的上报响应里若带 `reply`，会触发“快速回复”；
我们固定走显式 `send_msg` 单通道，所以这里**所有完成分支都返回 204 空正文**。
"""

from __future__ import annotations

import asyncio
import json
import os

from pathlib import Path

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from src.qq.inbound import QQInbound
from src.qq.onebot import OneBotClient
from src.qq.outbox import QQOutbox
from src.qq.worker import QQWorker


router = APIRouter(tags=["qq"])

_inbound: QQInbound | None = None
_worker: QQWorker | None = None


def data_root() -> Path:
    return Path(
        os.getenv("PERSONAL_AGENT_DATA_DIR")
        or Path(__file__).resolve().parents[2] / "data"
    )


def build_client() -> OneBotClient:
    return OneBotClient(
        base_url=os.getenv("PA_ONEBOT_URL") or "",
        token=os.getenv("PA_ONEBOT_TOKEN") or "",
    )


def configured_inbound() -> QQInbound:
    global _inbound

    if _inbound is None:
        _inbound = QQInbound(
            owner_id=int(os.getenv("PA_QQ_OWNER") or 0),
            secret=os.getenv("PA_QQ_SECRET") or "",
            client=build_client(),
            enabled=(os.getenv("PA_QQ_ENABLED") or "").strip() == "1",
        )

    return _inbound


def configured_worker() -> QQWorker:
    global _worker

    if _worker is None:
        handler = configured_inbound()
        root = data_root()
        root.mkdir(parents=True, exist_ok=True)
        _worker = QQWorker(
            outbox=QQOutbox(root / "qq-outbox.db"),
            client=handler.client,
            handler=handler,
        )
        _worker.start()

    return _worker


def reset_inbound() -> None:
    """测试用：丢掉缓存的实例。"""

    global _inbound, _worker

    if _worker is not None:
        _worker.stop()

    _worker = None
    _inbound = None


@router.post("/api/qq/onebot")
async def onebot_event(request: Request):
    body = await request.body()
    handler = configured_inbound()

    if not handler.enabled:
        return Response(status_code=204)

    if not handler.verify(body, request.headers.get("X-Signature", "")):
        return JSONResponse(status_code=401, content={"error": "bad signature"})

    try:
        event = json.loads(body.decode("utf-8"))

    except (UnicodeDecodeError, json.JSONDecodeError):
        return JSONResponse(status_code=400, content={"error": "bad json"})

    job = await asyncio.to_thread(handler.accept, event)

    if job is not None:
        worker = configured_worker()
        enqueued = worker.outbox.enqueue(
            message_id=job["message_id"],
            sender=job["sender"],
            command=job["command"],
            question=job["question"],
        )

        if enqueued:
            handler.record_audit(job, "enqueued")
        else:
            handler.record_audit(job, "duplicate_ignored")

    # 始终 204 空正文：回复只走显式 send_msg
    return Response(status_code=204)
