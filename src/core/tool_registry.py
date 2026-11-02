"""工具注册表与 allow / ask / deny 审批前置策略。

审批是产品安全控制，不是沙箱隔离。allow 只表示无需人工确认，仍须经过参数、
身份、输出与后续 runner 边界检查。ask / deny 不产生工具 exec。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from src.core.run_control import CancellationToken


POLICIES = ("allow", "ask", "deny")


class ToolPolicyError(PermissionError):
    pass


class ToolApprovalRequired(ToolPolicyError):
    pass


class ToolDenied(ToolPolicyError):
    pass


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    schema: dict[str, Any]
    policy: str
    handler: Callable[..., dict[str, Any]]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(
        self,
        schema: dict[str, Any],
        *,
        policy: str,
        handler: Callable[..., dict[str, Any]],
    ) -> None:
        if policy not in POLICIES:
            raise ValueError(f"未知工具策略：{policy}")
        name = schema.get("function", {}).get("name")
        if not isinstance(name, str) or not name or name in self._tools:
            raise ValueError("工具名称无效或已注册")
        self._tools[name] = ToolDefinition(name, schema, policy, handler)

    def schemas(self) -> list[dict[str, Any]]:
        return [definition.schema for definition in self._tools.values()]

    def policy(self, name: str) -> str:
        definition = self._tools.get(name)
        return definition.policy if definition is not None else "deny"

    def execute(
        self,
        tool_call: Any,
        *,
        cancel_token: CancellationToken | None = None,
    ) -> dict[str, Any]:
        name = tool_call.function.name
        definition = self._tools.get(name)
        if definition is None or definition.policy == "deny":
            raise ToolDenied(f"工具被拒绝：{name}")
        if definition.policy == "ask":
            raise ToolApprovalRequired(f"工具需要用户确认：{name}")
        if cancel_token is not None:
            cancel_token.check()
        result = definition.handler(tool_call, cancel_token=cancel_token)
        if cancel_token is not None:
            cancel_token.check()
        return result
