"""产物 API（/api/artifacts）：列表、元数据、预览、下载、reveal、登记。

安全口径（路线图 §8.9）：

- 一切以 `artifact_id` 为入口，**不接受客户端传路径去读文件**（登记除外，
  而登记也会被根目录校验挡住）；
- owner 鉴权走既有 `get_current_user`，跨 owner 一律 403；
- `reveal` 只允许**本机（loopback）owner**，且默认不打开外部程序 ——
  只有注入了 opener 才会真的 reveal（测试与后续桌面端用）。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse

from src.artifacts.models import (
    ArtifactError,
    ArtifactForbidden,
    ArtifactNotFound,
    MAX_ARTIFACT_BYTES,
)
from src.artifacts.preview import preview_payload
from src.artifacts.registry import get_registry
from src.auth_api import get_current_user


router = APIRouter(prefix="/api/artifacts", tags=["产物"])

#: 注入点：默认不真打开资源管理器；桌面端/测试可替换。
reveal_opener = None

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _owner(user: dict) -> str:
    owner_id = user.get("id")

    if not isinstance(owner_id, str) or not owner_id:
        raise HTTPException(status_code=401, detail="登录信息不完整")

    return owner_id


def can_reveal(host: str | None) -> bool:
    """只有本机来源才允许 reveal。

    要同时处理 `127.0.0.1:8000` 与 `[::1]:8000` 这种带端口的写法 ——
    直接 `split(":")[0]` 会把 IPv6 切成空串，等于把本机误判成远程。
    """

    if not host:
        return False

    text = host.strip().lower()

    if text.startswith("["):
        text = text[1:].split("]", 1)[0]

    elif text.count(":") == 1:
        text = text.split(":", 1)[0]

    return text in LOOPBACK_HOSTS


def _handle(callable_, *args, **kwargs):
    try:
        return callable_(*args, **kwargs)

    except ArtifactNotFound:
        raise HTTPException(status_code=404, detail="产物不存在") from None

    except ArtifactForbidden:
        raise HTTPException(status_code=403, detail="无权访问该产物") from None

    except ArtifactError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None


@router.get("")
def list_artifacts(
    run_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
    user: dict = Depends(get_current_user),
) -> dict:
    registry = get_registry()
    owner_id = _owner(user)
    items = _handle(registry.list, owner_id, run_id=run_id, limit=limit)

    return {
        "items": [
            {
                "id": item.id,
                "title": item.title,
                "kind": item.kind,
                "media_type": item.media_type,
                "size_bytes": item.size_bytes,
                "sha256": item.sha256,
                "run_id": item.run_id,
                "exec_id": item.exec_id,
                "created_at": item.created_at,
            }
            for item in items
        ]
    }


@router.post("/register")
def register_artifact(
    payload: dict,
    user: dict = Depends(get_current_user),
) -> dict:
    """登记一个已在产物根目录里的文件（W5 的生成物走这里）。"""

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是对象")

    raw_path = payload.get("path")

    if not isinstance(raw_path, str) or not raw_path.strip():
        raise HTTPException(status_code=400, detail="path 不能为空")

    registry = get_registry()
    owner_id = _owner(user)

    artifact = _handle(
        registry.register,
        owner_id,
        raw_path,
        title=payload.get("title"),
        media_type=payload.get("media_type") or "",
        run_id=payload.get("run_id") or "",
        exec_id=payload.get("exec_id") or "",
    )

    return {
        "id": artifact.id,
        "title": artifact.title,
        "kind": artifact.kind,
        "size_bytes": artifact.size_bytes,
        "sha256": artifact.sha256,
    }


@router.get("/{artifact_id}")
def artifact_detail(
    artifact_id: str,
    user: dict = Depends(get_current_user),
) -> dict:
    registry = get_registry()
    artifact = _handle(registry.get, artifact_id, owner_id=_owner(user))

    return {
        "id": artifact.id,
        "title": artifact.title,
        "kind": artifact.kind,
        "media_type": artifact.media_type,
        "size_bytes": artifact.size_bytes,
        "sha256": artifact.sha256,
        "rel_path": artifact.rel_path,
        "run_id": artifact.run_id,
        "exec_id": artifact.exec_id,
        "created_at": artifact.created_at,
        "previewable": artifact.is_previewable,
    }


@router.get("/{artifact_id}/preview")
def artifact_preview(
    artifact_id: str,
    user: dict = Depends(get_current_user),
) -> dict:
    registry = get_registry()
    owner_id = _owner(user)
    artifact = _handle(registry.get, artifact_id, owner_id=owner_id)
    path = _handle(registry.absolute_path, artifact_id, owner_id=owner_id)

    return _handle(preview_payload, artifact, path)


@router.get("/{artifact_id}/diff/{other_id}")
def artifact_diff(
    artifact_id: str,
    other_id: str,
    user: dict = Depends(get_current_user),
) -> dict:
    """比较两个产物的内容差异（文本 unified diff / 二进制摘要）。"""

    from src.diff.compare import DiffError, compare_files

    registry = get_registry()
    owner_id = _owner(user)

    # 路径一律经 absolute_path（自带 owner 校验），越权由 _handle 映射成 403/404
    a_path = _handle(registry.absolute_path, artifact_id, owner_id=owner_id)
    b_path = _handle(registry.absolute_path, other_id, owner_id=owner_id)

    try:
        result = compare_files(a_path, b_path)

    except DiffError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None

    return result.to_dict()

@router.get("/{artifact_id}/content")
def artifact_content(
    artifact_id: str,
    inline: int = Query(default=0, ge=0, le=1),
    user: dict = Depends(get_current_user),
) -> FileResponse:
    registry = get_registry()
    owner_id = _owner(user)
    artifact = _handle(registry.get, artifact_id, owner_id=owner_id)
    path = _handle(registry.absolute_path, artifact_id, owner_id=owner_id)

    return FileResponse(
        path=Path(path),
        media_type=artifact.media_type or "application/octet-stream",
        filename=artifact.title,
        content_disposition_type="inline" if inline else "attachment",
    )


@router.post("/{artifact_id}/reveal")
def artifact_reveal(
    artifact_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    registry = get_registry()
    owner_id = _owner(user)
    artifact = _handle(registry.get, artifact_id, owner_id=owner_id)

    host = request.client.host if request.client else None

    if not can_reveal(host):
        return {
            "revealed": False,
            "reason": "仅本机可用 reveal，远程端只提供下载",
        }

    if reveal_opener is None:
        return {"revealed": False, "reason": "当前环境未启用 reveal"}

    path = _handle(registry.absolute_path, artifact_id, owner_id=owner_id)

    try:
        reveal_opener(path, artifact)

    except Exception as error:  # noqa: BLE001 - reveal 失败不该 500 掉整个页面
        return {"revealed": False, "reason": f"reveal 失败：{type(error).__name__}"}

    return {"revealed": True, "reason": ""}


@router.get("/_limits/max-bytes")
def artifact_limits() -> dict:
    """给前端提示：单文件上限多少（避免上传后才被拒）。"""

    return {"max_bytes": MAX_ARTIFACT_BYTES}
