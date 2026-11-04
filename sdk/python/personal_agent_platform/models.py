"""Typed v1 resources.

Unknown response fields are deliberately ignored so additive v1 responses
remain readable by older clients.
"""

from dataclasses import dataclass
from typing import Any, ClassVar, Mapping


@dataclass(frozen=True)
class Sandbox:
    sandbox_id: str
    state: str
    node_id: str
    workspace_id: str
    created_at: str
    expires_at: str
    ttl_seconds: int
    reclaim_reason: str | None = None
    reclaimed_at: str | None = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Sandbox":
        return cls(
            sandbox_id=payload["sandbox_id"],
            state=payload["state"],
            node_id=payload["node_id"],
            workspace_id=payload["workspace_id"],
            created_at=payload["created_at"],
            expires_at=payload["expires_at"],
            ttl_seconds=payload["ttl_seconds"],
            reclaim_reason=payload.get("reclaim_reason"),
            reclaimed_at=payload.get("reclaimed_at"),
        )


@dataclass(frozen=True)
class ExecResult:
    exec_id: str
    sandbox_id: str
    state: str
    timeout_ms: int
    max_output_bytes: int
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    truncated: bool = False
    result_code: str | None = None

    TERMINAL_STATES: ClassVar[frozenset[str]] = frozenset(
        {"succeeded", "failed", "cancelled", "timed_out"}
    )

    @property
    def terminal(self) -> bool:
        return self.state in self.TERMINAL_STATES

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExecResult":
        return cls(
            exec_id=payload["exec_id"],
            sandbox_id=payload["sandbox_id"],
            state=payload["state"],
            timeout_ms=payload["timeout_ms"],
            max_output_bytes=payload["max_output_bytes"],
            exit_code=payload.get("exit_code"),
            stdout=payload.get("stdout", ""),
            stderr=payload.get("stderr", ""),
            started_at=payload.get("started_at"),
            finished_at=payload.get("finished_at"),
            truncated=payload.get("truncated", False),
            result_code=payload.get("result_code"),
        )
