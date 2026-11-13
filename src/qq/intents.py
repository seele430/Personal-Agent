"""意图契约与静态注册表。

九字段 `IntentSpec`：name / params_schema / risk / policy / handler /
timeout_seconds / retry_policy / output_kind / enabled。

注册表是**静态字典**，不做动态发现；`handler` 是受信任的键，不是模块路径。
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class StatusParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AskParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=500)


class ReadFileParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=500)


class CancelParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str = ""


class ConfirmParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation_id: str = ""


class ResetParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ContinueRunParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=8, max_length=64)
    message: str = Field(min_length=1, max_length=2000)


class DenyParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=2, max_length=2000)


class SessionsParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UseSessionParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prefix: str = Field(min_length=6, max_length=36)


class NewSessionParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PosterParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=2, max_length=500)


@dataclass(frozen=True)
class IntentSpec:
    name: str
    params_schema: type[BaseModel]
    risk: str
    policy: str
    handler: str
    timeout_seconds: float
    retry_policy: str
    output_kind: str
    enabled: bool


STATUS_SPEC = IntentSpec(
    name="status",
    params_schema=StatusParams,
    risk="low",
    policy="allow",
    handler="status",
    timeout_seconds=5.0,
    retry_policy="none",
    output_kind="text",
    enabled=True,
)

ASK_SPEC = IntentSpec(
    name="ask",
    params_schema=AskParams,
    risk="low",
    policy="allow",
    handler="ask",
    timeout_seconds=60.0,
    retry_policy="none",
    output_kind="text",
    enabled=True,
)

READ_FILE_SPEC = IntentSpec(
    name="read_file",
    params_schema=ReadFileParams,
    risk="medium",
    policy="confirm",
    handler="read_file",
    timeout_seconds=10.0,
    retry_policy="none",
    output_kind="text",
    enabled=True,
)

CANCEL_SPEC = IntentSpec(
    name="cancel",
    params_schema=CancelParams,
    risk="low",
    policy="allow",
    handler="cancel",
    timeout_seconds=5.0,
    retry_policy="none",
    output_kind="text",
    enabled=True,
)

CONFIRM_SPEC = IntentSpec(
    name="confirm",
    params_schema=ConfirmParams,
    risk="medium",
    policy="allow",
    handler="confirm",
    timeout_seconds=5.0,
    retry_policy="none",
    output_kind="text",
    enabled=True,
)

RESET_SPEC = IntentSpec(
    name="reset",
    params_schema=ResetParams,
    risk="low",
    policy="allow",
    handler="reset",
    timeout_seconds=5.0,
    retry_policy="none",
    output_kind="text",
    enabled=True,
)

CONTINUE_RUN_SPEC = IntentSpec(
    name="continue_run",
    params_schema=ContinueRunParams,
    risk="low",
    policy="allow",
    handler="continue_run",
    timeout_seconds=190.0,
    retry_policy="none",
    output_kind="text",
    enabled=True,
)

EXEC_SPEC = IntentSpec(
    name="exec",
    params_schema=DenyParams,
    risk="forbidden",
    policy="deny",
    handler="deny",
    timeout_seconds=1.0,
    retry_policy="none",
    output_kind="text",
    enabled=True,
)


TASK_SPEC = IntentSpec(
    name="task",
    params_schema=TaskParams,
    risk="high",
    policy="allow",
    handler="task",
    timeout_seconds=10.0,
    retry_policy="none",
    output_kind="text",
    enabled=True,
)


SESSIONS_SPEC = IntentSpec(
    name="sessions",
    params_schema=SessionsParams,
    risk="low",
    policy="allow",
    handler="sessions",
    timeout_seconds=5.0,
    retry_policy="none",
    output_kind="text",
    enabled=True,
)

USE_SESSION_SPEC = IntentSpec(
    name="use_session",
    params_schema=UseSessionParams,
    risk="low",
    policy="allow",
    handler="use_session",
    timeout_seconds=5.0,
    retry_policy="none",
    output_kind="text",
    enabled=True,
)

NEW_SESSION_SPEC = IntentSpec(
    name="new_session",
    params_schema=NewSessionParams,
    risk="low",
    policy="allow",
    handler="new_session",
    timeout_seconds=5.0,
    retry_policy="none",
    output_kind="text",
    enabled=True,
)

POSTER_SPEC = IntentSpec(
    name="poster",
    params_schema=PosterParams,
    risk="low",
    policy="allow",
    handler="poster",
    timeout_seconds=180.0,
    retry_policy="none",
    output_kind="image",
    enabled=True,
)


INTENT_REGISTRY: dict[str, IntentSpec] = {
    spec.name: spec
    for spec in (
        STATUS_SPEC,
        ASK_SPEC,
        READ_FILE_SPEC,
        CANCEL_SPEC,
        CONFIRM_SPEC,
        RESET_SPEC,
        CONTINUE_RUN_SPEC,
        EXEC_SPEC,
        TASK_SPEC,
        SESSIONS_SPEC,
        USE_SESSION_SPEC,
        NEW_SESSION_SPEC,
        POSTER_SPEC,
    )
}


def get_spec(name: str) -> IntentSpec | None:
    return INTENT_REGISTRY.get(name)


def check_contract(intent: str, params: dict) -> tuple[IntentSpec | None, dict, str]:
    """共享契约检查：注册表 + enabled + 参数 Schema。

    返回 (spec, 校验后的参数, 错误码)。错误码为空表示通过。
    """

    spec = get_spec(intent)

    if spec is None:
        return None, {}, "unregistered"

    if not spec.enabled:
        # 保留原参数，便于审计“禁用时原本要读什么”；不参与执行
        return spec, dict(params), "disabled"

    try:
        validated = spec.params_schema(**params)

    except ValidationError:
        return spec, {}, "invalid_params"

    return spec, validated.model_dump(), ""
