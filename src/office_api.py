"""Office 转换 API：POST /api/office/convert。"""

from __future__ import annotations

import threading

from fastapi import APIRouter, Body, Depends, HTTPException

from src.artifacts.registry import ArtifactRegistry
from src.artifacts_api import _owner
from src.auth_api import get_current_user
from src.core.office_convert import OfficeError, convert_artifact, find_soffice
from src.platform.exec_service import Tier1Executor


router = APIRouter(prefix="/api/office", tags=["Office"])

_global_lock = threading.Lock()
_owner_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


def _acquire(owner_id: str) -> threading.Lock:
    if not _global_lock.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="已有转换在执行，请稍后再试")

    with _guard:
        lock = _owner_locks.setdefault(owner_id, threading.Lock())

    if not lock.acquire(blocking=False):
        _global_lock.release()
        raise HTTPException(status_code=429, detail="同一用户已有转换在执行")

    return lock


def _release(owner_id: str, lock: threading.Lock) -> None:
    lock.release()
    _global_lock.release()


@router.post("/convert")
def convert(
    payload: dict = Body(...),
    user: dict = Depends(get_current_user),
):
    owner_id = _owner(user)

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是对象")

    artifact_id = payload.get("source_artifact_id")

    if not isinstance(artifact_id, str) or not artifact_id.startswith("art_"):
        raise HTTPException(status_code=400, detail="source_artifact_id 缺失或格式错误")

    if find_soffice() is None:
        raise HTTPException(status_code=503, detail="LibreOffice 未安装或不可用")

    lock = _acquire(owner_id)

    try:
        result = convert_artifact(
            source_artifact_id=artifact_id,
            owner_id=owner_id,
            registry=ArtifactRegistry(),
            executor=Tier1Executor(),
        )
    except OfficeError as error:
        raise HTTPException(status_code=error.status, detail=error.code) from None
    finally:
        _release(owner_id, lock)

    return result.to_dict()
