"""记忆 API：当前列表 / 搜索 / 详情 / 历史链。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from pydantic import BaseModel, ConfigDict

from src.auth_api import get_current_user
from src.memory import service
from src.memory.models import MemoryError


router = APIRouter(prefix="/api/memory", tags=["Memory"])

HISTORY_ERRORS = {
    "history_cycle": 409,
    "history_forked": 409,
    "history_broken": 409,
    "history_owner_mismatch": 409,
    "history_too_long": 409,
}


class RememberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    key: str = ""
    kind: str = "project_fact"
    layer: str = "project"


def _owner(user: dict) -> str:
    owner = user.get("id") if isinstance(user, dict) else None

    if not isinstance(owner, str) or not owner.strip():
        raise HTTPException(status_code=401, detail="未登录")

    return owner


def _item(item) -> dict:
    return {
        "id": item.id, "key": item.key, "kind": item.kind, "layer": item.layer,
        "text": item.text, "trust": item.trust,
        "valid_from": item.valid_from, "valid_to": item.valid_to,
        "supersedes_id": item.supersedes_id, "created_at": item.created_at,
    }


@router.post("/items", status_code=201)
def create_item(
    payload: RememberRequest,
    response: Response,
    user: dict = Depends(get_current_user),
):
    owner = _owner(user)
    response.headers["Cache-Control"] = "no-store"

    try:
        item = service.remember(
            owner, payload.text, key=payload.key,
            kind=payload.kind, layer=payload.layer,
        )
    except MemoryError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None

    return {
        "id": item.id, "key": item.key, "kind": item.kind, "layer": item.layer,
        "trust": item.trust, "valid_from": item.valid_from,
        "supersedes_id": item.supersedes_id,
    }


@router.get("/items")
def list_items(
    response: Response,
    query: str = Query("", max_length=500),
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    owner = _owner(user)
    response.headers["Cache-Control"] = "no-store"
    text = (query or "").strip()

    if text:
        if offset > 0:
            raise HTTPException(status_code=422, detail="search_pagination_unsupported")

        try:
            result = service.recall(owner, text, k=limit)
        except MemoryError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None

        return {
            "mode": "search",
            "items": [
                {
                    **_item(hit.item), "rank": hit.rank, "score": hit.score,
                }
                for hit in result["hits"]
            ],
            "count": len(result["hits"]),
            "limit": limit,
            "search_mode": result["mode"],
        }

    return service.list_current(owner, limit=limit, offset=offset)


@router.get("/items/{item_id}")
def get_item(
    item_id: int,
    response: Response,
    user: dict = Depends(get_current_user),
):
    owner = _owner(user)
    response.headers["Cache-Control"] = "no-store"
    item = service.get_item(owner, item_id)

    if item is None:
        raise HTTPException(status_code=404, detail="memory_not_found")

    return _item(item)


@router.delete("/items/{item_id}", status_code=204)
def delete_item(
    item_id: int,
    response: Response,
    user: dict = Depends(get_current_user),
):
    owner = _owner(user)
    response.headers["Cache-Control"] = "no-store"

    if not service.delete(owner, item_id):
        raise HTTPException(status_code=404, detail="memory_not_found")

    return Response(status_code=204)


@router.get("/items/{item_id}/history")
def get_history(
    item_id: int,
    response: Response,
    user: dict = Depends(get_current_user),
):
    owner = _owner(user)
    response.headers["Cache-Control"] = "no-store"

    try:
        return service.history(owner, item_id)
    except service.HistoryError as error:
        status = 404 if error.code == "memory_not_found" else HISTORY_ERRORS.get(error.code, 409)
        raise HTTPException(status_code=status, detail=error.code) from None
