"""QQ 入站 HTTP 端点：NapCat（OneBot 11）→ 入队 → 立即 204。

入站只做四件事：验签、白名单、幂等、选队列。
完整路由在 task Worker；确认/取消在 control Worker。

双通道教训：上报响应不能带 `reply`，所有完成分支统一 204 空正文。
"""

from __future__ import annotations

import json
import os

from pathlib import Path

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from src.qq.inbound import QQInbound
from src.qq.onebot import OneBotClient
from src.qq.outbox import QQOutbox
from src.qq.worker import build_workers


router = APIRouter(tags=["qq"])

_inbound: QQInbound | None = None
_outbox: QQOutbox | None = None
_task_worker = None
_control_worker = None


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
        text_enabled = (os.getenv("PA_QQ_ENABLED") or "").strip() == "1"
        _inbound = QQInbound(
            owner_id=int(os.getenv("PA_QQ_OWNER") or 0),
            secret=os.getenv("PA_QQ_SECRET") or "",
            client=build_client(),
            enabled=text_enabled,
        )

    return _inbound


def configured_workers():
    global _outbox, _task_worker, _control_worker

    if _task_worker is None:
        handler = configured_inbound()
        root = data_root()
        root.mkdir(parents=True, exist_ok=True)
        _outbox, _task_worker, _control_worker = build_workers(
            db_path=str(root / "qq-outbox.db"),
            client=handler.client,
            handler=handler,
        )
        _task_worker.start()
        _control_worker.start()

    return _outbox, _task_worker, _control_worker


def reset_inbound() -> None:
    """测试用：停掉 worker、丢掉缓存。"""

    global _inbound, _outbox, _task_worker, _control_worker

    for worker in (_task_worker, _control_worker):
        if worker is not None:
            worker.stop()

    _task_worker = None
    _control_worker = None
    _outbox = None
    _inbound = None


def queue_for(text: str) -> str:
    stripped = (text or "").strip()

    if stripped.startswith("/confirm") or stripped.startswith("/cancel"):
        return "control"

    return "task"


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

    if event.get("post_type") == "meta_event":
        handler.handle_meta(event)

        return Response(status_code=204)

    if event.get("post_type") != "message" or event.get("message_type") != "private":
        return Response(status_code=204)

    sender = int((event.get("sender") or {}).get("user_id") or event.get("user_id") or 0)

    if sender != handler.owner_id:
        handler.record(event, "dropped:not-owner")

        return Response(status_code=204)

    message_id = str(event.get("message_id") or "")
    text = handler._text_of(event).strip()

    if not message_id or not text:
        return Response(status_code=204)

    outbox, _, _ = configured_workers()
    enqueued = outbox.enqueue(
        message_id=message_id,
        sender=sender,
        raw_text=text,
        queue=queue_for(text),
    )

    handler.record(
        event,
        "enqueued" if enqueued else "duplicate_ignored",
    )

    if enqueued:
        try:
            from src.sessions import SessionStore

            store = SessionStore()
            session = store.ensure_active(sender)
            store.add_message(session["id"], "user", text, message_id=message_id)
        except Exception:  # noqa: BLE001 - 会话记录失败不影响入站
            pass

    return Response(status_code=204)
