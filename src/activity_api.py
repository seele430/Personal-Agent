"""活动摘要 API：GET /api/activity/{trace_id}/summary。

只读、无副作用；没有 span 也返回 200 + "暂无活动记录"。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from src.activity.summary import summarize_trace
from src.auth_api import get_current_user


router = APIRouter(prefix="/api/activity", tags=["活动摘要"])


@router.get("/{trace_id}/summary")
def activity_summary(
    trace_id: str,
    user: dict = Depends(get_current_user),
) -> dict:
    try:
        summary = summarize_trace(trace_id)

    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None

    return summary.to_dict()
