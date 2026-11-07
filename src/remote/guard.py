"""只读守卫：ASGI 中间件，默认放行；启用后**先鉴权、再判方法、再判白名单**。

顺序是有讲究的：
1. 没令牌 → 401（不泄露路径是否存在）；
2. 令牌对但方法危险 → 403；
3. 令牌对、方法安全但不在白名单 → 403。

任何一条拒绝都**不调用下游应用**，避免"先执行再拒绝"。
"""

from __future__ import annotations

import json
import time

from src.remote.access_log import AccessLog, build_record, strip_query
from src.remote.config import SAFE_METHODS, RemoteConfig
from src.remote.ratelimit import FailureLimiter, LimitPolicy


UNAUTHORIZED = {
    "error": "unauthorized",
    "detail": "需要 owner 令牌（Authorization: Bearer <token>）",
}

FORBIDDEN_METHOD = {"error": "forbidden", "detail": "远程只允许只读方法"}

FORBIDDEN_PATH = {"error": "forbidden", "detail": "该路径不在远程白名单"}

TOO_MANY = {"error": "too_many_attempts", "detail": "失败次数过多，请稍后再试"}


def _header(scope, name: bytes) -> str:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            return value.decode() if isinstance(value, bytes) else str(value)

    return ""


def extract_token(scope) -> str:
    """从 Authorization 或 cookie 里取令牌。"""

    authorization = _header(scope, b"authorization").strip()

    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()

    cookie = _header(scope, b"cookie")

    for part in cookie.split(";"):
        key, _, value = part.strip().partition("=")

        if key == "pa_remote":
            return value.strip()

    return ""


def client_address(scope) -> str:
    client = scope.get("client") or ()

    return str(client[0]) if client else ""


async def _send_json(send, status: int, payload: dict, *, headers=None) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
                *(headers or []),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class RemoteGuard:
    """包装一个 ASGI 应用。"""

    def __init__(
        self,
        app,
        config: RemoteConfig,
        *,
        log: AccessLog | None = None,
        limiter: FailureLimiter | None = None,
    ):
        self.app = app
        self.config = config
        self.log = log or (AccessLog(config.log_path) if config.log_path else None)
        self.limiter = limiter or FailureLimiter(
            LimitPolicy(
                max_failures=config.max_failures,
                block_seconds=config.block_seconds,
            )
        )

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not self.config.usable:
            await self.app(scope, receive, send)

            return

        started = time.perf_counter()
        method = (scope.get("method") or "").upper()
        # 防御性剥离 query：正常 ASGI 里 path 不含 query，但不该依赖调用方守规矩
        path = strip_query(scope.get("path") or "/")
        token = extract_token(scope)
        client = client_address(scope)
        status = 200
        note = ""

        allowed, retry_after = self.limiter.allow(client)

        if not allowed:
            status = 429
            note = "rate limited"
            await _send_json(
                send,
                status,
                TOO_MANY,
                headers=[(b"retry-after", str(retry_after).encode())],
            )
            self._record(
                method=method, path=path, scope=scope, status=status,
                duration_ms=(time.perf_counter() - started) * 1000, note=note,
            )

            return

        if not token or token != self.config.token:
            status = 401
            note = "bad token"
            self.limiter.record_failure(client)
            await _send_json(send, status, UNAUTHORIZED)

        elif method not in SAFE_METHODS:
            status = 403
            note = f"method {method} blocked"
            await _send_json(send, status, FORBIDDEN_METHOD)

        elif not self.config.allows(path):
            status = 403
            note = "path not allowed"
            await _send_json(send, status, FORBIDDEN_PATH)

        else:
            async def capture(message):
                nonlocal status

                if message.get("type") == "http.response.start":
                    status = int(message.get("status") or 200)

                await send(message)

            await self.app(scope, receive, capture)

        self._record(
            method=method,
            path=path,
            scope=scope,
            status=status,
            duration_ms=(time.perf_counter() - started) * 1000,
            note=note,
        )

    def _record(self, *, method, path, scope, status, duration_ms, note) -> None:
        if self.log is None:
            return

        self.log.append(
            build_record(
                method=method,
                path=path,
                status=status,
                duration_ms=duration_ms,
                client=client_address(scope),
                user_agent=_header(scope, b"user-agent"),
                note=note,
            )
        )
