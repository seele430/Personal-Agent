"""远程只读访问的配置：默认关闭，令牌不过关强制关闭。

为什么这么保守：远程模式一旦"忘了配令牌"就打开，等于把产物库暴露给整个局域网。
所以配置层自己会**拒绝**不安全组合，而不是靠文档提醒。
"""

from __future__ import annotations

import os

from dataclasses import dataclass
from pathlib import Path


MIN_TOKEN_LENGTH = 32
DEFAULT_PORT = 8443
DEFAULT_PREFIXES = ("/api/artifacts", "/api/activity", "/health")
SAFE_METHODS = ("GET", "HEAD")


class RemoteConfigError(ValueError):
    """远程配置不合法。"""


@dataclass(frozen=True)
class RemoteConfig:
    enabled: bool
    token: str
    port: int = DEFAULT_PORT
    host: str = "0.0.0.0"
    cert_path: Path | None = None
    key_path: Path | None = None
    log_path: Path | None = None
    allow_prefixes: tuple[str, ...] = DEFAULT_PREFIXES
    reason: str = ""

    def __post_init__(self):
        if not 1 <= int(self.port) <= 65535:
            raise RemoteConfigError("port 必须在 1..65535")

        if self.enabled:
            if len(self.token) < MIN_TOKEN_LENGTH:
                raise RemoteConfigError(
                    f"远程模式要求令牌 ≥ {MIN_TOKEN_LENGTH} 字符"
                )

            if not self.allow_prefixes:
                raise RemoteConfigError("白名单不能为空")

            for prefix in self.allow_prefixes:
                if not prefix.startswith("/"):
                    raise RemoteConfigError(f"白名单必须以 / 开头：{prefix}")

    @property
    def usable(self) -> bool:
        return self.enabled and bool(self.token)

    def allows(self, path: str) -> bool:
        if not path.startswith("/"):
            return False

        return any(
            path == prefix or path.startswith(prefix.rstrip("/") + "/")
            for prefix in self.allow_prefixes
        )

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "port": self.port,
            "host": self.host,
            "allow_prefixes": list(self.allow_prefixes),
            "cert": str(self.cert_path) if self.cert_path else None,
            "has_token": bool(self.token),
            "reason": self.reason,
        }


def from_env(environ: dict | None = None) -> RemoteConfig:
    """从环境变量读配置；不安全组合一律降级为关闭。"""

    env = dict(os.environ if environ is None else environ)
    token = (env.get("PERSONAL_AGENT_REMOTE_TOKEN") or "").strip()
    requested = (env.get("PERSONAL_AGENT_REMOTE") or "").strip() == "1"

    base = {
        "port": int(env.get("PERSONAL_AGENT_REMOTE_PORT") or DEFAULT_PORT),
        "host": env.get("PERSONAL_AGENT_REMOTE_HOST") or "0.0.0.0",
        "cert_path": _path(env.get("PERSONAL_AGENT_REMOTE_CERT")),
        "key_path": _path(env.get("PERSONAL_AGENT_REMOTE_KEY")),
        "log_path": _path(env.get("PERSONAL_AGENT_REMOTE_LOG")),
    }

    if not requested:
        return RemoteConfig(enabled=False, token=token, reason="未开启", **base)

    if len(token) < MIN_TOKEN_LENGTH:
        return RemoteConfig(
            enabled=False,
            token=token,
            reason=f"令牌不足 {MIN_TOKEN_LENGTH} 字符，强制关闭",
            **base,
        )

    return RemoteConfig(enabled=True, token=token, **base)


def _path(value: str | None) -> Path | None:
    return Path(value) if value else None
