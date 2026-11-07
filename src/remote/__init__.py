"""远程只读访问（v1.2）：配置、鉴权守卫、访问日志、自签名证书。

默认**关闭**；只有 `PERSONAL_AGENT_REMOTE=1` 且令牌 ≥32 字符时才生效。
"""

from src.remote.access_log import (
    AccessLog,
    AccessRecord,
    build_record,
    redact,
    strip_query,
)
from src.remote.cert import CertError, describe, generate_self_signed
from src.remote.config import (
    DEFAULT_PREFIXES,
    MIN_TOKEN_LENGTH,
    SAFE_METHODS,
    RemoteConfig,
    RemoteConfigError,
    from_env,
)
from src.remote.guard import RemoteGuard, extract_token


__all__ = [
    "DEFAULT_PREFIXES",
    "MIN_TOKEN_LENGTH",
    "SAFE_METHODS",
    "AccessLog",
    "AccessRecord",
    "CertError",
    "RemoteConfig",
    "RemoteConfigError",
    "RemoteGuard",
    "build_record",
    "describe",
    "extract_token",
    "from_env",
    "generate_self_signed",
    "redact",
    "strip_query",
]
