"""意图契约与静态注册表。

九字段 `IntentSpec`：name / params_schema / risk / policy / handler /
timeout_seconds / retry_policy / output_kind / enabled。

注册表是**静态字典**，不做动态发现；`handler` 是受信任的键，不是模块路径。
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field


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


class DenyParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


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


INTENT_REGISTRY: dict[str, IntentSpec] = {
    spec.name: spec
    for spec in (
        STATUS_SPEC,
        ASK_SPEC,
        READ_FILE_SPEC,
        CANCEL_SPEC,
        CONFIRM_SPEC,
        EXEC_SPEC,
    )
}


def get_spec(name: str) -> IntentSpec | None:
    return INTENT_REGISTRY.get(name)
