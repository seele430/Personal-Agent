"""团队任务分配接口。

与其他模块并行存在，互不影响。
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.auth_api import get_current_user
from src.core.dispatcher import DispatchError, run_goal
from src.core.team_store import TeamError, TeamStore


router = APIRouter(tags=["团队任务"])

logger = logging.getLogger("personal_agent.teams")


def _open_store() -> TeamStore:
    return TeamStore()


def _owned_team(store: TeamStore, team_id: int, user: dict) -> dict:
    team = store.get_team(team_id)

    if team is None or team["owner_id"] != user["id"]:
        # 不存在与无权访问返回同一个响应，避免探测
        raise HTTPException(status_code=404, detail="团队不存在")

    return team


class TeamCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    max_workers: int = Field(default=3, ge=1, le=8)


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    skills: list[str] = Field(min_length=1, max_length=10)
    model: str | None = None


class GoalCreate(BaseModel):
    goal: str = Field(min_length=2, max_length=2000)


@router.post("/teams")
def create_team(
    payload: TeamCreate,
    user: dict = Depends(get_current_user),
):
    return _open_store().create_team(
        owner_id=user["id"],
        name=payload.name,
        max_workers=payload.max_workers,
    )


@router.get("/teams")
def list_teams(user: dict = Depends(get_current_user)):
    return {"teams": _open_store().list_teams(user["id"])}


@router.post("/teams/{team_id}/agents")
def add_agent(
    team_id: int,
    payload: AgentCreate,
    user: dict = Depends(get_current_user),
):
    store = _open_store()

    _owned_team(store, team_id, user)

    try:
        return store.add_agent(
            team_id=team_id,
            name=payload.name,
            skills=payload.skills,
            model=payload.model,
        )

    except TeamError as error:
        raise HTTPException(
            status_code=422, detail=str(error)
        ) from None


@router.get("/teams/{team_id}/agents")
def list_agents(
    team_id: int,
    user: dict = Depends(get_current_user),
):
    store = _open_store()

    _owned_team(store, team_id, user)

    return {"agents": store.list_agents(team_id)}


@router.post("/teams/{team_id}/goals")
def start_goal(
    team_id: int,
    payload: GoalCreate,
    user: dict = Depends(get_current_user),
):
    """同步跑完：分解 → 分派 → 执行 → 返回任务树。

    v1 同步执行；目标复杂时会占住请求一段时间。
    """

    store = _open_store()

    _owned_team(store, team_id, user)

    try:
        return run_goal(
            store=store,
            team_id=team_id,
            goal=payload.goal,
        )

    except DispatchError as error:
        raise HTTPException(
            status_code=422, detail=str(error)
        ) from None


@router.get("/tasks/{task_id}/tree")
def task_tree(
    task_id: int,
    user: dict = Depends(get_current_user),
):
    store = _open_store()

    task = store.get_task(task_id)

    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    _owned_team(store, task["team_id"], user)

    return store.task_tree(task_id)


@router.get("/tasks/{task_id}/events")
def task_events(
    task_id: int,
    user: dict = Depends(get_current_user),
):
    store = _open_store()

    task = store.get_task(task_id)

    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    _owned_team(store, task["team_id"], user)

    return {"events": store.events(task_id)}
