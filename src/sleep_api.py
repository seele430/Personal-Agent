"""Sleep 运行记录只读 API。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from src.auth_api import get_current_user
from src.sleep_store import SleepRunStore


router = APIRouter(prefix="/api/sleep", tags=["Sleep"])


def _owner(user: dict) -> str:
    owner = user.get("id") if isinstance(user, dict) else None

    if not isinstance(owner, str) or not owner.strip():
        raise HTTPException(status_code=401, detail="未登录")

    return owner


@router.get("/runs")
def list_runs(
    response: Response,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    mode: str = "",
    execution_status: str = "",
    gate_status: str = "",
    user: dict = Depends(get_current_user),
):
    owner = _owner(user)
    response.headers["Cache-Control"] = "no-store"

    try:
        return SleepRunStore().list_runs(
            owner, limit=limit, offset=offset,
            mode=mode or None,
            execution_status=execution_status or None,
            gate_status=gate_status or None,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None


@router.get("/runs/{run_id}")
def get_run(
    run_id: int,
    response: Response,
    user: dict = Depends(get_current_user),
):
    owner = _owner(user)
    response.headers["Cache-Control"] = "no-store"
    run = SleepRunStore().get_run(owner, run_id)

    if run is None:
        raise HTTPException(status_code=404, detail="记录不存在")

    run.pop("owner_id", None)

    return run
