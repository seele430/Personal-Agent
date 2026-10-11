from dataclasses import dataclass
from enum import Enum


class AccessAction(str, Enum):
    SUCCESS = "success"
    STOP = "stop"
    PAUSE = "pause"
    RETRY = "retry"


@dataclass
class AccessDecision:
    action: AccessAction
    reason: str
    retry_seconds: int = 0


def decide_access(status_code: int) -> AccessDecision:
    """根据 HTTP 状态决定下一步处理策略。"""

    if 200 <= status_code < 300:
        return AccessDecision(
            AccessAction.SUCCESS,
            "请求成功"
        )

    if status_code == 429:
        return AccessDecision(
            AccessAction.PAUSE,
            "请求被限流，等待后重新调度",
            retry_seconds=300
        )

    if status_code in (401, 402, 403):
        return AccessDecision(
            AccessAction.STOP,
            "认证、余额或访问权限异常，需要人工处理"
        )

    if status_code in (400, 404, 422):
        return AccessDecision(
            AccessAction.STOP,
            "请求参数或资源地址异常"
        )

    if status_code in (500, 502, 503, 504):
        return AccessDecision(
            AccessAction.RETRY,
            "服务器临时故障，允许有限重试",
            retry_seconds=60
        )

    return AccessDecision(
        AccessAction.STOP,
        f"未知 HTTP 状态：{status_code}"
    )