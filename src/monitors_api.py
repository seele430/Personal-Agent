"""监控任务的个人接口（owner / admin）。

监控任务的设计是"**全局调度、按归属隔离**"：
调度器对所有人一视同仁地跑到期任务（谁离线也不影响），
但每个账号只能看到、暂停、恢复、归档**自己的**任务。

为什么创建只开放给站长：现在只有一个全局 `NTFY_TOPIC`，
通知实际只会发到站长的手机。开放给别人创建，会让他们以为
"我建的监控会通知我"，而那是不成立的。等按人投递落地后，
把 `MONITOR_CREATE_ROLES` 放宽即可 —— 归属字段早就写好了。
"""

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from src.auth_api import get_current_user
from src.core import monitor_registry
from src.monitors.url_policy import UrlPolicyError


router = APIRouter(tags=["监控任务"])

# 谁能创建：见模块开头。
MONITOR_CREATE_ROLES = frozenset({"owner"})

# 谁能查看 / 暂停 / 恢复 / 归档（各自只看自己的）。
MONITOR_MANAGE_ROLES = frozenset({"owner", "admin"})

DEFAULT_INTERVAL_MINUTES = 30
MIN_INTERVAL_MINUTES = 15
MAX_INTERVAL_MINUTES = 24 * 60


class MonitorCreate(BaseModel):
    source_url: str
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES


def _require_manage_role(user: dict) -> None:
    if user.get("role") not in MONITOR_MANAGE_ROLES:
        raise HTTPException(
            status_code=403,
            detail="没有管理监控任务的权限",
        )


def _not_found() -> HTTPException:
    # 别人的任务与不存在的任务返回同一个响应，
    # 避免用任务 ID 探测他人数据。
    return HTTPException(
        status_code=404,
        detail="监控任务不存在或无权访问",
    )


@router.get("/monitors")
def list_monitors(
    response: Response,
    user: dict = Depends(get_current_user),
):
    """列出当前用户自己的监控任务。"""

    response.headers["Cache-Control"] = "no-store"

    _require_manage_role(user)

    return {
        "monitors": monitor_registry.list_monitor_tasks(
            user["id"]
        )
    }


@router.post("/monitors", status_code=201)
def create_monitor(
    payload: MonitorCreate,
    response: Response,
    user: dict = Depends(get_current_user),
):
    """创建一个 RSS 监控任务。"""

    response.headers["Cache-Control"] = "no-store"

    if user.get("role") not in MONITOR_CREATE_ROLES:
        raise HTTPException(
            status_code=403,
            detail="当前只有站长可以创建监控任务",
        )

    if not (
        MIN_INTERVAL_MINUTES
        <= payload.interval_minutes
        <= MAX_INTERVAL_MINUTES
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                f"检查间隔必须是 "
                f"{MIN_INTERVAL_MINUTES}～"
                f"{MAX_INTERVAL_MINUTES} 分钟的整数"
            ),
        )

    try:
        task_id = monitor_registry.create_monitor_task(
            user["id"],
            payload.source_url,
            payload.interval_minutes,
        )

    except UrlPolicyError as error:
        raise HTTPException(
            status_code=422,
            detail=str(error),
        ) from None

    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail=str(error),
        ) from None

    return {
        "id": task_id,
        "source_url": payload.source_url.strip(),
        "interval_minutes": payload.interval_minutes,
        "status": "active",
    }


@router.post("/monitors/{task_id}/pause")
def pause_monitor(
    task_id: str,
    response: Response,
    user: dict = Depends(get_current_user),
):
    """暂停一条监控任务。"""

    response.headers["Cache-Control"] = "no-store"

    _require_manage_role(user)

    if not monitor_registry.pause_monitor_task(
        user["id"], task_id
    ):
        raise _not_found() from None

    return {"id": task_id, "status": "paused"}


@router.post("/monitors/{task_id}/resume")
def resume_monitor(
    task_id: str,
    response: Response,
    user: dict = Depends(get_current_user),
):
    """恢复一条已暂停的监控任务。"""

    response.headers["Cache-Control"] = "no-store"

    _require_manage_role(user)

    if not monitor_registry.resume_monitor_task(
        user["id"], task_id
    ):
        raise _not_found() from None

    return {"id": task_id, "status": "active"}


@router.delete("/monitors/{task_id}")
def archive_monitor(
    task_id: str,
    response: Response,
    user: dict = Depends(get_current_user),
):
    """归档（软删除）一条监控任务。"""

    response.headers["Cache-Control"] = "no-store"

    _require_manage_role(user)

    if not monitor_registry.archive_monitor_task(
        user["id"], task_id
    ):
        raise _not_found() from None

    return {"id": task_id, "status": "archived"}
