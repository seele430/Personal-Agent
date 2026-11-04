"""Stable SDK and HTTP error semantics."""

from typing import Any


class PlatformError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
        request_id: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.request_id = request_id
        self.retryable = retryable

    def __str__(self) -> str:
        status = (
            f"HTTP {self.status_code}: "
            if self.status_code is not None
            else ""
        )
        return f"{status}{self.code}: {self.message}"


class ApiError(PlatformError):
    """An HTTP error response following the v1 error envelope."""


class ValidationError(ApiError):
    pass


class UnauthorizedError(ApiError):
    """401：缺少或错误的平台访问令牌。"""


class NotFoundError(ApiError):
    pass


class ConflictError(ApiError):
    pass


class GoneError(ApiError):
    pass


class RateLimitError(ApiError):
    pass


class ProtocolError(PlatformError):
    def __init__(self, message: str) -> None:
        super().__init__("INVALID_RESPONSE", message)


class TransportError(PlatformError):
    def __init__(self, message: str) -> None:
        super().__init__("TRANSPORT_ERROR", message, retryable=True)


class RequestTimeoutError(TransportError):
    def __init__(self, message: str) -> None:
        PlatformError.__init__(
            self,
            "REQUEST_TIMEOUT",
            message,
            retryable=True,
        )


class WaitTimeoutError(PlatformError):
    """Local wait deadline expired; the exec may have been cancelled."""

    def __init__(
        self,
        exec_id: str,
        *,
        cancel_result: Any = None,
        cancel_error: PlatformError | None = None,
    ) -> None:
        super().__init__(
            "SDK_WAIT_TIMEOUT",
            f"Timed out waiting for exec {exec_id}",
        )
        self.exec_id = exec_id
        self.cancel_result = cancel_result
        self.cancel_error = cancel_error
