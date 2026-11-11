"""记忆 API：owner 绑定写入、召回、按 ID 读历史。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from pydantic import BaseModel, ConfigDict

from src.auth_api import get_current_user
from src.memory import service
from src.memory.models import MemoryError


router = APIRouter(prefix="/api/memory", tags=["Memory"])


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
    query: str = Query(..., min_length=1, max_length=500),
    limit: int = Query(5, ge=1, le=50),
    user: dict = Depends(get_current_user),
):
    owner = _owner(user)
    response.headers["Cache-Control"] = "no-store"

    try:
        result = service.recall(owner, query, k=limit)
    except MemoryError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None

    return {
        "mode": result["mode"],
        "items": [
            {
                "id": hit.item.id, "key": hit.item.key, "text": hit.item.text,
                "trust": hit.item.trust, "valid_from": hit.item.valid_from,
                "supersedes_id": hit.item.supersedes_id,
                "rank": hit.rank, "score": hit.score,
            }
            for hit in result["hits"]
        ],
    }


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
        raise HTTPException(status_code=404, detail="记忆不存在")

    return {
        "id": item.id, "key": item.key, "kind": item.kind, "layer": item.layer,
        "text": item.text, "trust": item.trust,
        "valid_from": item.valid_from, "valid_to": item.valid_to,
        "supersedes_id": item.supersedes_id, "created_at": item.created_at,
    }
