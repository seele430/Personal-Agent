"""外部 Agent 能力快照：三态（True / False / unknown）。

路线图 §6.2 的硬要求："能力未验证时标 unknown，**不得默认可提交或可取消**"。
所以 `can()` 只在显式 True 时放行，unknown 一律拒绝并给原因。
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone


ACTIONS = ("submit", "cancel", "events", "artifacts")


class CapabilityError(ValueError):
    """能力快照不合法。"""


@dataclass(frozen=True)
class CapabilitySnapshot:
    provider: str
    submit: bool | None = None
    cancel: bool | None = None
    events: bool | None = None
    artifacts: bool | None = None
    captured_at: str = ""
    notes: str = ""

    def __post_init__(self):
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise CapabilityError("provider 不能为空")

        for action in ACTIONS:
            value = getattr(self, action)

            if value is not None and not isinstance(value, bool):
                raise CapabilityError(f"{action} 只能是 True/False/None")

    def state(self, action: str) -> str:
        if action not in ACTIONS:
            raise CapabilityError(f"未知能力：{action}")

        value = getattr(self, action)

        if value is True:
            return "verified"

        if value is False:
            return "unsupported"

        return "unknown"

    def can(self, action: str) -> bool:
        """只有显式 True 才算能用。"""

        return self.state(action) == "verified"

    def reason(self, action: str) -> str:
        state = self.state(action)

        if state == "verified":
            return ""

        if state == "unsupported":
            return f"{self.provider} 不支持 {action}（能力探测为 False）"

        return f"{self.provider} 的 {action} 能力未验证（unknown）"

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["states"] = {action: self.state(action) for action in ACTIONS}

        return payload

    @classmethod
    def from_dict(cls, payload: dict) -> "CapabilitySnapshot":
        if not isinstance(payload, dict):
            raise CapabilityError("能力快照必须是对象")

        kwargs = {action: payload.get(action) for action in ACTIONS}

        return cls(
            provider=str(payload.get("provider") or ""),
            captured_at=str(payload.get("captured_at") or ""),
            notes=str(payload.get("notes") or ""),
            **kwargs,
        )


def snapshot(provider: str, **capabilities) -> CapabilitySnapshot:
    """构造快照；未传的能力保持 None（unknown），**不默认 True**。"""

    return CapabilitySnapshot(
        provider=provider,
        captured_at=datetime.now(timezone.utc).isoformat(),
        **{action: capabilities.get(action) for action in ACTIONS},
    )
