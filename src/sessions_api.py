"""会话 API：列表 / 新建 / 消息 / 改名 / 置顶 / 软删。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.sessions import SessionError, SessionStore


router = APIRouter(tags=["会话"])


class CreateSessionBody(BaseModel):
    title: str = ""


class PatchSessionBody(BaseModel):
    title: str | None = None
    pinned: bool | None = None


def _store() -> SessionStore:
    return SessionStore()


@router.get("/sessions")
def list_sessions(limit: int = 50, include_deleted: bool = False):
    return {"sessions": _store().list(limit=limit, include_deleted=include_deleted)}


@router.post("/sessions")
def create_session(body: CreateSessionBody | None = None):
    title = (body.title if body else "") or ""

    return _store().create("web", title)


@router.get("/sessions/{session_id}")
def get_session(session_id: str, after: int = 0, limit: int = 200):
    store = _store()
    session = store.get(session_id)

    if session is None or session["deleted"]:
        raise HTTPException(status_code=404, detail="会话不存在")

    return {
        "session": session,
        "messages": store.messages(session_id, after=after, limit=limit),
    }


@router.patch("/sessions/{session_id}")
def patch_session(session_id: str, body: PatchSessionBody):
    store = _store()
    changed = False

    if body.title is not None:
        changed = store.rename(session_id, body.title) or changed

    if body.pinned is not None:
        changed = store.set_pinned(session_id, body.pinned) or changed

    if not changed:
        raise HTTPException(status_code=404, detail="会话不存在或无变更")

    return store.get(session_id)


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    if not _store().soft_delete(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")

    return {"ok": True, "soft_deleted": session_id}


@router.get("/sessions/by-prefix/{prefix}")
def sessions_by_prefix(prefix: str):
    try:
        return {"sessions": _store().find_by_prefix(prefix)}
    except SessionError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
